#!/usr/bin/env python3
"""Headless MarketMind pipeline driver for Codex CLI — Claude-parity execution.

Why this exists
---------------
Under Claude Code each desk/analyst/writer runs as a `context: fork` subagent: a
fresh, full-context turn that loads the whole SKILL.md and produces a deep
artifact. When Codex drives the orchestrator skill inline, all 15 stages share
ONE context and compress each other → shallow output. Codex's `codex exec`
runs one task per invocation with a FRESH context — the exact analog of Claude's
fork. This driver orchestrates the 15-stage pipeline in code, running:

  * deterministic stages   -> committed Python directly (no LLM)
  * LLM stages             -> their own `codex exec` (fresh context per stage)
  * independent stages      -> in parallel (collect desks, analyst memos, critics)

so every analyst/desk/writer gets a full fresh context and the output matches a
Claude run. The depth gate (eval/graders/depth_grader.py) runs alongside as a
safety net.

Usage
-----
    .venv/bin/python3 scripts/run_codex_pipeline.py workspaces/TSLA
    .venv/bin/python3 scripts/run_codex_pipeline.py workspaces/TSLA --dry-run
    .venv/bin/python3 scripts/run_codex_pipeline.py workspaces/TSLA --from quant --to export
    .venv/bin/python3 scripts/run_codex_pipeline.py workspaces/TSLA --model gpt-5-codex

Prereqs (real run): `codex` on PATH, the 3 MCP servers in ~/.codex/config.toml
(see .agents/references/codex-config.toml), and NEWSAPI_KEY exported for evidence
parity. `--dry-run` needs none of this — it prints the full ordered plan.
"""
import argparse
import itertools
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = str(ROOT / ".venv" / "bin" / "python3")
CODEX = "codex"
sys.path.insert(0, str(ROOT / "mcp"))
from shared import contracts  # noqa: E402
from shared.contracts import STAGES  # noqa: E402

# ── small utilities ────────────────────────────────────────────────────────


class Ctx:
    def __init__(self, ws: Path, dry: bool, model: str | None, max_workers: int):
        self.ws = ws
        self.dry = dry
        self.model = model
        self.max_workers = max_workers
        self.cfg = {}
        self.date = None
        self.ticker = ws.name
        self.lang = "en"
        self.run_mode = "daily"
        self.plan = []  # recorded commands (for --dry-run / tests)


def log(msg: str):
    print(f"[driver] {msg}", flush=True)


def _run(cmd: list[str], ctx: Ctx, cwd: Path = ROOT, critical: bool = True,
         stdout_path: Path | None = None) -> int:
    """Run a subprocess (or record it in dry-run). Returns exit code."""
    pretty = " ".join(cmd if len(cmd) < 12 else cmd[:11] + ["…"])
    ctx.plan.append(cmd)
    if ctx.dry:
        print(f"  $ {pretty}")
        return 0
    log(f"$ {pretty}")
    if stdout_path is not None:
        with open(stdout_path, "w", encoding="utf-8") as out:
            proc = subprocess.run(cmd, cwd=str(cwd), stdout=out)
    else:
        proc = subprocess.run(cmd, cwd=str(cwd))
    if proc.returncode != 0 and critical:
        raise RuntimeError(f"command failed ({proc.returncode}): {pretty}")
    return proc.returncode


def det(ctx: Ctx, module: str, *args: str, critical: bool = True) -> int:
    """Run a deterministic committed-Python stage."""
    return _run([VENV_PY, module, *map(str, args)], ctx, critical=critical)


_USAGE_LOCK = threading.Lock()
_USAGE_SEQ = itertools.count(1)


def _usage_stream(ctx: Ctx, skill: str, extra: tuple) -> Path | None:
    """Where this call's `codex exec --json` events go (read by
    eval/usage_telemetry.py), with a sidecar naming its stage and skill."""
    if ctx.dry:
        return None
    with _USAGE_LOCK:
        n = next(_USAGE_SEQ)
    d = ctx.ws / "eval" / ctx.date / "usage"
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{n:03d}_{skill}" + ("_" + "_".join(extra) if extra else "")
    stage = getattr(ctx, "current_stage", None) or "unknown"
    d.joinpath(f"{stem}.meta.json").write_text(json.dumps(
        {"stage": stage, "skill": skill, "args": list(extra), "at": _now()}), encoding="utf-8")
    return d / f"{stem}.jsonl"


def codex_skill(ctx: Ctx, skill: str, *extra: str, critical: bool = True) -> int:
    """Run one LLM stage as a fresh `codex exec` (fresh context = Claude's fork)."""
    arg_lines = [f"$ARGUMENTS[0]={ctx.ws}", f"$ARGUMENTS[1]={ctx.date}"]
    arg_lines += [f"$ARGUMENTS[{i + 2}]={v}" for i, v in enumerate(extra)]
    prompt = (
        f"You are running the '{skill}' stage of the MarketMind-AlphaEngine "
        f"equity-research pipeline. Read .agents/skills/{skill}/SKILL.md and "
        f"follow it EXACTLY. Arguments: {' '.join(arg_lines)}. Produce the "
        f"COMPLETE artifact(s) the skill specifies at full institutional depth — "
        f"no stubs, no abbreviation; an analyst memo is a multi-paragraph argument, "
        f"not a few sentences. Use .venv/bin/python3 for any Python. Write all "
        f"artifacts to the dated workspace paths. When finished, print only the "
        f"artifact path(s) you wrote."
    )
    # workspace-write disables network egress by DEFAULT; the collection desks
    # (and any shell requests fallback when an MCP tool isn't attached)
    # need outbound DNS/HTTP. Grant it per-invocation so the fix doesn't depend on
    # the user's global ~/.codex/config.toml.
    cmd = [CODEX, "exec", "-C", str(ROOT),
           "--sandbox", "workspace-write",
           "-c", "sandbox_workspace_write.network_access=true",
           "--skip-git-repo-check", "--json"]
    if ctx.model:
        cmd += ["--model", ctx.model]
    cmd.append(prompt)
    return _run(cmd, ctx, critical=critical, stdout_path=_usage_stream(ctx, skill, extra))


def parallel(ctx: Ctx, thunks: list):
    """Run independent stage thunks concurrently (sequential in dry-run)."""
    if ctx.dry or len(thunks) == 1:
        for t in thunks:
            t()
        return
    with ThreadPoolExecutor(max_workers=min(ctx.max_workers, len(thunks))) as ex:
        list(ex.map(lambda t: t(), thunks))


def role_to_skill(role: str) -> str:
    return "mm-" + role.replace("_", "-")  # company_analyst -> mm-company-analyst


# ── status + config ────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_stage(ctx: Ctx, stage: str, completed: str | None = None):
    """Write status.json (mirrors the orchestrator / workspace MCP)."""
    if ctx.dry:
        return
    path = ctx.ws / "status.json"
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {"ticker": ctx.ticker, "run_mode": ctx.run_mode,
              "started_at": _now(), "stages_completed": [], "errors": []}
    st["stage"] = stage
    st["run_date"] = ctx.date
    st["updated_at"] = _now()
    st.setdefault("stages_completed", [])
    if completed and completed in STAGES and completed not in st["stages_completed"]:
        st["stages_completed"].append(completed)
    contracts.write_atomic(path, json.dumps(st, indent=2, ensure_ascii=False))


def add_error(ctx: Ctx, message: str) -> None:
    """Append to status.json errors[] (the orchestrator's update_status(error=...))."""
    if ctx.dry:
        return
    path = ctx.ws / "status.json"
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {"ticker": ctx.ticker, "stages_completed": [], "errors": []}
    st.setdefault("errors", []).append({"message": message, "at": _now()})
    contracts.write_atomic(path, json.dumps(st, indent=2, ensure_ascii=False))


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_config(ctx: Ctx):
    """Merge example + root config.yaml + workspace config.yaml -> resolved_config.json."""
    import yaml
    merged = {}
    for p in (ROOT / "config.example.yaml", ROOT / "config.yaml", ctx.ws / "config.yaml"):
        if p.exists():
            merged = _deep_merge(merged, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    merged.setdefault("company", {}).setdefault("ticker", ctx.ws.name)
    if not ctx.dry:
        (ctx.ws / "resolved_config.json").write_text(
            json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    ctx.cfg = merged
    ctx.ticker = merged.get("company", {}).get("ticker") or ctx.ws.name
    ctx.lang = merged.get("language", "en")
    ctx.run_mode = merged.get("run_mode", "daily")


def load_existing_config(ctx: Ctx):
    """In dry-run / resume, read an existing resolved_config.json if present."""
    p = ctx.ws / "resolved_config.json"
    if p.exists():
        try:
            ctx.cfg = json.loads(p.read_text(encoding="utf-8"))
            ctx.ticker = ctx.cfg.get("company", {}).get("ticker") or ctx.ws.name
            ctx.lang = ctx.cfg.get("language", "en")
            ctx.run_mode = ctx.cfg.get("run_mode", "daily")
        except json.JSONDecodeError:
            pass


def resolve_run_date(ctx: Ctx, keep_recorded: bool = False) -> str:
    """The run's date: the last trading session, as the orchestrator does. A run
    recorded for that same date is resumed (see _completed_stages); a run of an
    earlier session, finished or not, never pins the date. An explicit --from
    re-runs part of the recorded run, so it keeps the recorded date."""
    if keep_recorded:
        recorded = (_read_json(ctx.ws / "status.json") or {}).get("run_date")
        if recorded:
            return recorded
    out = subprocess.run([VENV_PY, str(ROOT / "scripts" / "trading_day.py"), str(ctx.ws)],
                         capture_output=True, text=True, cwd=str(ROOT))
    return (out.stdout or "").strip() or datetime.now(timezone.utc).date().isoformat()


def start_fresh_if_new_date(ctx: Ctx) -> None:
    """A new run date starts a new status: the previous run's completed stages
    and errors must not be read as this run's."""
    if ctx.dry:
        return
    st = _read_json(ctx.ws / "status.json")
    if st is not None and st.get("run_date") == ctx.date:
        return
    contracts.write_atomic(ctx.ws / "status.json", json.dumps(
        {"ticker": ctx.ticker, "run_mode": ctx.run_mode, "run_date": ctx.date,
         "stage": None, "started_at": _now(), "updated_at": _now(),
         "stages_completed": [], "errors": []}, indent=2, ensure_ascii=False))


def reopen_stages(ctx: Ctx, stages: list[str]) -> None:
    """A forced re-run (--from) redoes these stages: they are no longer complete, so a
    crash part-way resumes them instead of finding the earlier run marked done."""
    if ctx.dry:
        return
    st = _read_json(ctx.ws / "status.json")
    if st is None or st.get("run_date") != ctx.date:
        return
    st["stages_completed"] = [s for s in st.get("stages_completed") or [] if s not in stages]
    st["stage"] = stages[0] if stages else st.get("stage")
    st["updated_at"] = _now()
    contracts.write_atomic(ctx.ws / "status.json", json.dumps(st, indent=2, ensure_ascii=False))


def _analyst_roles(ctx: Ctx) -> list[str]:
    roles = (ctx.cfg.get("discussion", {}) or {}).get("analyst_roles")
    return roles or ["company_analyst", "chips_analyst", "risk_analyst", "market_analyst"]


def _read_json(path: Path) -> dict | None:
    """A JSON object from a workspace file; None if missing, torn or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── stages ─────────────────────────────────────────────────────────────────


def st_resolve_config(ctx):
    resolve_config(ctx)


def st_init_workspace(ctx):
    if not ctx.dry:
        for sub in ("raw", "normalized", "quant", "valuation", "discussion",
                    "drafts", "reviews", "decision", "final", "exports", "eval"):
            (ctx.ws / sub / ctx.date).mkdir(parents=True, exist_ok=True)
    codex_skill(ctx, "mm-company-resolver")


def st_collect(ctx):
    # Preflight: record which data sources are reachable + which keys are present
    # (sanitized) so a fallback-heavy run self-explains (dns vs auth vs key).
    det(ctx, "scripts/check_data_sources.py", ctx.ws, ctx.date, critical=False)
    # Deterministic macro layer: FRED series collection (keyless fredgraph CSV
    # fallback), regime classification, and per-ticker macro evidence cards.
    det(ctx, "scripts/collect_macro_series.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "scripts/compute_macro_regime.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "scripts/macro_evidence_cards.py", ctx.ws, ctx.date, critical=False)
    # A-share chip/flow signals (akshare; CN-only, per-block degradation)
    det(ctx, "scripts/collect_cn_chips.py", ctx.ws, ctx.date, critical=False)
    desks = ["mm-market-desk", "mm-company-desk", "mm-sector-desk", "mm-web-research"]
    parallel(ctx, [lambda d=d: codex_skill(ctx, d, critical=False) for d in desks])


def st_normalize(ctx):
    # Chip structure needs the desks' raw price CSVs, and its evidence cards
    # must exist before dedup — hence computed here, not in st_quant.
    det(ctx, "scripts/compute_chip_structure.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "scripts/chip_evidence_cards.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "normalize/dedup_evidence.py", ctx.ws, ctx.date)


def st_quant(ctx):
    # Deterministic 1h/4h timing block (timing-only contract: frames entry/exit
    # zones, never a vote reason). Runs before the quant LLM so it can report status.
    det(ctx, "scripts/intraday_timing.py", ctx.ws, ctx.date, critical=False)
    # Peer-cohort path divergence (which names follow the sector, which walk alone)
    det(ctx, "scripts/peer_divergence.py", ctx.ws, ctx.date, critical=False)
    codex_skill(ctx, "mm-quant-analyst")


def st_valuation(ctx):
    det(ctx, "valuation/run_valuation.py", ctx.ws, ctx.date, critical=False)
    # Bundle shared context (quant + valuation + profile + peers + catalysts +
    # macro_regime + intraday) for all downstream agents.
    det(ctx, "scripts/build_shared_context.py", ctx.ws, ctx.date, critical=False)


def st_discuss_memos(ctx):
    det(ctx, "memory/retrieval.py", ctx.ws, ctx.date, "analyst", critical=False)
    roles = _analyst_roles(ctx)
    parallel(ctx, [lambda r=r: codex_skill(ctx, role_to_skill(r), "memo") for r in roles])


def st_discuss_debate(ctx):
    # Multi-round discussion panel (mirrors the decision panel): each analyst role
    # files a structured view (stance + conviction) per round; the chair tallies;
    # a deterministic grader decides iterate-vs-exit with a hard max_rounds cap.
    panel = (ctx.cfg.get("discussion", {}) or {}).get("panel", {}) or {}
    if not panel.get("enabled", True):
        return  # panel off → memos feed straight into synthesis (no cross-critique)
    roles = _analyst_roles(ctx)
    max_r = int(panel.get("max_rounds", 3))
    for r in range(1, max_r + 1):
        # 1. each role files a view (stance + conviction + challenges) — fresh context each
        parallel(ctx, [
            (lambda role=role: codex_skill(ctx, "mm-discussion-panelist", role, str(r), critical=False))
            for role in roles
        ])
        # 2. chair tallies views + surfaces retained dissent
        codex_skill(ctx, "mm-discussion-moderator", "tally", str(r), critical=False)
        # 3. deterministic convergence grader decides iterate-vs-exit (hard cap at max_rounds);
        #    a verdict the grader did not write this round (it crashed) is never acted on
        _clear(ctx, contracts.discussion_convergence_path(ctx.ws, ctx.date, r))
        det(ctx, "eval/graders/discussion_convergence_grader.py", ctx.ws, ctx.date, str(r), critical=False)
        if ctx.dry:
            print(f"  # (round {r}) if convergence>=threshold and round>=min_rounds -> exit; else next round")
            break
        conv = _read_json(contracts.discussion_convergence_path(ctx.ws, ctx.date, r))
        if (conv or {}).get("exit"):
            log(f"discussion panel converged on round {r}: {(conv or {}).get('exit_reason')}")
            break
        log(f"discussion panel round {r}: no convergence, continuing")


def st_discuss_synthesis(ctx):
    # depth gate (anti-shallow): redo thin memos once before synthesis
    det(ctx, "eval/graders/depth_grader.py", ctx.ws, ctx.date, "--memos-only", critical=False)
    res = _read_json(ctx.ws / "eval" / ctx.date / "depth_result.json")
    thin = []
    if res and not res.get("pass"):
        for name in res.get("thin_artifacts", []):
            if name.endswith(".md"):
                thin.append(role_to_skill(name[:-3]))
    if ctx.dry:
        print("  # (if memos thin) re-run those analysts once with a full-depth instruction")
    elif thin:
        log(f"depth gate: redoing thin memos once -> {thin}")
        parallel(ctx, [lambda s=s: codex_skill(ctx, s, "memo", critical=False) for s in thin])
    codex_skill(ctx, "mm-discussion-moderator", "synthesis")


def st_draft(ctx):
    det(ctx, "memory/retrieval.py", ctx.ws, ctx.date, "writer", critical=False)
    codex_skill(ctx, "mm-report-writer", "initial")


def st_review(ctx):
    max_loops = int((ctx.cfg.get("review", {}) or {}).get("max_revision_loops", 3))
    det(ctx, "memory/retrieval.py", ctx.ws, ctx.date, "reviewer", critical=False)
    for i in range(max_loops):
        det(ctx, "eval/graders/depth_grader.py", ctx.ws, ctx.date, "--report-only", critical=False)
        codex_skill(ctx, "mm-report-reviewer")
        if ctx.dry:
            print(f"  # (loop {i+1}) if reviewer+depth pass -> stop; else revise")
            break
        review = _latest_review(ctx)
        depth = _read_json(ctx.ws / "eval" / ctx.date / "depth_result.json")
        passed = (review or {}).get("pass") and (depth or {}).get("pass", True)
        if passed:
            log(f"review passed on loop {i+1}")
            break
        log(f"review loop {i+1}: revising")
        codex_skill(ctx, "mm-report-writer", "revision")


def _latest_review(ctx):
    d = ctx.ws / "reviews" / ctx.date / "final_reviews"
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    return _read_json(files[-1]) if files else None


def _clear(ctx, path: Path) -> None:
    if not ctx.dry:
        try:
            path.unlink(missing_ok=True)
        except OSError:   # not a file (a directory, say): _read_json reads it as nothing
            pass


def _ledger_outcome(ctx) -> dict | None:
    """This run date's release outcome in the ledger (sealed or blocked), if any.
    A ledger that cannot be read yields {"type": "unreadable"}: it may hold one."""
    if ctx.dry:
        return None
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from ledger import seal as ledger_seal
        return ledger_seal.outcome(ctx.ws, ctx.date)
    except Exception as exc:  # noqa: BLE001 - unknown is not none
        return {"type": "unreadable", "seq": None, "error": str(exc)}


def _release_check(ctx, ledger: bool = True) -> dict:
    if str(ROOT / "eval") not in sys.path:
        sys.path.insert(0, str(ROOT / "eval"))
    from pre_release_gate import release_check
    return release_check(ctx.ws, ctx.date, ledger=ledger)


class LedgerUnavailable(RuntimeError):
    """The ledger cannot be verified, so this run date's outcome is unknown. Raised
    out of the decide stage: the stage fails, is not marked complete, and a normal
    re-run after the ledger is repaired decides (or finds the outcome) then."""


def st_decide(ctx):
    # A run date has one release outcome. Once the ledger holds it (a crash after
    # the seal or the block, a resume, --from decide), deciding again could only
    # produce a decision that can never be sealed or published.
    settled = _ledger_outcome(ctx)
    if settled and settled["type"] == "unreadable":
        add_error(ctx, f"ledger unreadable, so {ctx.date}'s outcome is unknown; not deciding "
                       f"until it is repaired (ledger/cli.py verify): {settled['error']}")
        raise LedgerUnavailable("ledger unreadable; repair it, then re-run to resume at decide")
    if settled:
        log(f"decide: the ledger already holds record {settled['seq']} ({settled['type']}) "
            f"for {ctx.date}; not deciding again")
        if settled["type"] == "gate_block":
            errors = (_read_json(ctx.ws / "status.json") or {}).get("errors") or []
            if not any("pre_release_gate blocked" in str(e.get("message", "")) for e in errors
                       if isinstance(e, dict)):
                add_error(ctx, f"pre_release_gate blocked (ledger record {settled['seq']})")
        return
    panel = (ctx.cfg.get("decision", {}) or {}).get("panel", {}) or {}
    if not panel.get("enabled", True):
        codex_skill(ctx, "mm-decision-maker")   # legacy single-shot decision
        gate_decision(ctx)
        return
    roles = _analyst_roles(ctx)
    max_r = int(panel.get("max_rounds", 3))
    for r in range(1, max_r + 1):
        # 1. each role casts a ballot (vote + conviction + overlay) — fresh context each
        parallel(ctx, [
            (lambda role=role: codex_skill(ctx, "mm-decision-panelist", role, str(r), critical=False))
            for role in roles
        ])
        # 2. chair tallies ballots + surfaces retained dissent
        codex_skill(ctx, "mm-decision-maker", "tally", str(r), critical=False)
        # 3. deterministic convergence grader decides iterate-vs-exit (hard cap at max_rounds);
        #    a verdict the grader did not write this round (it crashed) is never acted on
        _clear(ctx, contracts.panel_convergence_path(ctx.ws, ctx.date, r))
        det(ctx, "eval/graders/panel_convergence_grader.py", ctx.ws, ctx.date, str(r), critical=False)
        if ctx.dry:
            print(f"  # (round {r}) if convergence>=threshold and round>=min_rounds -> exit; else next round")
            break
        conv = _read_json(contracts.panel_convergence_path(ctx.ws, ctx.date, r))
        if (conv or {}).get("exit"):
            log(f"panel converged on round {r}: {(conv or {}).get('exit_reason')}")
            break
        log(f"panel round {r}: no convergence, continuing")
    # 4. chair writes the final decision (reads panel summaries -> risk_overlay + panel block)
    codex_skill(ctx, "mm-decision-maker")
    gate_decision(ctx)


def _gate(ctx) -> str:
    """Run the pre-release gate, then re-check rather than trust its file: the
    gate removes any earlier verdict before it runs, and release_check
    re-evaluates the decision, so a crashed gate or a planted verdict reads
    blocked. Exit 0 is passed and 2 blocked; anything else is a crash."""
    rc = det(ctx, "eval/pre_release_gate.py", ctx.ws, ctx.date, critical=False)
    if ctx.dry:
        return "passed"
    if rc not in (0, 2):
        return "blocked"
    # the decision alone; whether it is the sealed one is st_decide's and export's question
    return "passed" if _release_check(ctx, ledger=False)["status"] == "passed" else "blocked"


def gate_decision(ctx) -> str:
    """Gate -> one revise on a block -> gate again. A second block releases the
    run as blocked: recorded in status.json, and export renders nothing."""
    if ctx.dry:
        print("  # pre-release gate; if blocked: keep final_decision.pre_gate.json, "
              "run mm-decision-maker revise once, gate again")
    status = _gate(ctx)
    if status == "passed":
        _seal(ctx)
        return status
    dec = contracts.decision_path(ctx.ws, ctx.date)
    if dec.exists():
        contracts.pre_gate_decision_path(ctx.ws, ctx.date).write_text(
            dec.read_text(encoding="utf-8"), encoding="utf-8")
    log("pre-release gate blocked the decision; one revision")
    codex_skill(ctx, "mm-decision-maker", "revise", critical=False)
    status = _gate(ctx)
    if status == "passed":
        _seal(ctx)
    else:
        checks = sorted({str(e.get("check", "?")) for e in _release_check(ctx, ledger=False)["errors"]}) or ["unreadable"]
        # ledger first: a crash between the two then resumes into st_decide's
        # settled path, which adds the missing error — never an error without a block
        if det(ctx, "ledger/cli.py", "block", ctx.ws, ctx.date, critical=False) != 0:
            add_error(ctx, "ledger block not recorded: the ledger refused it (ledger/cli.py verify)")
        add_error(ctx, f"pre_release_gate blocked: {', '.join(checks)}")
        log(f"release BLOCKED by the pre-release gate ({', '.join(checks)})")
    return status


def _seal(ctx) -> None:
    """Seal the passed decision in the ledger. Never blocks the release, but a
    failure is recorded loudly: an unsealed decision cannot be scored."""
    if det(ctx, "ledger/cli.py", "seal", ctx.ws, ctx.date, critical=False) != 0:
        add_error(ctx, "ledger seal failed: decision released but NOT sealed (run `ledger/cli.py init`?)")
        log("WARNING: ledger seal failed — the decision is not in the ledger")


def st_export(ctx):
    # Same command as orchestrator stage 13: re-check the decision, then the
    # report and the decision land in final/ — or BLOCKED.md does.
    if not ctx.dry:
        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        import export_release
        result = export_release.export(ctx.ws, ctx.date, ctx.run_mode, ctx.lang)
        if result["status"] == "blocked":
            log("export skipped: release blocked (final/{date}/BLOCKED.md)")
            return
    chart_args = [ctx.ws, ctx.date, ctx.ticker]
    if ctx.lang == "ch":
        chart_args.append("--lang")
        chart_args.append("ch")
    det(ctx, "templates/charts.py", *chart_args, critical=False)
    det(ctx, "templates/render_pdf.py", ctx.ws, ctx.date, critical=False)


def st_user_review(ctx):
    out = contracts.user_review_path(ctx.ws, ctx.date)
    if ctx.dry:
        print("  # prompt on stdin if a TTY, else write empty user_review.json (headless)")
        return
    blocked = contracts.blocked_report_path(ctx.ws, ctx.date)
    if blocked.exists():
        print(blocked.read_text(encoding="utf-8"))
    feedback = ""
    if sys.stdin.isatty():
        try:
            feedback = input("[driver] Optional user review feedback (Enter to skip): ").strip()
        except EOFError:
            feedback = ""
    out.parent.mkdir(parents=True, exist_ok=True)
    review = ({"reviewed": True, "skipped": False, "agrees_with_decision": None,
               "user_feedback": feedback, "timestamp": _now()} if feedback
              else {"reviewed": False, "skipped": True, "timestamp": _now()})
    out.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")


def st_reflect(ctx):
    # non-critical: never abort the run
    for g in ("factuality_grader", "evidence_grader", "consistency_grader",
              "valuation_grader", "depth_grader", "decision_risk_grader",
              "story_grader", "language_purity_grader", "cost_tracker"):
        det(ctx, f"eval/graders/{g}.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "eval/release_gate.py", ctx.ws, ctx.date, critical=False)
    # Ledger upkeep: upgrade pending OpenTimestamps receipts and score any
    # sealed decision whose horizon has matured (all tickers, not just this one).
    det(ctx, "ledger/cli.py", "stamps", critical=False)
    det(ctx, "ledger/cli.py", "score", critical=False)
    det(ctx, "memory/retrieval.py", ctx.ws, ctx.date, "analyst", critical=False)
    codex_skill(ctx, "mm-memory-writer", critical=False)
    # usage last, so the memory writer's tokens are counted
    det(ctx, "eval/usage_telemetry.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "eval/finalize_run.py", ctx.ws, ctx.date, critical=False)


STAGE_FUNCS = {
    "resolve_config": st_resolve_config,
    "init_workspace": st_init_workspace,
    "collect": st_collect,
    "normalize": st_normalize,
    "quant": st_quant,
    "valuation": st_valuation,
    "discuss_memos": st_discuss_memos,
    "discuss_debate": st_discuss_debate,
    "discuss_synthesis": st_discuss_synthesis,
    "draft": st_draft,
    "review": st_review,
    "decide": st_decide,
    "export": st_export,
    "user_review": st_user_review,
    "reflect": st_reflect,
}
assert list(STAGE_FUNCS.keys()) == STAGES, "stage plan must match contracts.STAGES"


def _completed_stages(ctx: Ctx) -> set[str]:
    """Stages already completed for THIS run_date, per status.json.

    Mirrors the orchestrator skill's resume behavior: a same-date re-run skips
    completed stages; a different run_date is a fresh run and skips nothing.
    """
    try:
        st = json.loads((ctx.ws / "status.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    if st.get("run_date") != ctx.date:
        return set()
    done = st.get("stages_completed")
    return {s for s in done if s in STAGES} if isinstance(done, list) else set()


def run_pipeline(ctx: Ctx, from_stage: str | None, to_stage: str | None):
    start = STAGES.index(from_stage) if from_stage else 0
    end = STAGES.index(to_stage) + 1 if to_stage else len(STAGES)
    # config + date must be known even when resuming mid-pipeline; load any
    # existing resolved_config so the header/lang are accurate (the
    # resolve_config stage re-merges fresh when it runs).
    load_existing_config(ctx)
    ctx.date = resolve_run_date(ctx, keep_recorded=from_stage is not None)
    start_fresh_if_new_date(ctx)
    # An explicit --from is a forced re-run from that stage; otherwise resume by
    # skipping stages status.json already records as completed for this date.
    # Dry runs never skip — --dry-run previews the full plan, not resume state.
    skip = _completed_stages(ctx) if (from_stage is None and not ctx.dry) else set()
    if from_stage is not None:
        reopen_stages(ctx, STAGES[start:])
    log(f"ticker={ctx.ticker} run_date={ctx.date} lang={ctx.lang} "
        f"stages={STAGES[start]}..{STAGES[end-1]} dry_run={ctx.dry}")
    for stage in STAGES[start:end]:
        if stage in skip:
            log(f"skipping {stage} (already completed for {ctx.date})")
            continue
        header = f"=== STAGE: {stage} ==="
        print(f"\n{header}")
        det(ctx, "eval/stage_timer.py", "start", ctx.ws, stage, critical=False)
        ctx.current_stage = stage
        try:
            STAGE_FUNCS[stage](ctx)
            det(ctx, "eval/stage_timer.py", "end", ctx.ws, stage, "true", critical=False)
            set_stage(ctx, stage, completed=stage)
        except Exception as e:  # noqa: BLE001
            det(ctx, "eval/stage_timer.py", "end", ctx.ws, stage, "false", str(e), critical=False)
            if stage == "reflect":
                log(f"reflect failed (non-critical): {e}")
                set_stage(ctx, stage, completed=stage)
            else:
                log(f"FATAL in stage '{stage}': {e}")
                raise
    if end == len(STAGES) and not ctx.dry:
        set_stage(ctx, "completed")
    log("pipeline complete" if end == len(STAGES) else "partial run complete")


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless MarketMind pipeline driver for Codex CLI.")
    ap.add_argument("workspace", help="path to workspaces/{TICKER}")
    ap.add_argument("--dry-run", action="store_true", help="print the full plan, execute nothing")
    ap.add_argument("--from", dest="from_stage", choices=STAGES, help="start at this stage")
    ap.add_argument("--to", dest="to_stage", choices=STAGES, help="stop after this stage")
    ap.add_argument("--model", help="codex model override for LLM stages")
    ap.add_argument("--max-workers", type=int, default=4, help="parallel codex exec cap")
    args = ap.parse_args()

    ws = Path(args.workspace)
    if not ws.exists():
        print(f"workspace not found: {ws}", file=sys.stderr)
        return 2
    ctx = Ctx(ws, args.dry_run, args.model, args.max_workers)
    run_pipeline(ctx, args.from_stage, args.to_stage)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = str(ROOT / ".venv" / "bin" / "python3")
CODEX = "codex"
sys.path.insert(0, str(ROOT / "mcp"))
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


def _run(cmd: list[str], ctx: Ctx, cwd: Path = ROOT, critical: bool = True) -> int:
    """Run a subprocess (or record it in dry-run). Returns exit code."""
    pretty = " ".join(cmd if len(cmd) < 12 else cmd[:11] + ["…"])
    ctx.plan.append(cmd)
    if ctx.dry:
        print(f"  $ {pretty}")
        return 0
    log(f"$ {pretty}")
    proc = subprocess.run(cmd, cwd=str(cwd))
    if proc.returncode != 0 and critical:
        raise RuntimeError(f"command failed ({proc.returncode}): {pretty}")
    return proc.returncode


def det(ctx: Ctx, module: str, *args: str, critical: bool = True) -> int:
    """Run a deterministic committed-Python stage."""
    return _run([VENV_PY, module, *map(str, args)], ctx, critical=critical)


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
    # (and any shell yfinance/requests fallback when an MCP tool isn't attached)
    # need outbound DNS/HTTP. Grant it per-invocation so the fix doesn't depend on
    # the user's global ~/.codex/config.toml.
    cmd = [CODEX, "exec", "-C", str(ROOT),
           "--sandbox", "workspace-write",
           "-c", "sandbox_workspace_write.network_access=true",
           "--skip-git-repo-check"]
    if ctx.model:
        cmd += ["--model", ctx.model]
    cmd.append(prompt)
    return _run(cmd, ctx, critical=critical)


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
    path.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


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


def resolve_run_date(ctx: Ctx) -> str:
    p = ctx.ws / "status.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8")).get("run_date")
            if d:
                return d
        except json.JSONDecodeError:
            pass
    out = subprocess.run([VENV_PY, str(ROOT / "scripts" / "trading_day.py"), str(ctx.ws)],
                         capture_output=True, text=True, cwd=str(ROOT))
    return (out.stdout or "").strip() or datetime.now(timezone.utc).date().isoformat()


def _analyst_roles(ctx: Ctx) -> list[str]:
    roles = (ctx.cfg.get("discussion", {}) or {}).get("analyst_roles")
    return roles or ["company_analyst", "risk_analyst", "market_analyst"]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


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
    # Deterministic macro layer: FRED-first series collection (yfinance proxy
    # fallback), regime classification, and per-ticker macro evidence cards.
    det(ctx, "scripts/collect_macro_series.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "scripts/compute_macro_regime.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "scripts/macro_evidence_cards.py", ctx.ws, ctx.date, critical=False)
    desks = ["mm-market-desk", "mm-company-desk", "mm-sector-desk", "mm-web-research"]
    parallel(ctx, [lambda d=d: codex_skill(ctx, d, critical=False) for d in desks])


def st_normalize(ctx):
    det(ctx, "normalize/dedup_evidence.py", ctx.ws, ctx.date)


def st_quant(ctx):
    # Deterministic 1h/4h timing block (timing-only contract: frames entry/exit
    # zones, never a vote reason). Runs before the quant LLM so it can report status.
    det(ctx, "scripts/intraday_timing.py", ctx.ws, ctx.date, critical=False)
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
        # 3. deterministic convergence grader decides iterate-vs-exit (hard cap at max_rounds)
        det(ctx, "eval/graders/discussion_convergence_grader.py", ctx.ws, ctx.date, str(r), critical=False)
        if ctx.dry:
            print(f"  # (round {r}) if convergence>=threshold and round>=min_rounds -> exit; else next round")
            break
        conv = _read_json(ctx.ws / "discussion" / ctx.date / "panel" / f"convergence_round_{r}.json")
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


def st_decide(ctx):
    panel = (ctx.cfg.get("decision", {}) or {}).get("panel", {}) or {}
    if not panel.get("enabled", True):
        codex_skill(ctx, "mm-decision-maker")   # legacy single-shot decision
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
        # 3. deterministic convergence grader decides iterate-vs-exit (hard cap at max_rounds)
        det(ctx, "eval/graders/panel_convergence_grader.py", ctx.ws, ctx.date, str(r), critical=False)
        if ctx.dry:
            print(f"  # (round {r}) if convergence>=threshold and round>=min_rounds -> exit; else next round")
            break
        conv = _read_json(ctx.ws / "decision" / ctx.date / "panel" / f"convergence_round_{r}.json")
        if (conv or {}).get("exit"):
            log(f"panel converged on round {r}: {(conv or {}).get('exit_reason')}")
            break
        log(f"panel round {r}: no convergence, continuing")
    # 4. chair writes the final decision (reads panel summaries -> risk_overlay + panel block)
    codex_skill(ctx, "mm-decision-maker")


def st_export(ctx):
    chart_args = [ctx.ws, ctx.date, ctx.ticker]
    if ctx.lang == "ch":
        chart_args.append("--lang")
        chart_args.append("ch")
    det(ctx, "templates/charts.py", *chart_args, critical=False)
    det(ctx, "templates/render_pdf.py", ctx.ws, ctx.date, critical=False)


def st_user_review(ctx):
    out = ctx.ws / "decision" / ctx.date / "user_review.json"
    if ctx.dry:
        print("  # prompt on stdin if a TTY, else write empty user_review.json (headless)")
        return
    feedback = ""
    if sys.stdin.isatty():
        try:
            feedback = input("[driver] Optional user review feedback (Enter to skip): ").strip()
        except EOFError:
            feedback = ""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"feedback": feedback, "at": _now()}, ensure_ascii=False, indent=2),
                   encoding="utf-8")


def st_reflect(ctx):
    # non-critical: never abort the run
    for g in ("factuality_grader", "evidence_grader", "consistency_grader",
              "valuation_grader", "depth_grader", "decision_risk_grader", "cost_tracker"):
        det(ctx, f"eval/graders/{g}.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "eval/release_gate.py", ctx.ws, ctx.date, critical=False)
    det(ctx, "memory/retrieval.py", ctx.ws, ctx.date, "analyst", critical=False)
    codex_skill(ctx, "mm-memory-writer", critical=False)
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
    ctx.date = resolve_run_date(ctx)
    # An explicit --from is a forced re-run from that stage; otherwise resume by
    # skipping stages status.json already records as completed for this date.
    # Dry runs never skip — --dry-run previews the full plan, not resume state.
    skip = _completed_stages(ctx) if (from_stage is None and not ctx.dry) else set()
    log(f"ticker={ctx.ticker} run_date={ctx.date} lang={ctx.lang} "
        f"stages={STAGES[start]}..{STAGES[end-1]} dry_run={ctx.dry}")
    for stage in STAGES[start:end]:
        if stage in skip:
            log(f"skipping {stage} (already completed for {ctx.date})")
            continue
        header = f"=== STAGE: {stage} ==="
        print(f"\n{header}")
        det(ctx, "eval/stage_timer.py", "start", ctx.ws, stage, critical=False)
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

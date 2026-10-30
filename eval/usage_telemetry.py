#!/usr/bin/env python3
"""Actual model usage per run, by stage and skill (A3).

eval/graders/cost_tracker.py estimates "tokens" as file bytes / 4 and counts
upstream files again as downstream input, so its figures are not spend. This
module reads what the model APIs actually reported:

  Claude Code driver  subagent transcripts under ~/.claude/projects/<project>/
                      <session>/subagents/agent-*.jsonl, whose .meta.json
                      description the orchestrator sets to
                      ``mm:{TICKER}:{date}:{stage}:{skill}``; usage is
                      de-duplicated by API request id. The orchestrator's own
                      turns in those sessions, inside the run window, count
                      as stage ``orchestrator``.
  Codex driver        ``codex exec --json`` event streams saved by
                      scripts/run_codex_pipeline.py under
                      {workspace}/eval/{date}/usage/.

Writes {workspace}/eval/{date}/usage.json. Usage that cannot be attributed is
reported under ``unattributed`` — never dropped and never counted as zero; a
Codex stream whose event schema this parser does not recognise is listed in
``unparsed_codex_files``.

Usage:
    .venv/bin/python3 eval/usage_telemetry.py <workspace> <date> [--projects-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("input", "cache_write", "cache_read", "output", "thinking")
_DESC = re.compile(r"^mm:(?P<ticker>[^:]+):(?P<date>\d{4}-\d{2}-\d{2}):(?P<stage>[^:]+):(?P<skill>.+)$")


def _zero() -> dict:
    return {k: 0 for k in FIELDS} | {"requests": 0}


def _add(acc: dict, u: dict) -> None:
    for k in FIELDS:
        acc[k] += int(u.get(k) or 0)
    acc["requests"] += 1


def claude_usage(message_usage: dict) -> dict:
    return {
        "input": message_usage.get("input_tokens"),
        "cache_write": message_usage.get("cache_creation_input_tokens"),
        "cache_read": message_usage.get("cache_read_input_tokens"),
        "output": message_usage.get("output_tokens"),
        "thinking": (message_usage.get("output_tokens_details") or {}).get("thinking_tokens"),
    }


def _transcript_requests(path: Path, window: tuple[str, str] | None = None):
    """(request_key, model, usage) per API request in one transcript file."""
    seen = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "assistant":
            continue
        # compare to the second: "06:13:12.5Z" sorts after a bound of "06:13:12"
        if window and not (window[0] <= str(ev.get("timestamp", ""))[:19] <= window[1]):
            continue
        msg = ev.get("message") or {}
        usage = msg.get("usage")
        key = ev.get("requestId") or msg.get("id")
        if not usage or key in seen:
            continue
        seen.add(key)
        yield key, msg.get("model"), claude_usage(usage)


def project_dir(projects_dir: Path, repo: Path = PROJECT_ROOT) -> Path:
    return projects_dir / str(repo.resolve()).replace("/", "-")


def collect_claude(ticker: str, date: str, projects_dir: Path, window: tuple[str, str] | None) -> dict:
    by_stage, by_skill, by_model = defaultdict(_zero), defaultdict(_zero), defaultdict(_zero)
    unattributed = _zero()
    sessions: set[Path] = set()
    agents = 0
    base = project_dir(projects_dir)
    for meta_path in base.glob("*/subagents/*.meta.json"):
        try:
            desc = str(json.loads(meta_path.read_text(encoding="utf-8")).get("description", ""))
        except (OSError, ValueError):
            continue
        m = _DESC.match(desc.strip())
        if not m or m["ticker"] != ticker or m["date"] != date:
            continue
        sessions.add(meta_path.parent.parent)
        agents += 1
        transcript = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
        for _, model, u in _transcript_requests(transcript):
            _add(by_stage[m["stage"]], u)
            _add(by_skill[m["skill"]], u)
            _add(by_model[model or "unknown"], u)
    for session_dir in sessions:
        # other subagents in the same session that carry no run description
        for meta_path in session_dir.glob("subagents/*.meta.json"):
            try:
                desc = str(json.loads(meta_path.read_text(encoding="utf-8")).get("description", ""))
            except (OSError, ValueError):
                desc = ""
            if _DESC.match(desc.strip()):
                continue
            transcript = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
            for _, model, u in _transcript_requests(transcript, window):
                _add(unattributed, u)
        main = session_dir.with_suffix(".jsonl")
        for _, model, u in _transcript_requests(main, window):
            _add(by_stage["orchestrator"], u)
            _add(by_skill["mm-orchestrator"], u)
            _add(by_model[model or "unknown"], u)
    return {"agents": agents, "sessions": len(sessions), "by_stage": dict(by_stage),
            "by_skill": dict(by_skill), "by_model": dict(by_model), "unattributed": unattributed}


def codex_usage(path: Path) -> dict | None:
    """Final usage from one `codex exec --json` stream, or None if unrecognised.

    Handles `turn.completed` events carrying `usage` and `token_count` events
    carrying cumulative `info.total_token_usage`; the last one wins."""
    last = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else ev
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
        if usage is None and msg.get("type") == "token_count":
            usage = ((msg.get("info") or {}).get("total_token_usage")) or None
        if usage:
            last = usage
    if last is None:
        return None
    return {"input": last.get("input_tokens"), "cache_write": 0,
            "cache_read": last.get("cached_input_tokens"),
            "output": last.get("output_tokens"),
            "thinking": last.get("reasoning_output_tokens") or last.get("reasoning_tokens")}


def collect_codex(ws: Path, date: str) -> dict:
    by_stage, by_skill = defaultdict(_zero), defaultdict(_zero)
    unparsed = []
    files = sorted((ws / "eval" / date / "usage").glob("*.jsonl"))
    for path in files:
        meta = path.with_suffix(".meta.json")
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        u = codex_usage(path)
        if u is None:
            unparsed.append(path.name)
            continue
        _add(by_stage[info.get("stage", "unknown")], u)
        _add(by_skill[info.get("skill", "unknown")], u)
    return {"files": len(files), "by_stage": dict(by_stage), "by_skill": dict(by_skill),
            "unparsed_codex_files": unparsed}


def run(ws: Path, date: str, projects_dir: Path) -> dict:
    status = {}
    try:
        status = json.loads((ws / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    window = None
    if status.get("run_date") == date and status.get("started_at"):
        window = (str(status["started_at"])[:19], str(status.get("updated_at") or "9999")[:19])
    claude = collect_claude(ws.name, date, projects_dir, window)
    codex = collect_codex(ws, date)
    totals = _zero()
    for part in (claude["by_stage"], codex["by_stage"]):
        for v in part.values():
            for k in FIELDS:
                totals[k] += v[k]
            totals["requests"] += v["requests"]
    result = {
        "workspace": ws.name, "date": date, "method": "api_reported_usage",
        "totals_attributed": totals,
        "claude": claude, "codex": codex,
        "note": ("Token counts as reported by the model APIs. Unattributed usage is "
                 "listed separately, never dropped. Billing in dollars is not computed here."),
    }
    out = ws / "eval" / date / "usage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Actual token usage per run")
    ap.add_argument("workspace", type=Path)
    ap.add_argument("date")
    ap.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude" / "projects")
    args = ap.parse_args()
    if not args.workspace.is_dir():
        print(f"usage_telemetry: no workspace {args.workspace}", file=sys.stderr)
        return 1
    r = run(args.workspace, args.date, args.projects_dir)
    t = r["totals_attributed"]
    print(f"usage_telemetry: {t['requests']} requests; input {t['input']}, cache read "
          f"{t['cache_read']}, cache write {t['cache_write']}, output {t['output']} "
          f"(claude agents {r['claude']['agents']}, codex files {r['codex']['files']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

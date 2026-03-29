#!/usr/bin/env python3
"""Finalize run: assemble final run log entry from stage data and grader results."""

import sys
import json
import os
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import (
    decision_path as _decision_path,
    score_history_path as _score_history_path,
    release_gate_path as _release_gate_path,
    regression_flag_path as _regression_flag_path,
    user_review_path as _user_review_path,
    grader_result_path,
)


def load_json(path: Path):
    """Load JSON file, return None if missing or malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def finalize(workspace: str, date: str) -> dict:
    ws = Path(workspace)
    eval_dir = ws / "eval" / date

    # 1. Read stage timings
    stage_log_path = ws / "eval_stage_log.json"
    stage_log = load_json(stage_log_path)
    if stage_log is None:
        stage_log = []

    # 2. Read review score history
    score_history = load_json(_score_history_path(ws, date))

    # 3. Read final decision
    decision = load_json(_decision_path(ws, date))

    # 4. Read grader results
    factuality = load_json(grader_result_path(ws, date, "factuality"))
    evidence = load_json(grader_result_path(ws, date, "evidence"))
    consistency = load_json(grader_result_path(ws, date, "consistency"))
    cost = load_json(grader_result_path(ws, date, "cost"))

    # 5. Read status.json
    status_path = ws / "status.json"
    status = load_json(status_path)

    # Assemble run log entry
    ticker = (status or {}).get("ticker", os.path.basename(str(ws)))
    run_mode = (status or {}).get("run_mode", "daily")
    started_at = (status or {}).get("started_at")
    completed_at = (status or {}).get("updated_at")

    # Compute total pipeline duration from stage log
    total_duration_s = None
    if stage_log:
        durations = [e.get("duration_s") for e in stage_log if e.get("duration_s") is not None]
        if durations:
            total_duration_s = round(sum(durations), 2)

    # Failed stages
    failed_stages = [
        {"name": e.get("name"), "error": e.get("error", "")}
        for e in stage_log
        if e.get("success") is False
    ]

    # Review info — score_history.json may be a plain list [...] or {"history": [...]}
    review_iterations = 0
    final_scores = None
    if score_history:
        if isinstance(score_history, list):
            history = score_history
        elif isinstance(score_history, dict) and "history" in score_history:
            history = score_history["history"]
        else:
            history = []
        review_iterations = len(history)
        if history:
            last = history[-1]
            final_scores = last.get("scores") or last.get("dimension_scores")

    # Decision info
    decision_label = (decision or {}).get("decision")
    confidence = (decision or {}).get("confidence")

    # Grader summary
    graders = {}
    if factuality is not None:
        graders["factuality"] = {
            "pass": factuality.get("pass"),
            "passed_checks": factuality.get("passed_checks"),
            "total_checks": factuality.get("total_checks"),
        }
    if evidence is not None:
        graders["evidence_coverage"] = {
            "pass": evidence.get("pass"),
            "coverage_ratio": evidence.get("coverage_ratio"),
            "high_materiality_cards": evidence.get("high_materiality_cards"),
            "cited_cards": evidence.get("cited_cards"),
        }
    if consistency is not None:
        graders["consistency"] = {
            "pass": consistency.get("pass"),
            "thesis_lean": consistency.get("thesis_lean"),
            "confidence_calibration": consistency.get("confidence_calibration"),
        }
    if cost is not None:
        graders["cost"] = {
            "estimated_total_tokens": cost.get("estimated_total_tokens"),
            "estimated_cost_usd": cost.get("estimated_cost_usd"),
        }

    all_graders_pass = all(
        g.get("pass", False)
        for g in [factuality, evidence, consistency]
        if g is not None
    )

    # Read release gate result (produced by eval/release_gate.py)
    rg_path = _release_gate_path(ws, date)
    release_gate = load_json(rg_path)
    release_status = release_gate.get("release_status") if release_gate else None

    # Read regression flag if present
    rf_path = _regression_flag_path(ws, date)
    regression_flag = load_json(rf_path)

    # Read user review summary (only audit fields, not raw feedback text)
    ur_raw = load_json(_user_review_path(ws, date))
    user_review_summary = None
    if ur_raw:
        user_review_summary = {
            "reviewed": ur_raw.get("reviewed", False),
            "skipped": ur_raw.get("skipped", True),
            "agrees_with_decision": ur_raw.get("agrees_with_decision"),
            "key_points_count": len(ur_raw.get("key_points", [])),
            "has_personal_context": bool(ur_raw.get("personal_context")),
        }

    entry = {
        "ticker": ticker,
        "run_date": date,
        "run_mode": run_mode,
        "started_at": started_at,
        "completed_at": completed_at,
        "total_duration_s": total_duration_s,
        "stages": stage_log,
        "failed_stages": failed_stages,
        "review_iterations": review_iterations,
        "final_review_scores": final_scores,
        "decision": decision_label,
        "confidence": confidence,
        "graders": graders,
        "all_graders_pass": all_graders_pass,
        "release_status": release_status,
        "regression": regression_flag is not None,
        "regression_details": regression_flag,
        "user_review": user_review_summary,
        "finalized_at": datetime.now(timezone.utc).isoformat(),
    }

    return entry


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <workspace> <date>", file=sys.stderr)
        sys.exit(1)

    workspace = sys.argv[1]
    date = sys.argv[2]

    entry = finalize(workspace, date)

    # Append to run_log.jsonl
    run_log_path = PROJECT_ROOT / "logs" / "run_log.jsonl"
    run_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(run_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Clean up eval_stage_log.json — safe because the orchestrator calls
    # finalize AFTER stage_timer end for reflect, so the log is complete.
    stage_log_path = Path(workspace) / "eval_stage_log.json"
    if stage_log_path.exists():
        try:
            stage_log_path.unlink()
        except OSError:
            pass

    print(json.dumps(entry, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Release gate — deterministic pass/warning/failed assessment of a pipeline run.

Reads grader results from {workspace}/eval/{date}/ and produces release_gate.json.
If factuality or evidence graders fail, writes regression_flag.json (reviewer missed it).

Usage:
    .venv/bin/python3 eval/release_gate.py workspaces/AMD 2026-03-20
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import grader_result_path, release_gate_path, regression_flag_path


def _read_grader(workspace: Path, date: str, name: str) -> dict | None:
    """Read a grader result file, return None if missing."""
    path = grader_result_path(workspace, date, name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def evaluate(workspace: Path, date: str) -> dict:
    """Run release gate logic. Returns the gate result dict."""

    # Gate graders participate in pass/fail logic
    gate_graders = ["factuality", "evidence", "consistency", "valuation", "depth",
                    "decision_risk", "language_purity"]
    # Metric-only graders provide info but don't affect release status
    metric_graders = ["cost"]

    results = {}
    missing = []
    for name in gate_graders + metric_graders:
        r = _read_grader(workspace, date, name)
        if r is not None:
            results[name] = r
        else:
            results[name] = {"pass": None, "error": "grader result not found"}
            if name in gate_graders:
                missing.append(name)

    # Determine pass/fail only for gate graders (cost excluded)
    summary = {}
    for name in gate_graders:
        r = results[name]
        if r.get("pass") is True:
            summary[name] = "pass"
        elif r.get("pass") is False:
            summary[name] = "fail"
        else:
            summary[name] = "unknown"

    # Determine release status
    critical_graders = ["factuality", "evidence"]
    # language_purity is advisory: a mixed-language report is a presentation
    # defect, not a factual regression, so it must not write regression_flag.json.
    advisory_graders = ["consistency", "valuation", "depth", "decision_risk",
                        "language_purity"]

    critical_fail = any(summary.get(g) == "fail" for g in critical_graders)
    advisory_fail = any(summary.get(g) == "fail" for g in advisory_graders)
    # A missing result is not a pass: an absent critical grader means the run
    # was never actually checked, so the gate must fail rather than read "passed".
    critical_missing = [g for g in missing if g in critical_graders]
    advisory_missing = [g for g in missing if g in advisory_graders]

    if critical_fail or critical_missing:
        release_status = "failed"
    elif advisory_fail or advisory_missing:
        release_status = "warning"
    else:
        release_status = "passed"

    gate = {
        "release_status": release_status,
        "missing_graders": missing,
        "grader_summary": summary,  # Only gate graders (factuality, evidence, consistency)
        "grader_details": {
            name: {
                "pass": results[name].get("pass"),
                "key_metric": _extract_key_metric(name, results[name]),
            }
            for name in gate_graders
        },
        "metrics": {
            name: {
                "key_metric": _extract_key_metric(name, results[name]),
            }
            for name in metric_graders
            if name in results
        },
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # Write release_gate.json
    gate_out = release_gate_path(workspace, date)
    gate_out.parent.mkdir(parents=True, exist_ok=True)
    gate_out.write_text(json.dumps(gate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Write regression_flag.json if critical grader failed
    if critical_fail:
        failed = [g for g in critical_graders if summary.get(g) == "fail"]
        descriptions = []
        for g in failed:
            r = results[g]
            if g == "factuality":
                total = r.get("total_checks", "?")
                passed = r.get("passed_checks", "?")
                descriptions.append(f"factuality grader: {passed}/{total} checks passed")
            elif g == "evidence":
                ratio = r.get("coverage_ratio", "?")
                missed = r.get("missed_cards", [])
                descriptions.append(
                    f"evidence grader: coverage {ratio}, {len(missed)} high-materiality cards missed"
                )

        flag = {
            "failed_graders": failed,
            "description": (
                f"Reviewer passed report but {'; '.join(descriptions)}. "
                "This is a regression — the reviewer should have caught this."
            ),
            "severity": "high",
            "timestamp": gate["timestamp"],
        }
        flag_out = regression_flag_path(workspace, date)
        flag_out.parent.mkdir(parents=True, exist_ok=True)
        flag_out.write_text(json.dumps(flag, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        # A previously failed gate may be repaired and re-run. Keep the
        # regression flag aligned with the current gate state.
        flag_out = regression_flag_path(workspace, date)
        if flag_out.exists():
            flag_out.unlink()

    return gate


def _extract_key_metric(name: str, result: dict):
    """Extract the most informative metric from a grader result."""
    if name == "factuality":
        return f"{result.get('passed_checks', '?')}/{result.get('total_checks', '?')} checks"
    if name == "evidence":
        return f"{result.get('coverage_ratio', '?'):.0%} coverage" if isinstance(
            result.get("coverage_ratio"), (int, float)
        ) else str(result.get("coverage_ratio", "?"))
    if name == "consistency":
        return f"decision={result.get('decision', '?')}, lean={result.get('thesis_lean', '?')}"
    if name == "valuation":
        if result.get("applicable") is False:
            return "n/a (non-equity)"
        nwarn = len(result.get("warnings", []))
        return f"{'pass' if result.get('pass') else 'fail'}, conf={result.get('confidence', '?')}, {nwarn} warn"
    if name == "decision_risk":
        llm = result.get("llm_confidence", "?")
        ceiling = result.get("confidence_ceiling", "?")
        return f"conf={llm}/ceiling={ceiling}, {'over' if result.get('exceeded') else 'within'}"
    if name == "language_purity":
        return (f"{result.get('language', '?')}, "
                f"{len(result.get('violations', []))} violation(s)")
    if name == "cost":
        tokens = result.get("estimated_total_tokens", "?")
        cost = result.get("estimated_cost_usd", "?")
        if isinstance(tokens, (int, float)):
            return f"{tokens:,} tokens, ${cost:.2f}"
        return str(tokens)
    return None


def main():
    if len(sys.argv) < 3:
        print("Usage: release_gate.py <workspace> <date>", file=sys.stderr)
        sys.exit(1)

    workspace = Path(sys.argv[1])
    date = sys.argv[2]

    if not workspace.is_dir():
        print(f"Error: workspace '{workspace}' is not a directory", file=sys.stderr)
        sys.exit(1)

    gate = evaluate(workspace, date)
    print(json.dumps(gate, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

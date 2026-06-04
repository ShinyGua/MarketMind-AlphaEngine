#!/usr/bin/env python3
"""Consistency grader: verify final decision aligns with thesis_map consensus."""

import sys
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_json(path: Path):
    """Load JSON file, return None if missing or malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def determine_thesis_lean(thesis: dict) -> str:
    """Determine the thesis's overall directional lean.

    Prefers the moderator's authoritative ``net_directional_lean`` field when
    present; otherwise falls back to counting ``evidence_balance`` across
    disagreements. Returns one of "bull", "bear", "mixed".
    """
    net_lean = str(thesis.get("net_directional_lean", "")).lower().strip()
    if net_lean in ("bullish", "bull"):
        return "bull"
    if net_lean in ("bearish", "bear"):
        return "bear"
    if net_lean in ("neutral", "mixed", "even"):
        return "mixed"

    # Fallback: count bull vs bear evidence_balance across disagreements.
    disagreements = thesis.get("disagreements", [])
    bull_count = 0
    bear_count = 0
    for d in disagreements:
        balance = d.get("evidence_balance", "").lower()
        if balance == "bull":
            bull_count += 1
        elif balance == "bear":
            bear_count += 1
        # "even" counts for neither

    if bull_count > bear_count:
        return "bull"
    elif bear_count > bull_count:
        return "bear"
    else:
        return "mixed"


def check_decision_alignment(decision_label: str, thesis_lean: str) -> tuple[bool, str]:
    """Check if the decision is consistent with the thesis lean.

    Returns (is_consistent, explanation).
    """
    decision_label = decision_label.upper()

    if decision_label == "HOLD":
        # HOLD never hard-fails (the grader cannot judge lean *strength*
        # deterministically — that is the reviewer's job), but a HOLD that
        # diverges from an explicit directional lean is surfaced as a note so
        # it is not silently rubber-stamped.
        if thesis_lean in ("bull", "bear"):
            return True, f"NOTE: HOLD diverges from {thesis_lean} thesis lean — confirm it is not bias-driven HOLD-defaulting"
        return True, "HOLD is consistent with balanced/mixed evidence lean"

    if thesis_lean == "mixed":
        # Mixed evidence is consistent with any decision
        return True, f"{decision_label} is consistent with mixed evidence"

    if thesis_lean == "bull" and decision_label == "SELL":
        return False, f"SELL contradicts majority bull lean in thesis_map"

    if thesis_lean == "bear" and decision_label == "BUY":
        return False, f"BUY contradicts majority bear lean in thesis_map"

    return True, f"{decision_label} aligns with {thesis_lean} lean"


def check_horizon_match(decision_horizon: str, thesis_horizon: str) -> bool:
    """Check if decision horizon is compatible with thesis dominant_time_horizon."""
    if not decision_horizon or not thesis_horizon:
        return True  # Can't compare if missing

    # Normalize
    dh = decision_horizon.lower().strip()
    th = thesis_horizon.lower().strip()

    # Direct match
    if dh == th:
        return True

    # Compatible pairs: "1m" matches "swing", "3m" matches "swing" or "medium_term"
    horizon_groups = {
        "daily": {"daily", "1d"},
        "swing": {"swing", "1m", "1-3m", "1-3 months"},
        "medium_term": {"medium_term", "3m", "3-6m", "3-6 months"},
        "long_term": {"long_term", "6m", "12m", "1y"},
    }

    for group_name, members in horizon_groups.items():
        if dh in members and th in members:
            return True

    return False


def check_confidence_calibration(
    confidence: float,
    debate_quality_score: float | None,
) -> str:
    """Check if confidence is appropriately calibrated given debate quality."""
    if debate_quality_score is None:
        return "unknown"

    if debate_quality_score < 6 and confidence > 0.8:
        return "over_confident"
    if debate_quality_score >= 8 and confidence < 0.4:
        return "under_confident"
    return "appropriate"


def grade(workspace: str, date: str) -> dict:
    ws = Path(workspace)
    decision_path = ws / "decision" / date / "final_decision.json"
    thesis_path = ws / "discussion" / date / "thesis_map.json"

    result = {
        "grader": "consistency",
        "pass": False,
        "decision": None,
        "thesis_lean": None,
        "horizon_match": None,
        "confidence_calibration": None,
        "details": [],
        "errors": [],
    }

    decision = load_json(decision_path)
    if decision is None:
        result["errors"].append(f"Final decision not found or invalid: {decision_path}")

    thesis = load_json(thesis_path)
    if thesis is None:
        result["errors"].append(f"Thesis map not found or invalid: {thesis_path}")

    if decision is None or thesis is None:
        return result

    decision_label = decision.get("decision", "")
    confidence = decision.get("confidence", 0.5)
    decision_horizon = decision.get("horizon", "")

    result["decision"] = decision_label

    # Determine thesis lean (prefers net_directional_lean, falls back to disagreements)
    thesis_lean = determine_thesis_lean(thesis)
    result["thesis_lean"] = thesis_lean

    # Check decision-thesis alignment
    is_aligned, alignment_detail = check_decision_alignment(decision_label, thesis_lean)
    result["details"].append(alignment_detail)

    # Check horizon match
    thesis_horizon = thesis.get("dominant_time_horizon", "")
    horizon_ok = check_horizon_match(decision_horizon, thesis_horizon)
    result["horizon_match"] = horizon_ok
    if not horizon_ok:
        result["details"].append(
            f"Horizon mismatch: decision='{decision_horizon}', thesis='{thesis_horizon}'"
        )
    else:
        result["details"].append(
            f"Horizon compatible: decision='{decision_horizon}', thesis='{thesis_horizon}'"
        )

    # Check confidence calibration
    debate_quality = thesis.get("debate_quality_score")
    calibration = check_confidence_calibration(confidence, debate_quality)
    result["confidence_calibration"] = calibration
    result["details"].append(
        f"Confidence={confidence}, debate_quality={debate_quality}, calibration={calibration}"
    )

    # Panel alignment (note only — never hard-fails). If the multi-round decision
    # panel ran, surface a final label that diverges from the panel's vote tally so
    # a conviction-weighted override is explicit rather than silent.
    panel = decision.get("panel")
    if isinstance(panel, dict):
        tally = panel.get("final_tally") or {}
        if isinstance(tally, dict) and any(isinstance(v, (int, float)) for v in tally.values()):
            panel_majority = max(tally, key=lambda k: tally.get(k, 0))
            result["panel_majority"] = panel_majority
            result["panel_exit_reason"] = panel.get("exit_reason")
            if panel_majority and decision_label.upper() != str(panel_majority).upper():
                result["details"].append(
                    f"NOTE: decision {decision_label} diverges from panel vote majority "
                    f"{panel_majority} — confirm it is a conviction-weighted override, not an error"
                )
            else:
                result["details"].append(
                    f"Decision aligns with panel vote majority ({panel_majority})"
                )

    # Overall pass: alignment must be consistent, and no over-confidence flag
    all_ok = is_aligned and calibration != "over_confident"
    result["pass"] = all_ok

    if not all_ok:
        if not is_aligned:
            result["details"].append("FAIL: decision contradicts thesis_map evidence lean")
        if calibration == "over_confident":
            result["details"].append(
                "FAIL: confidence too high given low debate quality score"
            )

    return result


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <workspace> <date>", file=sys.stderr)
        sys.exit(1)

    workspace = sys.argv[1]
    date = sys.argv[2]

    result = grade(workspace, date)

    # Write output
    ws = Path(workspace)
    out_dir = ws / "eval" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "consistency_result.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

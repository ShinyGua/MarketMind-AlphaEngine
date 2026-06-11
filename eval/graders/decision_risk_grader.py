#!/usr/bin/env python3
"""Decision risk gate — deterministic, advisory confidence ceiling for the final call.

The LLM chair writes ``confidence`` in ``final_decision.json``. This grader does
NOT trust that number blindly: it computes a *ceiling* from reproducible risk
signals (weak panel convergence, retained dissent, low-confidence valuation used
as a reason, thin evidence, a stalled panel with unresolved dissent, and round-1
near-unanimity — instant agreement is a conformity signal, not a strong one) and
flags when the stated confidence exceeds it.

It is **advisory and non-mutating** — it never rewrites ``final_decision.json``.
An over-ceiling result makes the release gate a *warning*, never *failed*. This
preserves the decision artifact while making over-confidence visible and gated.

Usage:
    .venv/bin/python3 eval/graders/decision_risk_grader.py <workspace> <date>
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import (  # noqa: E402
    decision_path,
    valuation_summary_path,
    evidence_digest_path,
    panel_convergence_path,
    grader_result_path,
)

# Deterministic ceilings each risk signal imposes.
_WEAK_CONVERGENCE_CAP = 0.65
_DISSENT_BASE_CAP = 0.80
_DISSENT_PER = 0.05
_DISSENT_FLOOR = 0.50
_LOW_VAL_REASON_CAP = 0.70
_THIN_EVIDENCE_CAP = 0.70
_STALLED_DISSENT_CAP = 0.65    # panel exited stalled / with unresolved dissent
_FAST_UNANIMITY_CAP = 0.85     # round-1 near-unanimity is a conformity risk signal
_FAST_UNANIMITY_SCORE = 0.95

# Defaults mirror the canonical sources (decision.panel / review.depth).
_DEFAULT_CONVERGENCE_THRESHOLD = 0.70
_DEFAULT_MIN_EVIDENCE_CARDS = 12

# Substrings that mean "valuation is carrying this decision" (case-insensitive).
_VAL_KEYWORDS = ("valuation", "fair value", "margin of safety", "dcf", "comps", "price target")

# Intraday timing-only contract: 1h/4h TA may frame entry/exit zones but must
# never appear as a vote reason. A timeframe keyword counts only when an
# indicator keyword sits within this many characters (cuts "hourly news cadence"
# style false positives). Boundary guards reject "24h"/"24小时" and alphanumeric
# run-ons. Violations are WARNING-ONLY — they never cap or fail.
import re as _re

_INTRADAY_TF_RE = _re.compile(
    r"(?<![0-9a-z])(?:[14]hr?|intraday|hourly|four-hour|one-hour)(?![0-9a-z])"
    r"|(?<![0-9])[14]小时"
    r"|小时线")
_INTRADAY_IND = ("rsi", "macd", "overbought", "oversold", "超买", "超卖")
_INTRADAY_PROXIMITY = 40


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _load_config(ws: Path) -> dict:
    cfg = load_json(ws / "resolved_config.json")
    return cfg if isinstance(cfg, dict) else {}


def _convergence_threshold(cfg: dict) -> float:
    panel = (cfg.get("decision", {}) or {}).get("panel", {}) or {}
    v = panel.get("convergence_threshold")
    return float(v) if isinstance(v, (int, float)) else _DEFAULT_CONVERGENCE_THRESHOLD


def _min_evidence_cards(cfg: dict) -> int:
    depth = (cfg.get("review", {}) or {}).get("depth", {}) or {}
    v = depth.get("min_evidence_cards")
    return int(v) if isinstance(v, (int, float)) else _DEFAULT_MIN_EVIDENCE_CARDS


def _evidence_count(ws: Path, date: str) -> int | None:
    """Number of evidence cards — same source the depth grader uses."""
    digest = evidence_digest_path(ws, date)
    if digest.exists():
        try:
            return len(json.loads(digest.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, OSError):
            pass
    cards_dir = ws / "normalized" / date / "evidence_cards"
    if cards_dir.is_dir():
        return len(list(cards_dir.glob("*.json")))
    return None


def _final_convergence(ws: Path, date: str, panel: dict) -> dict | None:
    """The last round's convergence grader output, or None."""
    rounds = panel.get("rounds_run") if isinstance(panel, dict) else None
    if isinstance(rounds, int) and rounds > 0:
        conv = load_json(panel_convergence_path(ws, date, rounds))
        if isinstance(conv, dict):
            return conv
    return None


def _convergence_score(panel: dict, conv: dict | None) -> float | None:
    """Prefer the last round's convergence file; fall back to the panel summary."""
    if isinstance(conv, dict) and isinstance(conv.get("convergence_score"), (int, float)):
        return float(conv["convergence_score"])
    if isinstance(panel, dict) and isinstance(panel.get("convergence_score"), (int, float)):
        return float(panel["convergence_score"])
    return None


def _valuation_cited(decision: dict) -> bool:
    """True if valuation is invoked anywhere in the rationale text.

    Scans top_reasons + decision_summary + stance_notes so the cap can't be
    dodged by relocating the valuation argument out of top_reasons.
    """
    parts = []
    reasons = decision.get("top_reasons")
    if isinstance(reasons, list):
        parts.extend(str(r) for r in reasons)
    for key in ("decision_summary", "stance_notes"):
        val = decision.get(key)
        if isinstance(val, str):
            parts.append(val)
    blob = " ".join(parts).lower()
    return any(kw in blob for kw in _VAL_KEYWORDS)


def _cites_intraday_as_reason(text: str) -> bool:
    """True when an intraday timeframe keyword sits near an indicator keyword."""
    blob = text.lower()
    for m in _INTRADAY_TF_RE.finditer(blob):
        lo = max(0, m.start() - _INTRADAY_PROXIMITY)
        window = blob[lo:m.end() + _INTRADAY_PROXIMITY]
        if any(ind in window for ind in _INTRADAY_IND):
            return True
    return False


def _intraday_cited_as_reason(ws: Path, date: str, decision: dict) -> list[str]:
    """Scan ballot rationales and final top_reasons for intraday-indicator vote
    reasons (timing-only contract). Returns warning strings; never caps."""
    warnings = []
    panel_root = ws / "decision" / date / "panel"
    for ballot_path in sorted(panel_root.glob("round_*/*_ballot.json")):
        ballot = load_json(ballot_path) or {}
        blob = " ".join(str(ballot.get(k) or "")
                        for k in ("rationale", "top_risk", "responds_to_dissent"))
        if _cites_intraday_as_reason(blob):
            role = ballot_path.stem.replace("_ballot", "")
            rnd = ballot_path.parent.name.replace("round_", "")
            warnings.append(
                f"ballot {role} round {rnd} cites intraday indicators as a vote "
                f"reason (timing-only contract)")
    reasons = decision.get("top_reasons")
    if isinstance(reasons, list) and _cites_intraday_as_reason(" ".join(map(str, reasons))):
        warnings.append(
            "final_decision.top_reasons cites intraday indicators as a vote reason "
            "(timing-only contract)")
    return warnings


def grade(workspace: str, date: str) -> dict:
    ws = Path(workspace)
    result = {
        "grader": "decision_risk",
        "pass": None,
        "llm_confidence": None,
        "confidence_ceiling": 1.0,
        "exceeded": False,
        "cap_reasons": [],
        "warnings": [],  # advisory-only: never feeds the ceiling or pass
        "inputs": {},
        "errors": [],
    }

    decision = load_json(decision_path(ws, date))
    if decision is None:
        result["errors"].append(f"Final decision not found or invalid: {decision_path(ws, date)}")
        return result

    cfg = _load_config(ws)
    valuation = load_json(valuation_summary_path(ws, date)) or {}
    panel = decision.get("panel") if isinstance(decision.get("panel"), dict) else {}

    ceiling = 1.0
    reasons = []

    # 1. Weak panel convergence.
    conv_threshold = _convergence_threshold(cfg)
    conv = _final_convergence(ws, date, panel)
    conv_score = _convergence_score(panel, conv)
    result["inputs"]["convergence_score"] = conv_score
    result["inputs"]["convergence_threshold"] = conv_threshold
    if conv_score is not None and conv_score < conv_threshold:
        ceiling = min(ceiling, _WEAK_CONVERGENCE_CAP)
        reasons.append(
            f"panel convergence {conv_score:.2f} < threshold {conv_threshold:.2f} "
            f"-> ceiling {_WEAK_CONVERGENCE_CAP}")

    # 2. Retained dissent.
    dissent = panel.get("retained_dissent")
    dissent_count = len(dissent) if isinstance(dissent, list) else 0
    result["inputs"]["dissent_count"] = dissent_count
    if dissent_count > 0:
        adjusted = max(_DISSENT_FLOOR, _DISSENT_BASE_CAP - _DISSENT_PER * dissent_count)
        ceiling = min(ceiling, adjusted)
        reasons.append(
            f"{dissent_count} retained dissent(s) -> ceiling {adjusted:.2f}")

    # 3. Low valuation confidence used as a reason.
    val_conf = valuation.get("confidence")
    result["inputs"]["valuation_confidence"] = val_conf
    if val_conf == "low" and _valuation_cited(decision):
        ceiling = min(ceiling, _LOW_VAL_REASON_CAP)
        reasons.append(
            f"valuation confidence low and cited as a reason -> ceiling {_LOW_VAL_REASON_CAP}")

    # 4. Thin evidence.
    floor = _min_evidence_cards(cfg)
    ev_count = _evidence_count(ws, date)
    result["inputs"]["evidence_cards"] = ev_count
    result["inputs"]["min_evidence_cards"] = floor
    if ev_count is not None and ev_count < floor:
        ceiling = min(ceiling, _THIN_EVIDENCE_CAP)
        reasons.append(
            f"evidence cards {ev_count} < floor {floor} -> ceiling {_THIN_EVIDENCE_CAP}")

    # 5. Panel exited stalled / with unresolved dissent.
    rounds_run = panel.get("rounds_run") if isinstance(panel.get("rounds_run"), int) else None
    exit_reason = (conv or {}).get("exit_reason") or panel.get("exit_reason")
    unresolved = bool((conv or {}).get("unresolved_dissent")) or exit_reason == "stalled"
    result["inputs"]["rounds_run"] = rounds_run
    result["inputs"]["unresolved_dissent"] = unresolved
    if unresolved:
        ceiling = min(ceiling, _STALLED_DISSENT_CAP)
        reasons.append(
            f"panel stalled with unresolved dissent -> ceiling {_STALLED_DISSENT_CAP}")

    # 6. Fast unanimity: instant round-1 near-unanimous agreement is a
    # conformity risk signal (arXiv:2509.05396), not a confidence booster.
    if (rounds_run == 1 and conv_score is not None
            and conv_score >= _FAST_UNANIMITY_SCORE):
        ceiling = min(ceiling, _FAST_UNANIMITY_CAP)
        reasons.append(
            f"round-1 unanimity (convergence {conv_score:.2f}) "
            f"-> ceiling {_FAST_UNANIMITY_CAP}")

    # 7. Intraday timing-only contract (warning-only — no cap, no fail).
    result["warnings"].extend(_intraday_cited_as_reason(ws, date, decision))

    llm_conf = decision.get("confidence")
    result["llm_confidence"] = llm_conf
    result["confidence_ceiling"] = round(ceiling, 2)
    result["cap_reasons"] = reasons

    if isinstance(llm_conf, (int, float)):
        exceeded = llm_conf > ceiling + 1e-9
        result["exceeded"] = exceeded
        result["pass"] = not exceeded
    else:
        result["errors"].append("decision.confidence missing or non-numeric; cannot assess")
        # pass stays None -> release gate treats as 'unknown', not a fail

    return result


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <workspace> <date>", file=sys.stderr)
        sys.exit(1)

    workspace, date = sys.argv[1], sys.argv[2]
    result = grade(workspace, date)

    out_path = grader_result_path(Path(workspace), date, "decision_risk")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

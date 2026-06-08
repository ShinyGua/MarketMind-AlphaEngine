"""Unit tests for the deterministic decision risk gate (advisory confidence ceiling).

Each risk signal is exercised in isolation on a synthetic workspace — no LLM, no
pipeline run, no live-artifact numbers. Validates the ceiling math, the broadened
valuation-reason scan, the config-driven evidence floor, and the non-mutating
advisory contract (pass=None when confidence is missing).
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "decision_risk_grader", ROOT / "eval" / "graders" / "decision_risk_grader.py")
grader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grader)

DATE = "2026-06-05"


def _ws(tmp_path, *, decision, valuation=None, evidence_count=20,
        config=None, convergence_file=None):
    """Build a minimal workspace and return its path."""
    ws = tmp_path / "WS"
    (ws).mkdir(parents=True, exist_ok=True)

    cfg = config if config is not None else {}
    (ws / "resolved_config.json").write_text(json.dumps(cfg), encoding="utf-8")

    dec_dir = ws / "decision" / DATE
    dec_dir.mkdir(parents=True, exist_ok=True)
    (dec_dir / "final_decision.json").write_text(json.dumps(decision), encoding="utf-8")

    if valuation is not None:
        val_dir = ws / "valuation" / DATE
        val_dir.mkdir(parents=True, exist_ok=True)
        (val_dir / "valuation_summary.json").write_text(json.dumps(valuation), encoding="utf-8")

    if evidence_count is not None:
        norm_dir = ws / "normalized" / DATE
        norm_dir.mkdir(parents=True, exist_ok=True)
        cards = [{"id": f"ev_{i}"} for i in range(evidence_count)]
        (norm_dir / "evidence_digest.json").write_text(json.dumps(cards), encoding="utf-8")

    if convergence_file is not None:
        rnd, score = convergence_file
        panel_dir = dec_dir / "panel"
        panel_dir.mkdir(parents=True, exist_ok=True)
        (panel_dir / f"convergence_round_{rnd}.json").write_text(
            json.dumps({"convergence_score": score}), encoding="utf-8")

    return ws


def _clean_decision(confidence=0.70):
    """A decision that trips no risk signal."""
    return {
        "decision": "HOLD",
        "confidence": confidence,
        "top_reasons": ["Strong relative strength", "Catalyst calendar intact"],
        "decision_summary": "Balanced setup; staged hold.",
        "stance_notes": "",
        "panel": {"rounds_run": 1, "convergence_score": 0.85, "retained_dissent": []},
    }


def test_clean_decision_no_cap(tmp_path):
    ws = _ws(tmp_path, decision=_clean_decision(0.70),
             valuation={"confidence": "high"}, evidence_count=20)
    r = grader.grade(str(ws), DATE)
    assert r["confidence_ceiling"] == 1.0
    assert r["cap_reasons"] == []
    assert r["exceeded"] is False
    assert r["pass"] is True


def test_weak_convergence_caps_via_summary_fallback(tmp_path):
    dec = _clean_decision(0.80)
    dec["panel"]["convergence_score"] = 0.50          # < default 0.70 threshold
    ws = _ws(tmp_path, decision=dec, valuation={"confidence": "high"})
    r = grader.grade(str(ws), DATE)
    assert r["confidence_ceiling"] == 0.65
    assert r["exceeded"] is True                       # 0.80 > 0.65
    assert r["pass"] is False
    assert any("convergence" in s for s in r["cap_reasons"])


def test_weak_convergence_prefers_convergence_file(tmp_path):
    dec = _clean_decision(0.60)
    dec["panel"]["convergence_score"] = 0.95           # stale summary value...
    ws = _ws(tmp_path, decision=dec, valuation={"confidence": "high"},
             convergence_file=(1, 0.40))               # ...authoritative file says 0.40
    r = grader.grade(str(ws), DATE)
    assert r["inputs"]["convergence_score"] == 0.40
    assert r["confidence_ceiling"] == 0.65
    assert r["pass"] is True                            # 0.60 <= 0.65


def test_retained_dissent_two_caps_to_070(tmp_path):
    dec = _clean_decision(0.75)
    dec["panel"]["retained_dissent"] = ["risk_analyst SELL", "valuation_analyst SELL"]
    ws = _ws(tmp_path, decision=dec, valuation={"confidence": "high"})
    r = grader.grade(str(ws), DATE)
    # 0.80 - 0.05*2 = 0.70
    assert r["confidence_ceiling"] == 0.70
    assert r["inputs"]["dissent_count"] == 2
    assert r["exceeded"] is True


def test_dissent_floor_never_below_050(tmp_path):
    dec = _clean_decision(0.90)
    dec["panel"]["retained_dissent"] = [f"role_{i} SELL" for i in range(10)]
    ws = _ws(tmp_path, decision=dec, valuation={"confidence": "high"})
    r = grader.grade(str(ws), DATE)
    assert r["confidence_ceiling"] == 0.50             # max(0.50, 0.80-0.50)


def test_low_valuation_cited_in_summary_not_reasons(tmp_path):
    # valuation argument lives in decision_summary, NOT top_reasons — must still cap
    dec = _clean_decision(0.85)
    dec["top_reasons"] = ["Momentum fading", "Concentration risk"]
    dec["decision_summary"] = "Price sits well above fair value (margin of safety negative)."
    ws = _ws(tmp_path, decision=dec, valuation={"confidence": "low"})
    r = grader.grade(str(ws), DATE)
    assert r["confidence_ceiling"] == 0.70
    assert any("valuation" in s for s in r["cap_reasons"])


def test_low_valuation_not_cited_no_cap(tmp_path):
    dec = _clean_decision(0.85)
    dec["top_reasons"] = ["Momentum fading", "Concentration risk"]
    dec["decision_summary"] = "Crowded ownership and slowing growth."
    ws = _ws(tmp_path, decision=dec, valuation={"confidence": "low"})
    r = grader.grade(str(ws), DATE)
    assert r["confidence_ceiling"] == 1.0              # low val confidence, but not invoked
    assert r["cap_reasons"] == []


def test_thin_evidence_caps(tmp_path):
    ws = _ws(tmp_path, decision=_clean_decision(0.80),
             valuation={"confidence": "high"}, evidence_count=5)
    r = grader.grade(str(ws), DATE)
    assert r["confidence_ceiling"] == 0.70
    assert r["inputs"]["evidence_cards"] == 5


def test_evidence_exactly_floor_no_cap(tmp_path):
    ws = _ws(tmp_path, decision=_clean_decision(0.80),
             valuation={"confidence": "high"}, evidence_count=12)
    r = grader.grade(str(ws), DATE)
    assert r["confidence_ceiling"] == 1.0              # 12 >= floor 12


def test_config_override_evidence_floor(tmp_path):
    cfg = {"review": {"depth": {"min_evidence_cards": 30}}}
    ws = _ws(tmp_path, decision=_clean_decision(0.80),
             valuation={"confidence": "high"}, evidence_count=20, config=cfg)
    r = grader.grade(str(ws), DATE)
    assert r["inputs"]["min_evidence_cards"] == 30
    assert r["confidence_ceiling"] == 0.70             # 20 < 30


def test_missing_panel_block_skips_convergence_and_dissent(tmp_path):
    dec = {"decision": "BUY", "confidence": 0.70,
           "top_reasons": ["x"], "decision_summary": "y"}   # no panel key
    ws = _ws(tmp_path, decision=dec, valuation={"confidence": "high"})
    r = grader.grade(str(ws), DATE)
    assert r["inputs"]["convergence_score"] is None
    assert r["inputs"]["dissent_count"] == 0
    assert r["confidence_ceiling"] == 1.0


def test_missing_confidence_is_unknown_not_fail(tmp_path):
    dec = _clean_decision()
    dec.pop("confidence")
    ws = _ws(tmp_path, decision=dec, valuation={"confidence": "high"})
    r = grader.grade(str(ws), DATE)
    assert r["pass"] is None                           # release gate treats as 'unknown'
    assert r["errors"]


def test_lowest_cap_wins_when_multiple_signals(tmp_path):
    dec = _clean_decision(0.90)
    dec["panel"]["convergence_score"] = 0.40           # caps 0.65
    dec["decision_summary"] = "DCF says expensive."    # caps 0.70 (val low)
    ws = _ws(tmp_path, decision=dec, valuation={"confidence": "low"}, evidence_count=5)
    r = grader.grade(str(ws), DATE)
    assert r["confidence_ceiling"] == 0.65             # min of 0.65/0.70/0.70
    assert len(r["cap_reasons"]) == 3


def test_missing_decision_returns_unknown(tmp_path):
    ws = tmp_path / "WS"
    ws.mkdir()
    r = grader.grade(str(ws), DATE)
    assert r["pass"] is None
    assert r["errors"]

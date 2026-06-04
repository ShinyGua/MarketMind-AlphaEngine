"""Unit tests for the deterministic decision-panel convergence grader.

Validates the exit logic (converge early, hard cap at max_rounds, min_rounds
floor, insufficient-ballots self-degrade) and the convergence-score math —
without any LLM or pipeline run.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "panel_convergence_grader", ROOT / "eval" / "graders" / "panel_convergence_grader.py")
grader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grader)

DATE = "2026-06-04"


def _ws(tmp_path: Path, panel: dict | None = None) -> Path:
    ws = tmp_path / "WS"
    (ws).mkdir(parents=True, exist_ok=True)
    cfg = {"decision": {"panel": panel}} if panel else {}
    (ws / "resolved_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return ws


def _ballot(ws: Path, rnd: int, role: str, vote: str, conviction: float, overlay="none"):
    d = ws / "decision" / DATE / "panel" / f"round_{rnd}"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{role}_ballot.json").write_text(json.dumps({
        "role": role, "round": rnd, "vote": vote,
        "conviction": conviction, "risk_overlay": overlay,
        "rationale": "x", "top_risk": "y",
    }), encoding="utf-8")


def test_unanimous_high_conviction_exits_converged(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    for role in ("company_analyst", "risk_analyst", "market_analyst"):
        _ballot(ws, 1, role, "SELL", 0.9)
    r = grader.grade(str(ws), DATE, 1)
    assert r["majority_vote"] == "SELL"
    assert r["conviction_weighted_agreement"] == 1.0
    assert r["convergence_score"] == 1.0
    assert r["exit"] is True and r["exit_reason"] == "converged"
    assert r["dissenters"] == []


def test_split_low_conviction_does_not_exit_before_cap(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    _ballot(ws, 1, "company_analyst", "BUY", 0.5)
    _ballot(ws, 1, "risk_analyst", "SELL", 0.5)
    _ballot(ws, 1, "market_analyst", "HOLD", 0.4)
    r = grader.grade(str(ws), DATE, 1)
    assert r["exit"] is False and r["exit_reason"] == "not_converged"
    assert r["convergence_score"] < 0.70
    assert len(r["dissenters"]) == 2  # two roles disagree with the majority pick


def test_min_rounds_floor_blocks_early_exit(tmp_path):
    # Unanimous at round 1 but min_rounds=2 → must still run another round.
    ws = _ws(tmp_path, {"min_rounds": 2, "max_rounds": 3, "convergence_threshold": 0.70})
    for role in ("a", "b", "c"):
        _ballot(ws, 1, role, "BUY", 0.9)
    r = grader.grade(str(ws), DATE, 1)
    assert r["convergence_score"] == 1.0
    assert r["exit"] is False and r["exit_reason"] == "min_rounds_not_met"


def test_max_rounds_forces_exit_even_when_split(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 2, "convergence_threshold": 0.99})
    _ballot(ws, 2, "company_analyst", "BUY", 0.5)
    _ballot(ws, 2, "risk_analyst", "SELL", 0.5)
    r = grader.grade(str(ws), DATE, 2)
    assert r["exit"] is True and r["exit_reason"] == "max_rounds"


def test_single_ballot_is_insufficient(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    _ballot(ws, 1, "company_analyst", "BUY", 0.9)
    r = grader.grade(str(ws), DATE, 1)
    assert r["exit"] is True and r["exit_reason"] == "insufficient_ballots"
    assert r["convergence_score"] is None


def test_vote_stability_blends_into_score(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    # Round 1: 2 BUY / 1 SELL
    _ballot(ws, 1, "a", "BUY", 0.8)
    _ballot(ws, 1, "b", "BUY", 0.8)
    _ballot(ws, 1, "c", "SELL", 0.8)
    # Round 2: c flips to BUY → unanimous, full stability
    _ballot(ws, 2, "a", "BUY", 0.8)
    _ballot(ws, 2, "b", "BUY", 0.8)
    _ballot(ws, 2, "c", "BUY", 0.8)
    r2 = grader.grade(str(ws), DATE, 2)
    # a,b unchanged (BUY→BUY); c changed (SELL→BUY) → stability 2/3
    assert r2["vote_stability"] == round(2 / 3, 4)
    assert r2["conviction_weighted_agreement"] == 1.0
    # score = 0.6*1.0 + 0.4*0.6667
    assert abs(r2["convergence_score"] - (0.6 + 0.4 * (2 / 3))) < 1e-3
    assert r2["exit"] is True and r2["exit_reason"] == "converged"


def test_score_monotonic_in_agreement(tmp_path):
    def score(votes):
        ws = _ws(tmp_path / json.dumps(votes), {"min_rounds": 1, "max_rounds": 3,
                                                 "convergence_threshold": 0.70})
        for i, v in enumerate(votes):
            _ballot(ws, 1, f"r{i}", v, 0.8)
        return grader.grade(str(ws), DATE, 1)["convergence_score"]

    s_split = score(["BUY", "SELL", "HOLD"])
    s_majority = score(["BUY", "BUY", "SELL"])
    s_unanimous = score(["BUY", "BUY", "BUY"])
    assert s_split < s_majority < s_unanimous

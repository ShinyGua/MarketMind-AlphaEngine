"""Unit tests for the deterministic discussion-panel convergence grader.

Validates the exit logic (converge early, hard cap at max_rounds, min_rounds
floor, insufficient-views self-degrade) and the convergence-score math —
without any LLM or pipeline run. Mirrors test_panel_convergence_grader.py but
with bullish|neutral|bearish stances instead of BUY/HOLD/SELL votes.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "discussion_convergence_grader",
    ROOT / "eval" / "graders" / "discussion_convergence_grader.py")
grader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grader)

DATE = "2026-06-09"


def _ws(tmp_path: Path, panel: dict | None = None) -> Path:
    ws = tmp_path / "WS"
    ws.mkdir(parents=True, exist_ok=True)
    cfg = {"discussion": {"panel": panel}} if panel else {}
    (ws / "resolved_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return ws


def _view(ws: Path, rnd: int, role: str, stance: str, conviction: float):
    d = ws / "discussion" / DATE / "panel" / f"round_{rnd}"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{role}_view.json").write_text(json.dumps({
        "role": role, "round": rnd, "stance": stance,
        "conviction": conviction, "core_claims": ["x"],
    }), encoding="utf-8")


def test_unanimous_high_conviction_exits_converged(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    for role in ("company_analyst", "risk_analyst", "market_analyst"):
        _view(ws, 1, role, "bearish", 0.9)
    r = grader.grade(str(ws), DATE, 1)
    assert r["majority_stance"] == "bearish"
    assert r["conviction_weighted_agreement"] == 1.0
    assert r["convergence_score"] == 1.0
    assert r["exit"] is True and r["exit_reason"] == "converged"
    assert r["dissenters"] == []


def test_split_low_conviction_does_not_exit_before_cap(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    _view(ws, 1, "company_analyst", "bullish", 0.5)
    _view(ws, 1, "risk_analyst", "bearish", 0.5)
    _view(ws, 1, "market_analyst", "neutral", 0.4)
    r = grader.grade(str(ws), DATE, 1)
    assert r["exit"] is False and r["exit_reason"] == "not_converged"
    assert r["convergence_score"] < 0.70
    assert len(r["dissenters"]) == 2  # two roles disagree with the majority stance


def test_min_rounds_floor_blocks_early_exit(tmp_path):
    # Unanimous at round 1 but min_rounds=2 → must still run another round.
    ws = _ws(tmp_path, {"min_rounds": 2, "max_rounds": 3, "convergence_threshold": 0.70})
    for role in ("a", "b", "c"):
        _view(ws, 1, role, "bullish", 0.9)
    r = grader.grade(str(ws), DATE, 1)
    assert r["convergence_score"] == 1.0
    assert r["exit"] is False and r["exit_reason"] == "min_rounds_not_met"


def test_max_rounds_forces_exit_even_when_split(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 2, "convergence_threshold": 0.99})
    _view(ws, 2, "company_analyst", "bullish", 0.5)
    _view(ws, 2, "risk_analyst", "bearish", 0.5)
    r = grader.grade(str(ws), DATE, 2)
    assert r["exit"] is True and r["exit_reason"] == "max_rounds"


def test_single_view_is_insufficient(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    _view(ws, 1, "company_analyst", "bullish", 0.9)
    r = grader.grade(str(ws), DATE, 1)
    assert r["exit"] is True and r["exit_reason"] == "insufficient_views"
    assert r["convergence_score"] is None


def test_stance_stability_blends_into_score(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    # Round 1: 2 bullish / 1 bearish
    _view(ws, 1, "a", "bullish", 0.8)
    _view(ws, 1, "b", "bullish", 0.8)
    _view(ws, 1, "c", "bearish", 0.8)
    # Round 2: c flips to bullish → unanimous, full agreement
    _view(ws, 2, "a", "bullish", 0.8)
    _view(ws, 2, "b", "bullish", 0.8)
    _view(ws, 2, "c", "bullish", 0.8)
    r2 = grader.grade(str(ws), DATE, 2)
    # a,b unchanged (bullish→bullish); c changed (bearish→bullish) → stability 2/3
    assert r2["stance_stability"] == round(2 / 3, 4)
    assert r2["conviction_weighted_agreement"] == 1.0
    # score = 0.6*1.0 + 0.4*0.6667
    assert abs(r2["convergence_score"] - (0.6 + 0.4 * (2 / 3))) < 1e-3
    assert r2["exit"] is True and r2["exit_reason"] == "converged"


def test_score_monotonic_in_agreement(tmp_path):
    def score(stances):
        ws = _ws(tmp_path / json.dumps(stances), {"min_rounds": 1, "max_rounds": 3,
                                                  "convergence_threshold": 0.70})
        for i, s in enumerate(stances):
            _view(ws, 1, f"r{i}", s, 0.8)
        return grader.grade(str(ws), DATE, 1)["convergence_score"]

    s_split = score(["bullish", "bearish", "neutral"])
    s_majority = score(["bullish", "bullish", "bearish"])
    s_unanimous = score(["bullish", "bullish", "bullish"])
    assert s_split < s_majority < s_unanimous


def _view_full(ws: Path, rnd: int, role: str, stance: str, conviction: float, **extra):
    d = ws / "discussion" / DATE / "panel" / f"round_{rnd}"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"role": role, "round": rnd, "stance": stance,
               "conviction": conviction, "core_claims": ["x"], **extra}
    (d / f"{role}_view.json").write_text(json.dumps(payload), encoding="utf-8")


def _prev_convergence(ws: Path, rnd: int, score: float):
    d = ws / "discussion" / DATE / "panel"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"convergence_round_{rnd}.json").write_text(
        json.dumps({"convergence_score": score}), encoding="utf-8")


def test_exact_tie_is_flagged_and_blocks_convergence(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.45})
    _view(ws, 1, "a", "bullish", 0.8)
    _view(ws, 1, "b", "bullish", 0.8)
    _view(ws, 1, "c", "bearish", 0.8)
    _view(ws, 1, "d", "bearish", 0.8)
    r = grader.grade(str(ws), DATE, 1)
    # cwa 0.5 >= 0.45 would otherwise converge, but a tie can never converge
    assert r["tie_between"] == ["bullish", "bearish"]
    assert r["exit"] is False and r["exit_reason"] == "tie_unresolved"


def test_exact_tie_at_cap_exits_max_rounds(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.45})
    _view(ws, 3, "a", "bullish", 0.8)
    _view(ws, 3, "b", "bearish", 0.8)
    r = grader.grade(str(ws), DATE, 3)
    assert r["tie_between"] == ["bullish", "bearish"]
    assert r["exit"] is True and r["exit_reason"] == "max_rounds"
    assert r["at_max_rounds"] is True


def test_conviction_collapse_suppresses_early_converged_exit(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    _view(ws, 1, "a", "bullish", 0.9)
    _view(ws, 1, "b", "bullish", 0.85)
    _view(ws, 1, "c", "bearish", 0.8)
    # Round 2: unanimous stance but total conviction collapses 2.55 -> 1.0
    _view(ws, 2, "a", "bullish", 0.35)
    _view(ws, 2, "b", "bullish", 0.40)
    _view(ws, 2, "c", "bullish", 0.25)
    r = grader.grade(str(ws), DATE, 2)
    assert r["conviction_collapse"] is True
    assert r["conviction_retention"] < 0.75
    assert r["exit"] is False and r["exit_reason"] == "conviction_collapse"


def test_stalled_below_threshold_exits_with_unresolved_dissent(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.70})
    for rnd in (1, 2):
        _view(ws, rnd, "a", "bullish", 0.7)
        _view(ws, rnd, "b", "bearish", 0.6)
        _view(ws, rnd, "c", "neutral", 0.5)
    _prev_convergence(ws, 1, 0.62)  # round-2 score ~0.6333 -> |delta| < 0.05
    r = grader.grade(str(ws), DATE, 2)
    assert r["convergence_score"] < 0.70
    assert r["exit"] is True and r["exit_reason"] == "stalled"
    assert r["unresolved_dissent"] is True


def test_uncited_flip_carries_half_conviction(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.99})
    _view(ws, 1, "a", "bullish", 0.6)
    _view(ws, 1, "b", "bearish", 0.6)
    _view(ws, 1, "c", "bearish", 0.8)
    _view(ws, 2, "a", "bullish", 0.6)
    _view(ws, 2, "b", "bearish", 0.6)
    _view(ws, 2, "c", "bullish", 0.8)  # flip with no cited cause
    r = grader.grade(str(ws), DATE, 2)
    assert r["uncited_flips"] == ["c"]
    # bullish mass 0.6 + 0.8*0.5 = 1.0 of total 1.6, not 1.4 of 2.0
    assert r["conviction_weighted_agreement"] == round(1.0 / 1.6, 4)


def test_cited_flip_keeps_full_conviction(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 3, "convergence_threshold": 0.99})
    _view(ws, 1, "a", "bullish", 0.6)
    _view(ws, 1, "b", "bearish", 0.6)
    _view(ws, 1, "c", "bearish", 0.8)
    _view(ws, 2, "a", "bullish", 0.6)
    _view(ws, 2, "b", "bearish", 0.6)
    _view_full(ws, 2, "c", "bullish", 0.8,
               changed_beliefs="bearish->bullish",
               answers_to_prior_chair_notes="ev_1 resolved the chair's flagged dissent",
               evidence_ids=["ev_1"])
    r = grader.grade(str(ws), DATE, 2)
    assert r["uncited_flips"] == []
    assert r["conviction_weighted_agreement"] == round(1.4 / 2.0, 4)


def test_converged_at_cap_reports_at_max_rounds_flag(tmp_path):
    ws = _ws(tmp_path, {"min_rounds": 1, "max_rounds": 2, "convergence_threshold": 0.70})
    for role in ("a", "b", "c"):
        _view(ws, 2, role, "bullish", 0.9)
    r = grader.grade(str(ws), DATE, 2)
    assert r["exit"] is True and r["exit_reason"] == "converged"
    assert r["at_max_rounds"] is True

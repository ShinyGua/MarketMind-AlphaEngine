"""Valuation engine guards found by the formal-verification pass
(ex-docs/formal-verification/tracks/valuation.md), run through run_valuation.main
on synthetic workspaces, plus the grader false positives it found. Synthetic
fixtures only."""
import csv
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "valuation"))
import dcf as dcf_mod  # noqa: E402
import run_valuation as rv  # noqa: E402
from tests.test_valuation_grader_riskfree import DATE, _make, _summary  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "valuation_grader", ROOT / "eval" / "graders" / "valuation_grader.py")
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)

# A plain profitable US company: DCF, P/E and EV/EBITDA comps all run (blended).
COMPANY = {"quote_type": "EQUITY", "current_price": 100.0, "shares_outstanding": 1e9,
           "beta": 1.1, "market_cap": 1e11, "total_debt": 0.0, "total_cash": 0.0,
           "total_revenue": 3e10, "ebitda": 8e9, "trailing_pe": 25.0,
           "revenue_growth": 0.1, "operating_margins": 0.2}
PEERS = [{"trailing_pe": 20.0, "ev_to_ebitda": 12.0, "ev_to_revenue": 4.0, "revenue_growth": 0.08},
         {"trailing_pe": 30.0, "ev_to_ebitda": 15.0, "ev_to_revenue": 5.0, "revenue_growth": 0.09},
         {"trailing_pe": 22.0, "ev_to_ebitda": 14.0, "ev_to_revenue": 3.5, "revenue_growth": 0.07}]
# no macro file → rf 0.042, ERP 0.05: this beta puts WACC at 0.0250004, 4e-7 above g
NEAR_G_BETA = (0.0250004 - 0.042) / 0.05


def _strict(const):
    raise AssertionError(f"{const} is not JSON")


def _run(tmp_path, monkeypatch, valuation=None, **metrics):
    """Run the engine on COMPANY (with `metrics` overrides) and return
    (summary parsed as strict JSON, sensitivity CSV rows or None, workspace)."""
    ws = tmp_path / "workspaces" / "TEST"
    fdir = ws / "raw" / DATE / "fundamentals"
    (fdir / "peers").mkdir(parents=True)
    if valuation is not None:
        (ws / "resolved_config.json").write_text(json.dumps({"valuation": valuation}))
    fund = {"ticker": "TEST", "metrics": {**COMPANY, **metrics},
            "ttm": {"ebit": 6e9, "dep_amort": 1e9, "capex": -1.5e9},
            "income_statement": [{"revenue": 3e10, "ebit": 6e9, "pretax_income": 6e9,
                                  "tax_provision": 1.26e9},
                                 {"revenue": 2.73e10}, {"revenue": 2.48e10}, {"revenue": 2.25e10}],
            "cash_flow": [{"dep_amort": 1e9, "capex": -1.5e9, "operating_cash_flow": 7e9}]}
    (fdir / "TEST.json").write_text(json.dumps(fund))       # NaN/Infinity tokens, as json.load accepts
    for i, pm in enumerate(PEERS):
        (fdir / "peers" / f"P{i}.json").write_text(json.dumps({"ticker": f"P{i}", "metrics": pm}))
    monkeypatch.setattr(sys, "argv", ["run_valuation.py", str(ws), DATE])
    rv.main()
    vdir = ws / "valuation" / DATE
    summary = json.loads((vdir / "valuation_summary.json").read_text(), parse_constant=_strict)
    sens = vdir / "dcf_sensitivity.csv"
    rows = list(csv.reader(sens.open(newline=""))) if sens.exists() else None
    return summary, rows, ws


def _centre(rows):
    data = rows[1:]
    row = data[len(data) // 2][1:]
    return row[len(row) // 2]


# ── non-finite inputs (json.load accepts NaN / Infinity) ────────────────────

@pytest.mark.parametrize("bad", [{"beta": float("nan")},
                                 {"market_cap": float("nan"), "total_debt": 1e10},
                                 {"total_debt": float("nan")},
                                 {"total_revenue": float("inf")}])
def test_nonfinite_input_is_a_missing_value(tmp_path, monkeypatch, bad):
    # A NaN beta used to pass `wacc <= g` (NaN compares False) as a NaN WACC and
    # write bare NaN tokens into valuation_summary.json.
    s, _, _ = _run(tmp_path, monkeypatch, **bad)
    dcf = s["dcf"]
    assert math.isfinite(dcf["wacc"]) and dcf["wacc"] > dcf["terminal_growth"]
    assert s["intrinsic_value_base"] > 0 and s["fair_value"] > 0
    if "beta" in bad:
        assert dcf["beta"] == 1.0                     # same as a missing beta


def test_terminal_value_refuses_nan():
    for w, g in ((float("nan"), 0.025), (float("inf"), float("nan"))):
        with pytest.raises(ValueError):
            dcf_mod.terminal_value(1.0, w, g)


def test_summary_writer_never_emits_nan_tokens(tmp_path):
    rv._write_summary(str(tmp_path), {"a": float("inf"), "b": [1.5, float("nan")],
                                      "c": {"d": float("-inf")}})
    text = (tmp_path / "valuation_summary.json").read_text()
    assert json.loads(text, parse_constant=_strict) == {"a": None, "b": [1.5, None],
                                                        "c": {"d": None}}


def test_absurd_beta_skips_the_dcf_instead_of_raising(tmp_path, monkeypatch):
    # beta 1e40 overflows (1 + WACC)**t; the stage never raises into the pipeline
    s, rows, _ = _run(tmp_path, monkeypatch, beta=1e40)
    assert s["dcf"] is None and rows is None
    assert "dcf_overflow" in s["inputs_missing"]
    assert s["valuation_method"] == "comps_earnings"


def test_negative_share_count_is_missing(tmp_path, monkeypatch):
    # a negative count inverted bear/base/bull (-72.72 > -82.06 > -92.46)
    s, _, _ = _run(tmp_path, monkeypatch, shares_outstanding=-1e9)
    assert s["dcf"] is None and s["intrinsic_range"] is None
    assert "shares_outstanding" in s["inputs_missing"]


# ── sensitivity centre == base case ─────────────────────────────────────────

def test_centre_cell_is_the_base_case_to_the_cent(tmp_path, monkeypatch):
    # the centre used to be computed at round(wacc, 6): 8068.18 against base 8068.20
    s, rows, ws = _run(tmp_path, monkeypatch, shares_outstanding=1e7, current_price=8000.0,
                       market_cap=8e10, beta=1.1234567)
    assert float(_centre(rows)) == s["intrinsic_value_base"]
    assert vg.grade(str(ws), DATE)["pass"]


def test_wacc_just_above_g(tmp_path, monkeypatch):
    s, rows, ws = _run(tmp_path, monkeypatch, beta=NEAR_G_BETA)
    # centre at the base rates, not at round(0.0250004, 6) == g (an empty cell)
    assert _centre(rows) != "" and float(_centre(rows)) == s["intrinsic_value_base"]
    # the DCF is excluded as WACC ~ g, so its bull (~1.8e7 on a $100 stock) is no
    # part of the fair-value band
    dcf_c = next(c for c in s["method_candidates"] if c["method"] == "dcf")
    assert not dcf_c["included"] and "too close" in dcf_c["reason"]
    assert s["fair_value_range"]["high"] < 2 * s["current_price"]
    # wacc is published as 0.025 == g at 4 dp: the grader no longer hard-fails it
    g = vg.grade(str(ws), DATE)
    assert g["pass"], g["details"]


def test_grader_fails_an_empty_centre(tmp_path):
    s = _summary()
    s["intrinsic_value_base"] = 120.0
    ws = _make(tmp_path, s)
    (ws / "valuation" / DATE / "dcf_sensitivity.csv").write_text(
        "wacc\\g,0.0200,0.0250,0.0300\n0.08,130.0,140.0,150.0\n"
        "0.09,110.0,,130.0\n0.10,95.0,100.0,105.0\n")
    c = next(c for c in vg.grade(str(ws), DATE)["checks"]
             if c["name"] == "sensitivity_center_eq_base")
    assert not c["ok"]


# ── fair-value selection ────────────────────────────────────────────────────

def test_fragile_dcf_with_low_growth_confidence_reads_low():
    # The fragile-TV branch already set (0.35, medium), so the growth cap's weight test
    # (0.35 > 0.35) never fired and the label stayed medium: a more fragile DCF read
    # more confident than a sound one (which is capped to low).
    dcf = {"wacc": 0.09, "terminal_growth": 0.025, "growth_confidence": "low",
           "scenarios": {"base": {"value_per_share": 200.0, "tv_fraction": 0.88}}}
    _, _, _, cands = rv._select_fair_value(
        dcf, {"blended": 100.0}, price=100.0,
        cfg={"dcf_min_wacc_spread": 0.015, "fragile_tv_fraction": 0.80})
    d = next(c for c in cands if c["method"] == "dcf")
    assert (d["weight"], d["confidence"]) == (0.35, "low")
    assert "growth confidence low" in d["reason"]


@pytest.mark.parametrize("reason,kept", [
    ("excluded: WACC too close to terminal growth", False),
    ("excluded: initial growth 60.0% exceeds WACC 9.0% by >10pp (non-convergent explicit phase)", False),
    ("not available", False),
    ("excluded: DCF base 206 exceeds comps blended 63 by >2.0x (divergent from market comps)", True),
])
def test_fair_value_range_keeps_only_a_sound_dcf_tail(reason, kept):
    dcf_block = {"scenarios": {"bear": {"value_per_share": 150.0},
                               "bull": {"value_per_share": 900.0}}}
    cands = [{"method": "dcf", "value": 206.0, "weight": 0.0, "included": False,
              "reason": reason},
             {"method": "comps_earnings", "value": 63.0, "weight": 0.35, "included": True}]
    rng = rv._build_fair_value_range(63.0, cands, dcf_block)
    assert rng["high"] == (900.0 if kept else 63.0)


def test_divergence_guard_is_on_without_the_config_key(tmp_path, monkeypatch):
    # DCF 82 > 2 x comps 22; a resolved config lacking the key used to switch it off
    s, _, _ = _run(tmp_path, monkeypatch, valuation={}, trailing_pe=100.0, ebitda=None)
    d = next(c for c in s["method_candidates"] if c["method"] == "dcf")
    assert not d["included"] and "divergent" in d["reason"]
    assert s["valuation_method"] == "comps_earnings"


def test_divergence_guard_explicit_null_still_disables(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "resolved_config.json").write_text(
        json.dumps({"valuation": {"dcf_comps_divergence_cap": None}}))
    assert rv._cfg(str(ws))[0]["dcf_comps_divergence_cap"] is None


# ── grader: rounding of published values ───────────────────────────────────

def test_grader_mos_check_allows_for_penny_price_rounding(tmp_path):
    # HK 0.123 is published as 0.12; MoS was computed on 0.123 (0.626), the grader
    # recomputed 0.6667 from the published numbers and hard-failed
    s = _summary()
    s.update(current_price=0.12, fair_value=0.2, margin_of_safety=0.626)
    c = next(c for c in vg.grade(str(_make(tmp_path, s)), DATE)["checks"]
             if c["name"] == "margin_of_safety_recompute")
    assert c["ok"]


def test_grader_mos_check_still_catches_a_wrong_mos(tmp_path):
    s = _summary()
    s.update(current_price=100.0, fair_value=120.0, margin_of_safety=0.25)
    c = next(c for c in vg.grade(str(_make(tmp_path, s)), DATE)["checks"]
             if c["name"] == "margin_of_safety_recompute")
    assert not c["ok"]


def test_grader_wacc_check_still_catches_wacc_below_g(tmp_path):
    s = _summary()
    s["dcf"] = {"wacc": 0.0249, "terminal_growth": 0.025}
    r = vg.grade(str(_make(tmp_path, s)), DATE)
    assert not next(c for c in r["checks"] if c["name"] == "wacc_gt_terminal_growth")["ok"]

"""Tests for the valuation grader's risk-free checks: hard in-band check on
macro_inputs.risk_free_rate, and warning-only provenance checks (live yield
available but unused; stale drift). Synthetic fixtures only.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "valuation_grader", ROOT / "eval" / "graders" / "valuation_grader.py")
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)

DATE = "2026-01-15"


def _summary(rf=0.0454, src="DGS10", with_dcf=True):
    return {
        "ticker": "TEST", "applicable": True, "confidence": "medium",
        "macro_inputs": {"risk_free_rate": rf, "risk_free_source": src,
                         "risk_free_as_of": "2026-01-14"},
        "dcf": {"wacc": 0.09, "terminal_growth": 0.025} if with_dcf else None,
        "method_candidates": [], "inputs_missing": [],
    }


def _make(tmp_path, summary, regime=None):
    ws = tmp_path / "workspaces" / "TEST"
    vdir = ws / "valuation" / DATE
    vdir.mkdir(parents=True)
    (vdir / "valuation_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if regime is not None:
        p = (tmp_path / "workspaces" / "shared" / "market_context" / DATE
             / "indicators" / "macro_regime.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(regime), encoding="utf-8")
    return ws


def _regime(value=0.0454):
    return {"rates": {"us10y": {"value": value, "source": "fred",
                                "data_quality": "fred"}}}


def _check(result, name):
    return next((c for c in result["checks"] if c["name"] == name), None)


def test_in_band_rf_passes(tmp_path):
    ws = _make(tmp_path, _summary(), regime=_regime())
    r = vg.grade(str(ws), DATE)
    c = _check(r, "risk_free_in_band")
    assert c and c["ok"]
    assert r["pass"]
    assert r["warnings"] == []


def test_out_of_band_rf_fails(tmp_path):
    ws = _make(tmp_path, _summary(rf=0.45))
    r = vg.grade(str(ws), DATE)
    c = _check(r, "risk_free_in_band")
    assert c and not c["ok"]
    assert not r["pass"]


def test_live_available_but_fallback_warns_only(tmp_path):
    ws = _make(tmp_path, _summary(rf=0.042, src="config_fallback"), regime=_regime(0.0454))
    r = vg.grade(str(ws), DATE)
    assert r["pass"]  # warning, not a failure
    assert any("config_fallback" in w for w in r["warnings"])


def test_stale_drift_warns_only(tmp_path):
    ws = _make(tmp_path, _summary(rf=0.0470, src="DGS10"), regime=_regime(0.0454))
    r = vg.grade(str(ws), DATE)
    assert r["pass"]
    assert any("stale" in w for w in r["warnings"])


def test_no_shared_dir_no_warning(tmp_path):
    ws = _make(tmp_path, _summary(rf=0.042, src="config_fallback"))
    r = vg.grade(str(ws), DATE)
    assert r["pass"]
    assert r["warnings"] == []


# ── currency mismatch warning + fair_value_range ordering (added with market_capm) ──

def test_currency_mismatch_warns_only(tmp_path):
    # CNY name discounted with a US rate (^TNX) → warning, not a failure.
    s = _summary(rf=0.0449, src="^TNX")
    s["meta"] = {"market_profile": "CN", "currency": "CNY"}
    ws = _make(tmp_path, s)  # no regime → no drift/available warnings
    r = vg.grade(str(ws), DATE)
    assert r["pass"]
    assert any("market_profile=CN" in w for w in r["warnings"])


def test_no_currency_warning_when_config_market(tmp_path):
    # Currency-matched static rate → no mismatch warning.
    s = _summary(rf=0.022, src="config_market:CN")
    s["meta"] = {"market_profile": "CN", "currency": "CNY"}
    ws = _make(tmp_path, s)
    r = vg.grade(str(ws), DATE)
    assert r["pass"]
    assert not any("market_profile" in w for w in r["warnings"])


def test_config_market_rate_no_false_drift_warning(tmp_path):
    # A CN static rate (2.2%) must NOT be flagged as drifting from the US 10Y (4.54%).
    s = _summary(rf=0.022, src="config_market:CN")
    s["meta"] = {"market_profile": "CN", "currency": "CNY"}
    ws = _make(tmp_path, s, regime=_regime(0.0454))
    r = vg.grade(str(ws), DATE)
    assert r["pass"]
    assert not any("stale" in w for w in r["warnings"])


def test_fair_value_range_ordered_hard_fails(tmp_path):
    s = _summary()
    s["fair_value_range"] = {"low": 182.31, "base": 147.31, "high": 200.0}
    ws = _make(tmp_path, s)
    r = vg.grade(str(ws), DATE)
    c = _check(r, "fair_value_range_ordered")
    assert c and not c["ok"]
    assert not r["pass"]


def test_fair_value_range_ordered_passes(tmp_path):
    s = _summary()
    s["fair_value_range"] = {"low": 62.91, "base": 147.31, "high": 233.35}
    ws = _make(tmp_path, s)
    r = vg.grade(str(ws), DATE)
    c = _check(r, "fair_value_range_ordered")
    assert c and c["ok"]
    assert r["pass"]

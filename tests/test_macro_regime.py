"""Tests for scripts/compute_macro_regime.py — deterministic regime classification
on synthetic series (no live-artifact numbers). Covers direction labels, boundary
values (zero slope), sparse-data degradation, and bilingual summary emission.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "compute_macro_regime", ROOT / "scripts" / "compute_macro_regime.py")
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

DATE = "2026-01-15"
TH = mr.DEFAULT_THRESHOLDS


# ---------- rate_trend ----------

def test_rate_trend_rising():
    values = [0.040 + 0.0001 * i for i in range(70)]  # +0.69% over 69 steps
    r = mr.rate_trend(values, TH["rate_stable_band"])
    assert r["direction"] == "rising"
    assert r["value"] == round(values[-1], 6)
    assert r["delta_3m"] > 0


def test_rate_trend_falling():
    values = [0.050 - 0.0001 * i for i in range(70)]
    assert mr.rate_trend(values, TH["rate_stable_band"])["direction"] == "falling"


def test_rate_trend_flat_is_stable():
    values = [0.042] * 70
    r = mr.rate_trend(values, TH["rate_stable_band"])
    assert r["direction"] == "stable"
    assert r["delta_3m"] == 0


def test_rate_trend_short_series_has_no_direction():
    r = mr.rate_trend([0.042] * 10, TH["rate_stable_band"])
    assert r["direction"] is None
    assert r["value"] is not None


def test_rate_trend_empty():
    assert mr.rate_trend([], TH["rate_stable_band"])["value"] is None


# ---------- curve_slope ----------

def test_curve_inverted():
    assert mr.curve_slope(0.045, 0.042, TH["curve_flat_band"])["label"] == "inverted"


def test_curve_exactly_zero_slope_is_flat():
    assert mr.curve_slope(0.042, 0.042, TH["curve_flat_band"])["label"] == "flat"


def test_curve_normal():
    c = mr.curve_slope(0.038, 0.045, TH["curve_flat_band"])
    assert c["label"] == "normal"
    assert c["slope_2s10s"] == round(0.045 - 0.038, 6)


def test_curve_missing_leg():
    assert mr.curve_slope(None, 0.045, TH["curve_flat_band"])["label"] is None


# ---------- inflation_trend ----------

def test_inflation_needs_13_obs():
    out = mr.inflation_trend([100.0] * 12, [100.0] * 12, TH["inflation_band"])
    assert out["cpi_yoy"] is None


def test_inflation_cooling():
    # 4% YoY but the last 3 months are flat -> 3m annualized ~0 -> cooling
    cpi = [100.0 * (1.04 ** (i / 12)) for i in range(12)] + [100.0 * 1.04] * 3
    out = mr.inflation_trend(cpi, cpi, TH["inflation_band"])
    assert out["direction"] == "cooling"
    assert out["cpi_yoy"] > 0.03


def test_inflation_heating():
    # flat for a year, then +1%/month for 3 months
    cpi = [100.0] * 12 + [101.0, 102.0, 103.0]
    out = mr.inflation_trend(cpi, cpi, TH["inflation_band"])
    assert out["direction"] == "heating"


def test_inflation_zero_base_guarded():
    cpi = [0.0] * 14
    out = mr.inflation_trend(cpi, cpi, TH["inflation_band"])
    assert out["cpi_yoy"] is None  # zero denominator never divides


# ---------- policy_stance ----------

def test_policy_easing():
    assert mr.policy_stance([0.050, 0.048, 0.045, 0.043], TH["policy_band"])["stance"] == "easing"


def test_policy_on_hold():
    assert mr.policy_stance([0.0433] * 6, TH["policy_band"])["stance"] == "on_hold"


def test_policy_tightening():
    assert mr.policy_stance([0.020, 0.025, 0.030, 0.035], TH["policy_band"])["stance"] == "tightening"


# ---------- vix_percentile ----------

def test_vix_percentile_max_is_top():
    values = [float(10 + i % 20) for i in range(252)] + [60.0]
    v = mr.vix_percentile(values, TH["vix_percentile_window"], TH["vix_labels"])
    assert v["percentile_1y"] == 1.0
    assert v["label"] == "stressed"


def test_vix_percentile_min_is_calm():
    values = [20.0] * 251 + [5.0]
    v = mr.vix_percentile(values, TH["vix_percentile_window"], TH["vix_labels"])
    assert v["percentile_1y"] <= TH["vix_labels"]["calm"]
    assert v["label"] == "calm"


def test_vix_insufficient_history():
    v = mr.vix_percentile([20.0] * 30, TH["vix_percentile_window"], TH["vix_labels"])
    assert v["label"] == "insufficient_history"
    assert v["percentile_1y"] is None


# ---------- credit_regime boundaries ----------

def test_credit_regime_boundaries():
    assert mr.credit_regime([0.029], TH["hy_regime"])["regime"] == "tight"
    assert mr.credit_regime([0.030], TH["hy_regime"])["regime"] == "normal"
    assert mr.credit_regime([0.045], TH["hy_regime"])["regime"] == "wide"
    assert mr.credit_regime([0.060], TH["hy_regime"])["regime"] == "stressed"


# ---------- usd_trend ----------

def test_usd_strengthening():
    values = [100.0 + 0.05 * i for i in range(70)]
    assert mr.usd_trend(values, TH["usd_trend_band_pct"])["direction"] == "strengthening"


def test_usd_zero_base_guarded():
    values = [0.0] * 70
    assert mr.usd_trend(values, TH["usd_trend_band_pct"])["chg_3m_pct"] is None


# ---------- end-to-end compute ----------

def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("date,value\n" + "\n".join(f"{d},{v}" for d, v in rows) + "\n",
                    encoding="utf-8")


def test_compute_all_missing_writes_skeleton(tmp_path):
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    regime = mr.compute(ws, DATE)
    out = tmp_path / "workspaces" / "shared" / "market_context" / DATE / "indicators" / "macro_regime.json"
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["rates"]["us10y"]["value"] is None
    assert data["rates"]["us10y"]["data_quality"] == "missing"
    assert "en" in data["summary"] and "ch" in data["summary"]
    assert regime["schema_version"] == 1


def test_compute_with_synthetic_fred_data(tmp_path):
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    macro = tmp_path / "workspaces" / "shared" / "market_context" / DATE / "raw" / "macro"
    # 10Y rising in FRED percent units; 2Y above it -> inverted curve
    _write_csv(macro / "DGS10.csv", [(f"d{i}", 4.0 + 0.01 * i) for i in range(70)])
    _write_csv(macro / "DGS2.csv", [(f"d{i}", 5.0) for i in range(70)])
    (macro / "macro_sources.json").write_text(json.dumps({
        "series": {"DGS10": {"source": "fred"}, "DGS2": {"source": "yfinance:^IRX"}},
        "inputs_missing": ["CPIAUCSL", "FEDFUNDS"],
    }), encoding="utf-8")

    regime = mr.compute(ws, DATE)
    assert regime["rates"]["us10y"]["direction"] == "rising"
    assert regime["rates"]["us10y"]["value"] == round((4.0 + 0.01 * 69) / 100, 6)  # percent -> decimal
    assert regime["rates"]["us10y"]["data_quality"] == "fred"
    assert regime["curve"]["label"] == "inverted"
    assert regime["curve"]["data_quality"] == "proxy"  # ^IRX leg taints the pair
    assert regime["inflation"]["data_quality"] == "missing"
    assert regime["inputs_missing"] == ["CPIAUCSL", "FEDFUNDS"]
    assert "10Y" in regime["summary"]["en"]
    assert "美债" in regime["summary"]["ch"]


def test_malformed_threshold_override_keeps_defaults(tmp_path):
    # A scalar where a dict is expected (or vice versa) must not kill the
    # artifact — the default threshold wins and the regime is still written.
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    (ws / "resolved_config.json").write_text(json.dumps({
        "macro_regime": {"vix_labels": 0.5,            # scalar over dict
                         "rate_stable_band": {"x": 1},  # dict over scalar
                         "usd_trend_band_pct": 3.0},    # valid override
    }), encoding="utf-8")
    th = mr._load_thresholds(ws)
    assert th["vix_labels"] == mr.DEFAULT_THRESHOLDS["vix_labels"]
    assert th["rate_stable_band"] == mr.DEFAULT_THRESHOLDS["rate_stable_band"]
    assert th["usd_trend_band_pct"] == 3.0
    out = (tmp_path / "workspaces" / "shared" / "market_context" / DATE
           / "indicators" / "macro_regime.json")
    mr.compute(ws, DATE)
    assert out.exists()  # artifact still written despite the malformed config

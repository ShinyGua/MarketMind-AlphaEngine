"""Tests for scripts/intraday_timing.py — RSI/MACD guards, 4h resample,
swing-pivot detection, and the available:false degradation path.
Synthetic 1h OHLCV only; yfinance is never called.
"""
import importlib.util
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "intraday_timing", ROOT / "scripts" / "intraday_timing.py")
it = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(it)

DATE = "2026-01-15"


def _ohlcv(closes, start="2026-01-05 10:00"):
    idx = pd.date_range(start, periods=len(closes), freq="1h")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.005, "Low": c * 0.995,
                         "Close": c, "Volume": 1000.0}, index=idx)


# ---------- rsi guards ----------

def test_rsi_monotone_up_hits_100():
    close = pd.Series([100.0 + i for i in range(40)])
    assert it.rsi(close, 14) == 100.0


def test_rsi_monotone_down_hits_0():
    close = pd.Series([100.0 - i for i in range(40)])
    assert it.rsi(close, 14) == 0.0


def test_rsi_flat_is_50_no_div_by_zero():
    close = pd.Series([100.0] * 40)
    assert it.rsi(close, 14) == 50.0


def test_rsi_short_series_is_none():
    assert it.rsi(pd.Series([100.0] * 5), 14) is None


# ---------- macd ----------

def test_macd_uptrend_positive():
    close = pd.Series([100.0 * (1.01 ** i) for i in range(60)])
    m = it.macd(close)
    assert m["macd"] > 0
    assert set(m) == {"macd", "macd_signal", "macd_histogram"}


def test_macd_short_series_is_none():
    assert it.macd(pd.Series([100.0] * 10)) is None


# ---------- resample ----------

def test_resample_4h_bar_count():
    df = _ohlcv([100.0] * 24)
    df4 = it.resample_4h(df)
    assert 5 <= len(df4) <= 7  # 24 hourly bars -> ~6 four-hour bins
    assert float(df4["Volume"].sum()) == float(df["Volume"].sum())


def test_resample_4h_bin_membership_open_stamped_bars():
    # Bars are stamped at their OPEN: 8 bars from 12:00 cover 12:00-20:00 and
    # must split into exactly two bins of 4 bars each — the 16:00 bar opens the
    # SECOND bin (right-closed binning would leak it into the first).
    df = _ohlcv([100.0, 101, 102, 103, 200, 201, 202, 203], start="2026-01-05 12:00")
    df4 = it.resample_4h(df)
    assert len(df4) == 2
    assert float(df4["Close"].iloc[0]) == 103.0  # 12:00-15:00 bars only
    assert float(df4["Open"].iloc[1]) == 200.0   # 16:00 bar opens bin 2


# ---------- swing levels ----------

def test_swing_levels_zigzag_finds_pivots():
    # 4h closes: rise to a peak, dip to a trough, recover — pivots confirmed
    pattern = [100, 102, 104, 106, 108, 110, 108, 106, 104, 102,
               100, 98, 96, 94, 92, 90, 92, 94, 96, 98, 100, 101]
    idx = pd.date_range("2026-01-05", periods=len(pattern), freq="4h")
    c = pd.Series(pattern, index=idx, dtype=float)
    df4 = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1.0})
    levels = it.swing_levels(df4, pivot_window=5)
    assert levels["swing_high"] == 110.0
    assert levels["swing_low"] == 90.0
    assert levels["range_30d_high"] == 110.0
    assert levels["range_30d_low"] == 90.0


def test_swing_levels_exact_double_top_confirms_newer_peak():
    # Two identical 110 peaks inside one window: the newer one must confirm
    # (strict uniqueness used to reject both).
    pattern = [100, 104, 108, 110, 108, 104, 108, 110, 108, 104,
               100, 96, 92, 90, 92, 96, 100, 102, 104, 105]
    idx = pd.date_range("2026-01-05", periods=len(pattern), freq="4h")
    c = pd.Series(pattern, index=idx, dtype=float)
    df4 = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1.0})
    levels = it.swing_levels(df4, pivot_window=5)
    assert levels["swing_high"] == 110.0
    assert levels["swing_high_at"] == str(idx[7])  # the newer of the two peaks


def test_swing_levels_thin_data_nulls():
    df = _ohlcv([100.0] * 8)
    levels = it.swing_levels(it.resample_4h(df), pivot_window=5)
    assert levels["swing_high"] is None and levels["swing_low"] is None


# ---------- timing_state ----------

def test_timing_state_extended_overbought():
    levels = {"swing_high": 100.0, "swing_low": 80.0}
    assert it.timing_state(75.0, 99.0, levels) == "extended_overbought"
    assert it.timing_state(75.0, 90.0, levels) == "overbought"
    assert it.timing_state(25.0, 80.5, levels) == "oversold_near_support"
    assert it.timing_state(50.0, 90.0, levels) == "neutral"
    assert it.timing_state(None, 90.0, levels) == "unknown"


# ---------- end-to-end run ----------

def test_run_unavailable_writes_artifact_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "fetch_hourly", lambda *a, **k: None)
    ws = tmp_path / "workspaces" / "0941.HK"
    ws.mkdir(parents=True)
    artifact = it.run(ws, DATE)
    assert artifact == {"schema_version": 1, "ticker": "0941.HK", "available": False,
                        "reason": "no_intraday_data", "usage": "timing_only"}
    out = json.loads((ws / "quant" / DATE / "intraday_timing.json").read_text(encoding="utf-8"))
    assert out["available"] is False


def test_run_with_synthetic_bars(tmp_path, monkeypatch):
    closes = [100.0 + 0.2 * i for i in range(300)]  # steady uptrend
    monkeypatch.setattr(it, "fetch_hourly", lambda *a, **k: _ohlcv(closes))
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    artifact = it.run(ws, DATE)
    assert artifact["available"] is True
    assert artifact["usage"] == "timing_only"
    assert artifact["timeframes"]["1h"]["rsi_14"] == 100.0  # monotone up
    assert artifact["timeframes"]["4h"]["macd"] > 0
    assert artifact["timing_state"] in ("overbought", "extended_overbought")
    assert "en" in artifact["note"] and "ch" in artifact["note"]


def _fake_yfinance(df):
    import sys
    import types

    mod = types.ModuleType("yfinance")

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, period=None, interval=None):
            return df

    mod.Ticker = Ticker
    return mod


def test_nan_closes_dropped_and_json_strict(tmp_path, monkeypatch):
    # yfinance 1h data contains NaN closes; the REAL fetch_hourly must drop them
    # so they never reach the artifact (json NaN literals are invalid) nor drive
    # timing_state off a NaN comparison.
    import sys
    closes = [100.0 + 0.2 * i for i in range(120)] + [float("nan")] * 3
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance(_ohlcv(closes)))
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    artifact = it.run(ws, DATE)
    assert artifact["available"] is True
    assert artifact["last_close"] == round(100.0 + 0.2 * 119, 2)  # last REAL close
    raw = (ws / "quant" / DATE / "intraday_timing.json").read_text(encoding="utf-8")
    json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))


def test_all_nan_closes_degrades_to_unavailable(tmp_path, monkeypatch):
    import sys
    closes = [float("nan")] * 10
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance(_ohlcv(closes)))
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    artifact = it.run(ws, DATE)
    assert artifact["available"] is False and artifact["reason"] == "no_intraday_data"


def test_disabled_config(tmp_path):
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    (ws / "resolved_config.json").write_text(
        json.dumps({"quant": {"intraday": {"enabled": False}}}), encoding="utf-8")
    artifact = it.run(ws, DATE)
    assert artifact["available"] is False and artifact["reason"] == "disabled"

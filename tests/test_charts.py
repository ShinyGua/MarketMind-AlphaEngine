"""Tests for templates/charts.py — the PDF price chart.

Covers the SMA/MACD warm-up (compute-before-trim), the MACD panel content, and the
no-network fallback. All data is synthetic — no live artifacts, no network.
"""
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("charts", ROOT / "templates" / "charts.py")
charts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(charts)


def _synthetic_df(n):
    """n business days of gently trending OHLCV ending 2026-06-05."""
    idx = pd.bdate_range(end="2026-06-05", periods=n)
    close = 100.0 + np.linspace(0, 20, n) + np.sin(np.arange(n) / 5.0)
    return pd.DataFrame({
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": np.full(n, 1_000_000.0),
    }, index=idx)


def _write_csv(df, path):
    out = df.copy()
    out.index.name = "Date"
    out.to_csv(path)


def test_prepare_chart_df_warmup_114_rows_full_overlays():
    # 114 = 65 display + 49 prior bars: SMA50 must be valid at the FIRST visible row.
    df = charts._prepare_chart_df(_synthetic_df(114), window=65)
    assert len(df) == 65
    first = df.iloc[0]
    for col in ("SMA20", "SMA50", "MACD", "MACD_signal"):
        assert not math.isnan(first[col]), f"{col} should be non-null at first visible row"


def test_prepare_chart_df_short_64_rows_sma50_nan_at_left():
    # Too few rows: SMA50 is undefined at the first visible row (the data-shortage case).
    df = charts._prepare_chart_df(_synthetic_df(64), window=65)
    assert len(df) == 64  # no trim (< window)
    assert math.isnan(df.iloc[0]["SMA50"])
    assert not math.isnan(df.iloc[0]["MACD"])  # ewm MACD is defined from row 1


def test_generate_price_chart_svg_has_sma_macd_and_volume(tmp_path):
    csv = tmp_path / "T_3mo.csv"
    _write_csv(_synthetic_df(130), csv)
    out = tmp_path / "price_chart.svg"
    charts.generate_price_chart("T", str(csv), str(out), run_date="2026-06-05")
    assert out.exists() and out.stat().st_size > 0
    svg = out.read_text(encoding="utf-8")
    assert "SMA 50" in svg
    assert "MACD" in svg
    assert "Vol" in svg


def test_no_network_fallback_still_renders(tmp_path, monkeypatch):
    # Short CSV + warm-up fetch unavailable (simulating no network): must still render.
    monkeypatch.setattr(charts, "_fetch_warmup_history", lambda *a, **k: None)
    csv = tmp_path / "T_3mo.csv"
    _write_csv(_synthetic_df(64), csv)
    out = tmp_path / "price_chart.svg"
    charts.generate_price_chart("T", str(csv), str(out), run_date="2026-06-05")
    assert out.exists() and out.stat().st_size > 0


# ---------------------------------------------------------------------------
# Benchmark (relative-performance index) resolution
# ---------------------------------------------------------------------------

def test_index_filename_candidates_caret_stripped():
    cands = charts._index_filename_candidates("^HSI")
    assert "HSI_prices.csv" in cands
    assert "HSI_3mo.csv" in cands


def test_index_filename_candidates_dotted_and_sanitized():
    cands = charts._index_filename_candidates("000001.SS")
    # Both the dotted shape and the underscore-sanitized shape must be offered.
    assert "000001.SS_prices.csv" in cands
    assert "000001_SS_prices.csv" in cands


def test_index_filename_candidates_caret_and_bare_match():
    assert charts._index_filename_candidates("^HSTECH") == charts._index_filename_candidates("HSTECH")


def _make_ws(tmp_path, date="2026-06-08", link=None):
    """Build a synthetic workspace tree; return (workspace, raw_prices, shared_dated_raw)."""
    ws = tmp_path / "WS"
    raw_prices = ws / "raw" / date / "prices"
    shared_dated = ws / ".." / "shared" / "market_context" / date / "raw"
    raw_prices.mkdir(parents=True, exist_ok=True)
    (ws.parent / "shared" / "market_context" / date / "raw").mkdir(parents=True, exist_ok=True)
    if link is not None:
        (ws / "profile").mkdir(parents=True, exist_ok=True)
        (ws / "profile" / "market_context_link.json").write_text(json.dumps(link))
    return ws, raw_prices, (ws.parent / "shared" / "market_context" / date / "raw")


def test_resolve_benchmark_shared_primary_beats_workspace_sector_etf(tmp_path):
    # The 1810 failure mode: a workspace-local sector_etf.csv must NOT pre-empt the
    # shared primary index (^HSI -> HSI_prices.csv).
    ws, raw_prices, shared = _make_ws(tmp_path, link={"primary_index": "^HSI", "secondary_indices": ["^HSTECH"]})
    _write_csv(_synthetic_df(80), shared / "HSI_prices.csv")
    _write_csv(_synthetic_df(80), raw_prices / "sector_etf.csv")
    path, name = charts._resolve_benchmark(str(ws), "2026-06-08", raw_prices)
    assert path.endswith("HSI_prices.csv")
    assert "Hang Seng" in name


def test_resolve_benchmark_us_fallback_to_spy(tmp_path):
    # No market_context_link.json: fall back to the legacy US index (SPY).
    ws, raw_prices, shared = _make_ws(tmp_path, link=None)
    _write_csv(_synthetic_df(80), shared / "SPY_prices.csv")
    path, name = charts._resolve_benchmark(str(ws), "2026-06-08", raw_prices)
    assert path.endswith("SPY_prices.csv")
    assert name == "S&P 500"


def test_relative_chart_labels_selected_benchmark(tmp_path):
    # End-to-end: resolved HK benchmark should be drawn as Hang Seng, not SPY.
    ws, raw_prices, shared = _make_ws(tmp_path, link={"primary_index": "^HSI", "secondary_indices": []})
    _write_csv(_synthetic_df(80), shared / "HSI_prices.csv")
    ticker_csv = raw_prices / "T_3mo.csv"
    _write_csv(_synthetic_df(80), ticker_csv)
    path, name = charts._resolve_benchmark(str(ws), "2026-06-08", raw_prices)
    out = tmp_path / "relative_chart.svg"
    charts.generate_relative_chart("T", str(ticker_csv), path, str(out), index_name=name)
    svg = out.read_text(encoding="utf-8")
    assert "Hang Seng" in svg
    assert "SPY" not in svg

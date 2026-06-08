"""Tests for templates/charts.py — the PDF price chart.

Covers the SMA/MACD warm-up (compute-before-trim), the MACD panel content, and the
no-network fallback. All data is synthetic — no live artifacts, no network.
"""
import importlib.util
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

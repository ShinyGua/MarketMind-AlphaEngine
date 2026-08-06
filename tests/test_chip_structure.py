"""Tests for scripts/compute_chip_structure.py and scripts/chip_evidence_cards.py.

All data is synthetic — no network, no live artifacts. The engine is exercised
through its pure builders plus a raw-CSV-backed end-to-end run.
"""
import importlib.util
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chips = _load("compute_chip_structure", ROOT / "scripts" / "compute_chip_structure.py")
cards = _load("chip_evidence_cards", ROOT / "scripts" / "chip_evidence_cards.py")


def _df(n=130, trend="up", vol_last_mult=1.0):
    idx = pd.bdate_range(end="2026-07-16", periods=n)
    if trend == "up":
        close = 10.0 + np.linspace(0, 5, n) + 0.2 * np.sin(np.arange(n) / 4)
    else:
        close = 20.0 - np.linspace(0, 8, n) + 0.2 * np.sin(np.arange(n) / 4)
    vol = np.full(n, 1_000_000.0)
    vol[-1] *= vol_last_mult
    return pd.DataFrame({"Open": close - 0.1, "High": close + 0.3,
                         "Low": close - 0.3, "Close": close, "Volume": vol}, index=idx)


def _mk_workspace(tmp_path, df, market="CN", date="2026-07-16"):
    ws = tmp_path / "600000"
    (ws / "profile").mkdir(parents=True)
    (ws / "profile" / "company_profile.json").write_text(json.dumps({
        "yf_ticker": "600000.SS", "market_profile": market,
        "shares_outstanding": 50_000_000, "float_shares": 40_000_000}))
    prices = ws / "raw" / date / "prices"
    prices.mkdir(parents=True)
    out = df.copy()
    out.index.name = "Date"
    out.to_csv(prices / "600000_3mo.csv")
    return ws


# ── volume block ─────────────────────────────────────────────────────

def test_volume_ratio_expanding():
    df = _df(vol_last_mult=2.0)
    block = chips.compute_volume_block(df, chips.DEFAULTS, 40_000_000)
    assert block["volume_ratio"] == 2.0
    assert block["volume_regime"] == "expanding"
    assert block["turnover"]["turnover_pct"] == 5.0  # 2M / 40M * 100


def test_turnover_missing_float_flagged():
    block = chips.compute_volume_block(_df(), chips.DEFAULTS, None)
    assert block["turnover"]["turnover_pct"] is None
    assert any("float_shares" in m for m in block["inputs_missing"])


# ── chip distribution ────────────────────────────────────────────────

def test_chip_distribution_uptrend_profit_high():
    dist = chips.compute_chip_distribution(_df(trend="up"), chips.DEFAULTS)
    assert dist is not None
    # last close is at the top of an uptrend → most traded volume in profit
    assert dist["profit_ratio"] > 0.8
    assert dist["trapped_ratio"] == round(1 - dist["profit_ratio"], 3)
    lo, hi = dist["cost_band_90"]
    assert lo < dist["main_peak_price"] < hi or lo <= dist["main_peak_price"] <= hi


def test_chip_distribution_downtrend_trapped():
    dist = chips.compute_chip_distribution(_df(trend="down"), chips.DEFAULTS)
    assert dist["profit_ratio"] < 0.2  # everyone bought higher


# ── support / resistance ─────────────────────────────────────────────

def test_support_resistance_sides():
    df = _df(trend="down")
    dist = chips.compute_chip_distribution(df, chips.DEFAULTS)
    sr = chips.compute_support_resistance(df, dist, chips.DEFAULTS)
    last = float(df["Close"].iloc[-1])
    assert all(s["price"] < last for s in sr["supports"])
    assert all(r["price"] > last for r in sr["resistances"])


# ── end-to-end (raw CSV, no network) ─────────────────────────────────

def test_build_from_raw_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(chips, "fetch_daily", lambda *a, **k: None)
    ws = _mk_workspace(tmp_path, _df())
    artifact = chips.build(ws, "2026-07-16", dict(chips.DEFAULTS))
    assert artifact["available"] is True
    assert artifact["source"] == "raw_csv"
    assert artifact["usage"] == "directional"
    assert artifact["volume"]["turnover"]["source"] == "float_shares"
    # ONE language per artifact, selected at write time. The fixture workspace
    # has no resolved_config.json, so it defaults to en.
    assert isinstance(artifact["note"], str) and artifact["note"]
    assert artifact["note_lang"] == "en"
    assert not re.search(r"[一-鿿]", artifact["note"])


def test_build_unavailable_when_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr(chips, "fetch_daily", lambda *a, **k: None)
    ws = tmp_path / "EMPTY"
    (ws / "profile").mkdir(parents=True)
    artifact = chips.build(ws, "2026-07-16", dict(chips.DEFAULTS))
    assert artifact["available"] is False
    assert artifact["usage"] == "directional"


def test_official_turnover_overrides_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(chips, "fetch_daily", lambda *a, **k: None)
    ws = _mk_workspace(tmp_path, _df())
    chips_dir = ws / "raw" / "2026-07-16" / "chips"
    chips_dir.mkdir(parents=True)
    (chips_dir / "cn_flows.json").write_text(json.dumps({
        "available": True,
        "turnover": {"data_quality": "akshare", "turnover_pct": 3.21, "avg_20d_pct": 2.5},
    }))
    artifact = chips.build(ws, "2026-07-16", dict(chips.DEFAULTS))
    assert artifact["volume"]["turnover"]["turnover_pct"] == 3.21
    assert artifact["volume"]["turnover"]["source"] == "akshare_official"
    assert artifact["cn_flows"]["turnover"]["turnover_pct"] == 3.21


# ── evidence cards ───────────────────────────────────────────────────

def _chip_fixture(**over):
    base = {
        "available": True, "date": "2026-07-16", "last_close": 12.0,
        "volume": {"obv_price_divergence": "none"},
        "chip_distribution": {"profit_ratio": 0.5, "window_days": 120},
        "platform": {"breakout": {"direction": "none"}},
        "cn_flows": {},
    }
    base.update(over)
    return base


def test_cards_quiet_tape_zero():
    assert cards.material_observations(_chip_fixture()) == []


def test_card_holder_concentrating_positive():
    chip = _chip_fixture(cn_flows={"holder_count": {
        "data_quality": "akshare", "trend": "concentrating",
        "latest": {"as_of": "2026-06-30", "holders": 25000, "change_pct": -4.2}}})
    obs = cards.material_observations(chip)
    assert [o["key"] for o in obs] == ["holder_concentrating"]
    built = cards.build_cards(chip, "600000", "2026-07-16", "ch")
    assert built[0]["sentiment"] == "positive"
    assert built[0]["desk"] == "chips"
    assert built[0]["usage"] == "directional"
    assert "25,000" in built[0]["title"]  # numbers in title → dedup-safe


def test_card_unlock_overhang_window():
    chip = _chip_fixture(cn_flows={"restricted_release": {
        "data_quality": "akshare",
        "upcoming": [{"date": "2026-08-15", "shares": 1e7, "pct_of_total": 4.0}]}})
    obs = cards.material_observations(chip)
    assert [o["key"] for o in obs] == ["unlock_overhang"]
    # outside the 60-day horizon → no card
    chip2 = _chip_fixture(cn_flows={"restricted_release": {
        "data_quality": "akshare",
        "upcoming": [{"date": "2026-12-30", "shares": 1e7, "pct_of_total": 4.0}]}})
    assert cards.material_observations(chip2) == []


def test_card_breakout_and_profit_extreme():
    chip = _chip_fixture(
        chip_distribution={"profit_ratio": 0.05, "window_days": 120},
        platform={"days_in_range": 40, "range_high": 12.5, "range_low": 10.0,
                  "breakout": {"direction": "up", "volume_confirmed": True}})
    keys = [o["key"] for o in cards.material_observations(chip)]
    assert "breakout_volume" in keys
    assert "profit_extreme" in keys
    built = cards.build_cards(chip, "600000", "2026-07-16", "en")
    extreme = next(c for c in built if "profit" in c["title"] or "in profit" in c["title"])
    assert extreme["sentiment"] == "negative"  # deep trap


def test_cards_ignore_unavailable_blocks():
    chip = _chip_fixture(cn_flows={"holder_count": {
        "data_quality": "unavailable", "reason": "ConnectionError"}})
    assert cards.material_observations(chip) == []

"""Tests for scripts/peer_divergence.py and the peer-grid chart."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pdiv = _load("peer_divergence", ROOT / "scripts" / "peer_divergence.py")
charts = _load("charts_grid", ROOT / "templates" / "charts.py")


def _series(n=140, shape="up"):
    idx = pd.bdate_range(end="2026-07-16", periods=n)
    if shape == "up":
        vals = 100 + np.linspace(0, 40, n)
    elif shape == "down":
        vals = 100 - np.linspace(0, 30, n)
    elif shape == "flat":
        vals = 100 + 2 * np.sin(np.arange(n) / 7)
    else:  # launched: flat then +20% in the last 15 bars
        vals = np.concatenate([np.full(n - 15, 100.0), np.linspace(100, 125, 15)])
    return pd.Series(vals, index=idx)


def _write(prices: Path, name: str, series: pd.Series):
    df = pd.DataFrame({"Close": series.values,
                       "Volume": np.full(len(series), 1e6)}, index=series.index)
    df.index.name = "Date"
    prices.mkdir(parents=True, exist_ok=True)
    df.to_csv(prices / name)


def _mk_ws(tmp_path, date="2026-07-16"):
    ws = tmp_path / "600000"
    (ws / "profile").mkdir(parents=True)
    peers = [{"ticker": t, "name": t, "product_niche": f"niche-{t}"}
             for t in ("PA.SZ", "PB.SZ", "PC.SS")]
    (ws / "profile" / "peer_set.json").write_text(json.dumps({"peers": peers}))
    prices = ws / "raw" / date / "prices"
    _write(prices, "600000_3mo.csv", _series(shape="flat"))
    _write(prices, "peer_PA_SZ.csv", _series(shape="up"))
    _write(prices, "peer_PB_SZ.csv", _series(shape="down"))
    _write(prices, "peer_PC_SS.csv", _series(shape="launched"))
    _write(prices, "sector_etf.csv", _series(shape="up"))
    return ws


def test_divergence_classifies_paths(tmp_path):
    ws = _mk_ws(tmp_path)
    art = pdiv.run(ws, "2026-07-16")
    assert art["available"] is True
    classes = {m["ticker"]: m["path_class"] for m in art["members"]}
    assert classes["600000"] == "basing"          # flat target
    assert classes["PC.SS"] == "launched"         # +20% burst in 15 bars
    assert classes["PB.SZ"] == "independent_down"  # falling against a rising ETF
    assert art["dispersion_60d_pp"] > 0
    assert art["leader_60d"]["ticker"] in ("PA.SZ", "PC.SS")
    # target's product_niche is absent; peers carry theirs through
    pa = next(m for m in art["members"] if m["ticker"] == "PA.SZ")
    assert pa["product_niche"] == "niche-PA.SZ"


def test_divergence_degrades_without_peers(tmp_path):
    ws = tmp_path / "LONELY"
    (ws / "profile").mkdir(parents=True)
    art = pdiv.run(ws, "2026-07-16")
    assert art["available"] is False
    assert "reason" in art


def test_peer_grid_renders(tmp_path):
    series_map = {"T": _series(shape="flat"), "A": _series(shape="up"),
                  "B": _series(shape="down"), "C": _series(shape="launched")}
    out = tmp_path / "peer_grid.svg"
    charts.generate_peer_grid("T", series_map, str(out),
                              path_classes={"T": "basing", "A": "independent_up",
                                            "B": "independent_down", "C": "launched"})
    assert out.exists() and out.stat().st_size > 0
    svg = out.read_text(encoding="utf-8")
    assert "Peer Cohort" in svg or "同类股" in svg


def test_peer_grid_skips_tiny_cohort(tmp_path):
    out = tmp_path / "peer_grid.svg"
    charts.generate_peer_grid("T", {"T": _series()}, str(out))
    assert not out.exists()

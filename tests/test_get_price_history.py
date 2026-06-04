"""Regression tests for _get_price_history: a single-ticker yfinance download with
group_by="ticker" returns MultiIndex columns (ticker, field) even for one ticker,
which previously raised KeyError: 'Open'. yfinance.download is monkeypatched — no
network.
"""
import asyncio
import importlib.util
import json
from pathlib import Path

import pandas as pd
import yfinance

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "market_data_server", ROOT / "mcp" / "market_data_server.py")
mds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mds)

IDX = pd.DatetimeIndex(["2026-06-02", "2026-06-03"])
FIELDS = ["Open", "High", "Low", "Close", "Volume"]


def _multi(tickers):
    cols = pd.MultiIndex.from_product([tickers, FIELDS])
    rows = [[1.111, 2.0, 0.5, 1.5, 1000] * len(tickers)] * 2
    return pd.DataFrame(rows, index=IDX, columns=cols)


def _flat():
    return pd.DataFrame([[1.111, 2.0, 0.5, 1.5, 1000]] * 2, index=IDX, columns=FIELDS)


def _call(monkeypatch, frame, tickers):
    monkeypatch.setattr(yfinance, "download", lambda *a, **k: frame)
    out = asyncio.run(mds._get_price_history(
        {"tickers": tickers, "period": "5d", "interval": "1d"}))
    return json.loads(out[0].text)


def test_single_ticker_multiindex_no_keyerror(monkeypatch):
    r = _call(monkeypatch, _multi(["ORCL"]), ["ORCL"])
    d = r["data"]["ORCL"]
    assert d["open"] == [1.11, 1.11]          # rounded to 2dp
    assert d["volume"] == [1000, 1000]
    assert len(d["dates"]) == 2
    assert r["metadata"]["count"] == 2


def test_single_ticker_flat_frame(monkeypatch):
    r = _call(monkeypatch, _flat(), ["ORCL"])
    assert r["data"]["ORCL"]["close"] == [1.5, 1.5]


def test_single_ticker_as_bare_string(monkeypatch):
    # a stray string arg is coerced to a one-element list
    r = _call(monkeypatch, _multi(["ORCL"]), "ORCL")
    assert r["data"]["ORCL"]["high"] == [2.0, 2.0]


def test_multi_ticker_still_works(monkeypatch):
    r = _call(monkeypatch, _multi(["AAA", "BBB"]), ["AAA", "BBB"])
    assert set(r["data"]) == {"AAA", "BBB"}
    assert r["data"]["BBB"]["low"] == [0.5, 0.5]


def test_empty_frame_yields_empty_ohlcv(monkeypatch):
    r = _call(monkeypatch, pd.DataFrame(), ["ORCL"])
    assert r["data"]["ORCL"] == {"dates": [], "open": [], "high": [],
                                 "low": [], "close": [], "volume": []}

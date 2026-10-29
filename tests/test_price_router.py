"""Tests for scripts/price_router.py — symbol routing and partial-session handling.

No network: the per-route fetchers are monkeypatched.
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import price_router as pr  # noqa: E402


@pytest.mark.parametrize("symbol,route,native,market", [
    ("MU", "nasdaq", "MU", "US"),
    ("^GSPC", "nasdaq", "^GSPC", "US"),
    ("600519.SS", "cn_stock", "sh600519", "CN"),
    ("600519.SH", "cn_stock", "sh600519", "CN"),
    ("000333.SZ", "cn_stock", "sz000333", "CN"),
    ("300308", "cn_stock", "sz300308", "CN"),
    ("603072", "cn_stock", "sh603072", "CN"),
    ("000001.SS", "cn_index", "sh000001", "CN"),   # SSE Composite, not Ping An
    ("000001.SZ", "cn_stock", "sz000001", "CN"),   # Ping An Bank
    ("399006.SZ", "cn_index", "sz399006", "CN"),
    ("sh000300", "cn_index", "sh000300", "CN"),
    ("0700.HK", "hk_stock", "00700", "HK"),
    ("^HSI", "hk_index", "HSI", "HK"),
    ("^HSTECH", "hk_index", "HSTECH", "HK"),
    ("^VIX", "fred", "VIXCLS", "US"),
    ("BTC-USD", "fred", "CBBTCUSD", "US"),
    ("CNY=X", "fred", "DEXCHUS", "US"),
    ("^TNX", "fred", "DGS10", "US"),
])
def test_classify(symbol, route, native, market):
    assert pr.classify(symbol) == (route, native, market)


def test_unsupported_listing_raises():
    with pytest.raises(pr.FetchError, match="no approved"):
        pr.classify("7203.T")


def _frame(dates):
    return pd.DataFrame({"date": pd.to_datetime(dates), "open": 1.0, "high": 2.0,
                         "low": 0.5, "close": [1.5 + i for i in range(len(dates))],
                         "volume": 100.0})


class _FakeAk:
    def __init__(self, dates):
        self.dates = dates
        self.calls = []

    def stock_zh_a_daily(self, **kw):
        self.calls.append(("a", kw))
        return _frame(self.dates)

    def stock_hk_daily(self, **kw):
        self.calls.append(("hk", kw))
        return _frame(self.dates)

    def stock_zh_index_daily(self, **kw):
        self.calls.append(("idx", kw))
        return _frame(self.dates)

    def stock_hk_index_daily_sina(self, **kw):
        self.calls.append(("hkidx", kw))
        return _frame(self.dates)


SH = ZoneInfo("Asia/Shanghai")


def test_cn_route_unadjusted_and_bounded(monkeypatch):
    ak = _FakeAk(["2026-10-26", "2026-10-27", "2026-10-28"])
    monkeypatch.setattr(pr, "_akshare", lambda: ak)
    rows, prov = pr.fetch_any("600519.SS", months=1, end="2026-10-27",
                              now=datetime(2026, 10, 28, 20, 0, tzinfo=SH))
    assert [r["Date"] for r in rows] == ["2026-10-26", "2026-10-27"]  # end bound
    assert ak.calls[0][1]["adjust"] == "" and ak.calls[0][1]["symbol"] == "sh600519"
    assert prov["adjusted"] is False and prov["source"] == "akshare:stock_zh_a_daily"
    assert prov["last_bar_date"] == "2026-10-27"


def test_partial_cn_session_is_dropped(monkeypatch):
    ak = _FakeAk(["2026-10-28", "2026-10-29"])
    monkeypatch.setattr(pr, "_akshare", lambda: ak)
    # 11:21 Beijing: the 10-29 bar is a partial session.
    rows, prov = pr.fetch_any("300308.SZ", months=1, end="2026-10-29",
                              now=datetime(2026, 10, 29, 11, 21, tzinfo=SH))
    assert rows[-1]["Date"] == "2026-10-28" and prov["dropped_partial_row"] is True


def test_lunch_break_counts_as_in_session(monkeypatch):
    ak = _FakeAk(["2026-10-28", "2026-10-29"])
    monkeypatch.setattr(pr, "_akshare", lambda: ak)
    rows, prov = pr.fetch_any("000333.SZ", months=1, end="2026-10-29",
                              now=datetime(2026, 10, 29, 12, 30, tzinfo=SH))
    assert prov["dropped_partial_row"] is True


def test_after_cutoff_keeps_today(monkeypatch):
    ak = _FakeAk(["2026-10-28", "2026-10-29"])
    monkeypatch.setattr(pr, "_akshare", lambda: ak)
    rows, prov = pr.fetch_any("000333.SZ", months=1, end="2026-10-29",
                              now=datetime(2026, 10, 29, 15, 31, tzinfo=SH))
    assert rows[-1]["Date"] == "2026-10-29" and prov["dropped_partial_row"] is False


def test_hk_route_pads_code(monkeypatch):
    ak = _FakeAk(["2026-10-28"])
    monkeypatch.setattr(pr, "_akshare", lambda: ak)
    pr.fetch_any("0700.HK", months=1, end="2026-10-28",
                 now=datetime(2026, 10, 29, 9, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")))
    assert ak.calls[0][1]["symbol"] == "00700"


def test_fred_route_is_close_only(monkeypatch):
    monkeypatch.setattr(pr, "_fetch_fred", lambda native, m, f, t: [
        {"Date": "2026-10-27", "Open": None, "High": None, "Low": None, "Close": 16.2, "Volume": 0.0}])
    rows, prov = pr.fetch_any("^VIX", months=1, end="2026-10-28")
    assert prov["close_only"] is True and prov["native_symbol"] == "VIXCLS"
    assert rows[0]["Open"] is None


def test_source_errors_become_fetch_errors(monkeypatch):
    class Broken:
        def stock_zh_a_daily(self, **kw):
            raise ConnectionError("sina reset")

    monkeypatch.setattr(pr, "_akshare", lambda: Broken())
    with pytest.raises(pr.FetchError, match="cn_stock fetch failed"):
        pr.fetch_any("600519.SS", months=1, end="2026-10-28")


def test_empty_after_partial_drop_raises(monkeypatch):
    ak = _FakeAk(["2026-10-29"])
    monkeypatch.setattr(pr, "_akshare", lambda: ak)
    with pytest.raises(pr.FetchError, match="no completed bars"):
        pr.fetch_any("000333.SZ", months=1, end="2026-10-29",
                     now=datetime(2026, 10, 29, 10, 0, tzinfo=SH))

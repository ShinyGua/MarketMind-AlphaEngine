"""Tests for the MCP get_price_history tool, now served by scripts/price_router.py.

price_router.fetch_any is monkeypatched — no network. Covers per-symbol failure
isolation, period trimming, weekly resampling, close-only series and the
explicit 'unavailable' answer for intraday intervals.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import price_router  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "market_data_server", ROOT / "mcp" / "market_data_server.py")
mds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mds)


def _rows(dates, close_only=False):
    out = []
    for i, d in enumerate(dates):
        c = 10.0 + i
        out.append({"Date": d, "Open": None if close_only else c - 0.5,
                    "High": None if close_only else c + 1.111, "Low": None if close_only else c - 1,
                    "Close": c, "Volume": 0.0 if close_only else 1000.0 + i})
    return out


DATES = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05",
         "2026-06-08", "2026-06-09"]


def _call(monkeypatch, fake, tickers, period="1mo", interval="1d"):
    monkeypatch.setattr(price_router, "fetch_any", fake)
    out = asyncio.run(mds._get_price_history(
        {"tickers": tickers, "period": period, "interval": interval}))
    return json.loads(out[0].text)


def _ok_fetch(symbol, months=12, **_):
    return _rows(DATES), {"symbol": symbol, "source": "test", "adjusted": False}


def test_single_symbol_shape(monkeypatch):
    res = _call(monkeypatch, _ok_fetch, ["MU"])
    d = res["data"]["MU"]
    assert d["dates"] == DATES
    assert d["high"][0] == 11.11  # rounded to 2 dp
    assert d["volume"][0] == 1000
    assert res["metadata"]["sources"]["MU"]["source"] == "test"
    assert res["metadata"]["adjusted"] is False
    assert res["errors"] == {}


def test_bare_string_ticker_is_coerced(monkeypatch):
    res = _call(monkeypatch, _ok_fetch, "MU")
    assert list(res["data"]) == ["MU"]


def test_one_failure_does_not_abort_others(monkeypatch):
    def fake(symbol, months=12, **_):
        if symbol == "7203.T":
            raise price_router.FetchError("7203.T: no approved daily-price source")
        return _ok_fetch(symbol)

    res = _call(monkeypatch, fake, ["MU", "7203.T", "600519.SS"])
    assert res["data"]["7203.T"] == mds._EMPTY_OHLCV
    assert "no approved" in res["errors"]["7203.T"]
    assert len(res["data"]["MU"]["dates"]) == len(DATES)
    assert len(res["data"]["600519.SS"]["dates"]) == len(DATES)
    assert res["metadata"]["count"] == 2 * len(DATES)


def test_5d_period_keeps_last_five_bars(monkeypatch):
    res = _call(monkeypatch, _ok_fetch, ["MU"], period="5d")
    assert res["data"]["MU"]["dates"] == DATES[-5:]


def test_weekly_resample(monkeypatch):
    res = _call(monkeypatch, _ok_fetch, ["MU"], interval="1wk")
    d = res["data"]["MU"]
    # 06-01..06-05 is one ISO week, 06-08..06-09 the next; dated by last bar.
    assert d["dates"] == ["2026-06-05", "2026-06-09"]
    assert d["open"][0] == 9.5 and d["close"][0] == 14.0
    assert d["volume"][0] == sum(1000 + i for i in range(5))


def test_close_only_series_keeps_none(monkeypatch):
    def fake(symbol, months=12, **_):
        return _rows(DATES, close_only=True), {"symbol": symbol, "close_only": True}

    res = _call(monkeypatch, fake, ["^VIX"])
    d = res["data"]["^VIX"]
    assert d["open"] == [None] * len(DATES) and d["close"][0] == 10.0


def test_intraday_interval_is_explicitly_unavailable(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not fetch for intraday intervals")

    res = _call(monkeypatch, boom, ["MU"], interval="1h")
    assert res["data"]["MU"] == mds._EMPTY_OHLCV
    assert "daily bars only" in res["errors"]["MU"]
    assert res["metadata"]["unavailable"] is True


def test_company_info_is_explicitly_unavailable():
    out = json.loads(asyncio.run(mds._get_company_info({"ticker": "MU"}))[0].text)
    assert out["unavailable"] is True and out["use_instead"]


def test_non_us_fundamentals_and_calendar_unavailable():
    for fn in (mds._get_fundamentals, mds._get_earnings_calendar):
        out = json.loads(asyncio.run(fn({"ticker": "600519.SS"}))[0].text)
        assert out["unavailable"] is True

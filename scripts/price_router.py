#!/usr/bin/env python3
"""One daily-price router for every market — the only place symbols map to sources.

Yahoo/yfinance is not used anywhere in this pipeline (not as primary, not as a
fallback). Every consumer that needs daily bars — the MCP ``get_price_history``
tool, the workspace price collector, the chip layer, chart warm-up and the
outcome scorer — calls :func:`fetch_any`, so a symbol can never resolve to two
different series in two places.

    US stocks / ETFs     api.nasdaq.com (scripts/fetch_prices_nasdaq.fetch_daily)
    US broad indices     liquid tracking ETF via NASDAQ (^GSPC -> SPY, ...)
    CN A-shares          akshare Sina ``stock_zh_a_daily``      (600519.SS, 000333.SZ)
    CN indices           akshare Sina ``stock_zh_index_daily``  (000300.SS, 399001.SZ)
    HK stocks            akshare Sina ``stock_hk_daily``        (0700.HK)
    HK indices           akshare Sina ``stock_hk_index_daily_sina`` (^HSI)
    ^VIX, BTC-USD        FRED keyless CSV (VIXCLS, CBBTCUSD) — close-only

All prices are **as traded** (``adjusted: False``): a split inside the window is
a real gap, never silently smoothed. Close-only series carry ``None`` for
Open/High/Low and 0 volume, flagged ``close_only: True``.

A bar for today is dropped while that market's session is still open (the
lunch break counts as open), so no consumer mistakes a partial session for a
completed one. Provenance records ``dropped_partial_row``.

Usage:
    price_router.py 600519.SS --months 12 --end 2026-10-29 \
        --out workspaces/600519/raw/2026-10-29/prices/600519_3mo.csv --schema history
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_prices_nasdaq import INDEX_PROXY, FetchError  # noqa: E402
from fetch_prices_nasdaq import fetch_daily as _nasdaq_daily  # noqa: E402

# Close-only FRED series standing in for symbols with no approved OHLCV source.
# Workspace profiles still carry Yahoo-style macro symbols; each maps here.
FRED_SYMBOLS = {
    "^VIX": "VIXCLS", "VIX": "VIXCLS",
    "BTC-USD": "CBBTCUSD",
    "^TNX": "DGS10", "^FVX": "DGS5", "^TYX": "DGS30", "^IRX": "DTB3",
    "CNY=X": "DEXCHUS", "HKD=X": "DEXHKUS", "JPY=X": "DEXJPUS",
    "DX-Y.NYB": "DTWEXBGS",
}
# Substitutions that are not the same instrument — recorded in provenance.
FRED_PROXY_NOTES = {
    "DX-Y.NYB": "FRED DTWEXBGS is the broad trade-weighted dollar, not the ICE DXY",
    "^IRX": "FRED DTB3 is the 3-month bill yield in percent (Yahoo ^IRX was x10)",
}

HK_INDICES = {"^HSI": "HSI", "HSI": "HSI", "^HSCE": "HSCEI", "^HSTECH": "HSTECH"}

# A bar dated "today" is complete only after this local time.
SESSION = {
    "CN": (ZoneInfo("Asia/Shanghai"), time(15, 30)),
    "HK": (ZoneInfo("Asia/Hong_Kong"), time(16, 30)),
    "US": (ZoneInfo("America/New_York"), time(16, 30)),
}


def classify(symbol: str) -> tuple[str, str, str]:
    """Return (route, native_symbol, market). Raises FetchError when unsupported.

    route ∈ nasdaq | cn_stock | cn_index | hk_stock | hk_index | fred
    """
    s = symbol.strip().upper()
    if s in FRED_SYMBOLS:
        return "fred", FRED_SYMBOLS[s], "US"
    if s in HK_INDICES:
        return "hk_index", HK_INDICES[s], "HK"
    if s in INDEX_PROXY:
        return "nasdaq", s, "US"

    low = s.lower()
    if low[:2] in ("sh", "sz") and low[2:].isdigit() and len(low) == 8:
        return "cn_index", low, "CN"  # explicit Sina index form, e.g. sh000300

    code, _, suffix = s.partition(".")
    if suffix in ("SS", "SH", "SZ", "BJ") or (not suffix and code.isdigit() and len(code) == 6):
        if not (code.isdigit() and len(code) == 6):
            raise FetchError(f"{symbol}: malformed CN code")
        # 000xxx on Shanghai and 399xxx on Shenzhen are index codes; Shanghai
        # stocks start with 6/9, so 000001.SS is the SSE Composite, not Ping An.
        if suffix in ("SS", "SH") and code.startswith("000"):
            return "cn_index", f"sh{code}", "CN"
        if suffix == "SZ" and code.startswith("399"):
            return "cn_index", f"sz{code}", "CN"
        if suffix in ("SS", "SH"):
            prefix = "sh"
        elif suffix == "SZ":
            prefix = "sz"
        elif suffix == "BJ":
            prefix = "bj"
        else:  # bare 6-digit code: infer the exchange from the first digit
            prefix = "sh" if code[0] in "69" else ("bj" if code[0] in "48" else "sz")
        return "cn_stock", f"{prefix}{code}", "CN"
    if suffix == "HK":
        if not code.isdigit():
            raise FetchError(f"{symbol}: malformed HK code")
        return "hk_stock", code.zfill(5), "HK"
    if suffix and suffix != "US":
        raise FetchError(f"{symbol}: no approved daily-price source for .{suffix} listings")
    return "nasdaq", s, "US"


def _bounds(months: float, end: str | None) -> tuple[str, str]:
    todate = end or _date.today().isoformat()
    fromdate = (datetime.fromisoformat(todate) - timedelta(days=int(months * 31))).date().isoformat()
    return fromdate, todate


def _frame_rows(df, fromdate: str, todate: str) -> list[dict]:
    """akshare frame (date/open/high/low/close/volume) -> ascending row dicts."""
    out: dict[str, dict] = {}
    for rec in df.to_dict("records"):
        d = str(rec.get("date"))[:10]
        if not (fromdate <= d <= todate):
            continue
        close = rec.get("close")
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue
        if close != close:  # NaN
            continue

        def num(key):
            try:
                v = float(rec.get(key))
                return v if v == v else None
            except (TypeError, ValueError):
                return None

        out[d] = {"Date": d, "Open": num("open"), "High": num("high"), "Low": num("low"),
                  "Close": close, "Volume": num("volume") or 0.0}
    return [out[k] for k in sorted(out)]


def _akshare():
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise FetchError("akshare is not installed") from exc
    return ak


def _fetch_cn_stock(native: str, fromdate: str, todate: str) -> list[dict]:
    ak = _akshare()
    df = ak.stock_zh_a_daily(symbol=native, start_date=fromdate.replace("-", ""),
                             end_date=todate.replace("-", ""), adjust="")
    return _frame_rows(df, fromdate, todate)


def _fetch_cn_index(native: str, fromdate: str, todate: str) -> list[dict]:
    return _frame_rows(_akshare().stock_zh_index_daily(symbol=native), fromdate, todate)


def _fetch_hk_stock(native: str, fromdate: str, todate: str) -> list[dict]:
    return _frame_rows(_akshare().stock_hk_daily(symbol=native, adjust=""), fromdate, todate)


def _fetch_hk_index(native: str, fromdate: str, todate: str) -> list[dict]:
    return _frame_rows(_akshare().stock_hk_index_daily_sina(symbol=native), fromdate, todate)


def _fetch_fred(native: str, months: float, fromdate: str, todate: str) -> list[dict]:
    from collect_macro_series import fetch_fred_csv

    # fredgraph only takes a start date; over-fetch from the window start to today.
    span = max(1, (datetime.now() - datetime.fromisoformat(fromdate)).days // 30 + 1)
    rows, reason = fetch_fred_csv(native, span)
    if reason:
        raise FetchError(f"FRED {native}: {reason}")
    return [{"Date": d, "Open": None, "High": None, "Low": None, "Close": float(v), "Volume": 0.0}
            for d, v in rows if fromdate <= d <= todate]


def drop_partial_bar(rows: list[dict], market: str, now: datetime | None = None) -> tuple[list[dict], bool]:
    """Drop a trailing bar for a session that has not closed yet."""
    if not rows or market not in SESSION:
        return rows, False
    tz, cutoff = SESSION[market]
    local = (now or datetime.now(tz)).astimezone(tz)
    if rows[-1]["Date"] == local.date().isoformat() and local.time() < cutoff:
        return rows[:-1], True
    return rows, False


def fetch_any(symbol: str, months: float = 12, end: str | None = None,
              now: datetime | None = None, assetclass: str | None = None) -> tuple[list[dict], dict]:
    """Ascending OHLCV rows + provenance for any supported symbol. Raises FetchError."""
    route, native, market = classify(symbol)
    fromdate, todate = _bounds(months, end)
    note = None
    try:
        if route == "nasdaq":
            rows, prov = _nasdaq_daily(symbol, months=months, end=end, assetclass=assetclass)
            native, note = prov.get("nasdaq_symbol", native), prov.get("note")
        elif route == "cn_stock":
            rows = _fetch_cn_stock(native, fromdate, todate)
        elif route == "cn_index":
            rows = _fetch_cn_index(native, fromdate, todate)
        elif route == "hk_stock":
            rows = _fetch_hk_stock(native, fromdate, todate)
        elif route == "hk_index":
            rows = _fetch_hk_index(native, fromdate, todate)
        else:
            rows = _fetch_fred(native, months, fromdate, todate)
            note = FRED_PROXY_NOTES.get(symbol.strip().upper(),
                                        f"{symbol} served by FRED {native} (close-only)")
    except FetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - one symbol never aborts a caller
        raise FetchError(f"{symbol}: {route} fetch failed ({type(exc).__name__}: {str(exc)[:120]})") from exc

    rows, dropped = drop_partial_bar(rows, market, now)
    if not rows:
        raise FetchError(f"{symbol}: no completed bars in {fromdate}..{todate}")
    source = {
        "nasdaq": "api.nasdaq.com",
        "cn_stock": "akshare:stock_zh_a_daily",
        "cn_index": "akshare:stock_zh_index_daily",
        "hk_stock": "akshare:stock_hk_daily",
        "hk_index": "akshare:stock_hk_index_daily_sina",
        "fred": "fred:fredgraph.csv",
    }[route]
    return rows, {
        "symbol": symbol,
        "native_symbol": native,
        "route": route,
        "market": market,
        "source": source,
        "rows": len(rows),
        "first": rows[0]["Date"],
        "last": rows[-1]["Date"],
        "last_bar_date": rows[-1]["Date"],
        "adjusted": False,
        "close_only": route == "fred",
        "dropped_partial_row": dropped,
        "note": note,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily OHLCV from approved sources (no Yahoo)")
    ap.add_argument("symbol")
    ap.add_argument("--months", type=float, default=12)
    ap.add_argument("--end", help="end date YYYY-MM-DD (default: today)")
    ap.add_argument("--out", type=Path, help="write a CSV")
    ap.add_argument("--schema", choices=("history", "download"), default="download",
                    help="history = target-file layout; download = peers/indices layout")
    args = ap.parse_args()
    try:
        rows, prov = fetch_any(args.symbol, months=args.months, end=args.end)
    except FetchError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False))
        return 1
    if args.out:
        from fetch_prices_nasdaq import write_csv
        write_csv(rows, args.out, args.schema)
        prov["file"] = str(args.out)
    print(json.dumps({"ok": True, **prov}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

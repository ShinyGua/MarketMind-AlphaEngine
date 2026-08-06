#!/usr/bin/env python3
"""Daily OHLCV via the public NASDAQ API — the Yahoo-free price collector.

Replaces every ``yfinance`` price fetch in the pipeline. Emits byte-compatible
CSVs so nothing downstream (compute_chip_structure, peer_divergence,
mm-quant-analyst, templates/charts) needs to know the source changed.

Two on-disk schemas exist in the workspace and both are reproduced here:

  ``history``   Date,Open,High,Low,Close,Volume,Dividends,Stock Splits
                (what ``yf.Ticker().history()`` wrote — target price file)
  ``download``  Date,Adj Close,Close,High,Low,Open,Volume
                (what ``yf.download()`` wrote — peers, sector ETF, indices)

**Adjustment caveat.** NASDAQ returns *as-traded* prices; yfinance returned
split/dividend-adjusted ones. ``Adj Close`` is therefore set equal to ``Close``
and a split inside the window will show up as a real gap. peer_divergence.py
already discards any series with a >30% single-day move, so a split lands as a
dropped peer rather than a fabricated signal. Provenance is recorded in
``_sources.json`` next to the CSVs.

Index symbols have no NASDAQ historical endpoint; they route to liquid ETF
proxies (see INDEX_PROXY) and the substitution is recorded in provenance.

Usage:
    # one symbol
    fetch_prices_nasdaq.py MU --out prices/MU_3mo.csv --months 12 --schema history

    # whole workspace (target + peers + sector ETF + indices + macro assets)
    fetch_prices_nasdaq.py --workspace workspaces/MU --date 2026-08-03
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.nasdaq.com/api/quote/{symbol}/historical"

# api.nasdaq.com rejects non-browser agents (same pattern as scripts/nasdaq_fetch.py).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

# Index symbols have no historical endpoint — use the liquid tracking ETF.
INDEX_PROXY = {
    "^GSPC": "SPY", "^SPX": "SPY", "SPX": "SPY",
    "^IXIC": "QQQ", "^NDX": "QQQ", "NDX": "QQQ",
    "^DJI": "DIA",
    "^RUT": "IWM",
    "^SOX": "SOXX",
    "^VIX": "VIXY",
}

# Broad-market trackers — never the "sector" ETF for a specific name.
BROAD_INDEX_ETFS = {"SPY", "QQQ", "DIA", "IWM", "VTI", "VOO", "IVV"}

# Tried in order when the asset class is unknown.
ASSET_CLASSES = ("stocks", "etf", "index")

REQUEST_DELAY_S = 0.6  # be polite; the endpoint is unofficial


class FetchError(RuntimeError):
    pass


def resolve_symbol(symbol: str) -> tuple[str, str | None]:
    """Return (nasdaq_symbol, proxy_note). Index symbols map to an ETF proxy."""
    s = symbol.strip().upper()
    if s in INDEX_PROXY:
        return INDEX_PROXY[s], f"{s} has no NASDAQ history endpoint; used {INDEX_PROXY[s]} as proxy"
    # Non-US suffixed symbols (300685.SZ, 0293.HK) are not on this endpoint.
    if "." in s and not s.endswith(".US"):
        raise FetchError(f"{s}: non-US symbol, unsupported by api.nasdaq.com")
    return s.replace("-", "."), None  # BRK-B -> BRK.B


def _num(raw) -> float | None:
    """'$829.50' / '42,607,140' / 'N/A' -> float | None."""
    if raw is None:
        return None
    t = str(raw).replace("$", "").replace(",", "").strip()
    if not t or t.upper() in {"N/A", "NA", "--", "-"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _get_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_daily(
    symbol: str,
    months: int = 12,
    assetclass: str | None = None,
    end: str | None = None,
    timeout: int = 30,
    retries: int = 3,
) -> tuple[list[dict], dict]:
    """Ascending-by-date OHLCV rows + a provenance dict. Raises FetchError."""
    nsym, proxy_note = resolve_symbol(symbol)
    todate = end or _date.today().isoformat()
    fromdate = (datetime.fromisoformat(todate) - timedelta(days=int(months * 31))).date().isoformat()
    limit = max(120, int(months * 23) + 40)

    classes = (assetclass,) if assetclass else ASSET_CLASSES
    last_err = "unknown"
    for ac in classes:
        url = (f"{API.format(symbol=nsym)}?assetclass={ac}"
               f"&fromdate={fromdate}&todate={todate}&limit={limit}")
        for attempt in range(retries):
            try:
                payload = _get_json(url, timeout)
            except urllib.error.HTTPError as exc:
                last_err = f"http_{exc.code}"
                if exc.code in (429, 500, 502, 503):
                    time.sleep(2 ** attempt)
                    continue
                break
            except Exception as exc:  # noqa: BLE001 — never abort a run on one symbol
                last_err = type(exc).__name__
                time.sleep(1 + attempt)
                continue

            rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
            if not rows:
                last_err = "empty"
                break

            out = []
            for r in rows:
                try:
                    d = datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat()
                except (KeyError, ValueError):
                    continue
                close = _num(r.get("close"))
                if close is None:
                    continue  # a bar with no close is unusable
                out.append({
                    "Date": d,
                    "Open": _num(r.get("open")),
                    "High": _num(r.get("high")),
                    "Low": _num(r.get("low")),
                    "Close": close,
                    "Volume": _num(r.get("volume")) or 0.0,
                })
            if not out:
                last_err = "unparseable"
                break

            out.sort(key=lambda x: x["Date"])
            # The endpoint occasionally repeats the newest bar; keep the last per date.
            dedup: dict[str, dict] = {}
            for row in out:
                dedup[row["Date"]] = row
            out = [dedup[k] for k in sorted(dedup)]

            prov = {
                "symbol": symbol,
                "nasdaq_symbol": nsym,
                "assetclass": ac,
                "rows": len(out),
                "first": out[0]["Date"],
                "last": out[-1]["Date"],
                "source": "api.nasdaq.com",
                "adjusted": False,
                "note": proxy_note,
            }
            return out, prov
        time.sleep(REQUEST_DELAY_S)

    raise FetchError(f"{symbol}: no data ({last_err})")


def _fill(row: dict) -> dict:
    """Open/High/Low occasionally arrive N/A; fall back to Close so the bar is usable."""
    c = row["Close"]
    return {k: (row.get(k) if row.get(k) is not None else c) for k in ("Open", "High", "Low")}


def write_csv(rows: list[dict], path: Path, schema: str = "download") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        if schema == "history":
            w = csv.writer(fh)
            w.writerow(["Date", "Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"])
            for r in rows:
                f = _fill(r)
                w.writerow([r["Date"], f["Open"], f["High"], f["Low"], r["Close"],
                            int(r["Volume"]), 0.0, 0.0])
        else:  # download schema — Adj Close mirrors Close (see module docstring)
            w = csv.writer(fh)
            w.writerow(["Date", "Adj Close", "Close", "High", "Low", "Open", "Volume"])
            for r in rows:
                f = _fill(r)
                w.writerow([r["Date"], r["Close"], r["Close"], f["High"], f["Low"],
                            f["Open"], int(r["Volume"])])
    tmp.replace(path)


# --------------------------------------------------------------------------
# workspace batch mode
# --------------------------------------------------------------------------
def _load(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _peer_tickers(workspace: Path) -> list[str]:
    data = _load(workspace / "profile" / "peer_set.json")
    if not data:
        return []
    peers = data.get("peers", data) if isinstance(data, dict) else data
    out = []
    for p in peers or []:
        t = p.get("ticker") if isinstance(p, dict) else p
        if t:
            out.append(str(t))
    return out


def run_workspace(workspace: Path, date: str, months: int = 12) -> dict:
    prices = workspace / "raw" / date / "prices"
    profile = _load(workspace / "profile" / "company_profile.json") or {}
    ticker = profile.get("ticker") or workspace.name
    mc = _load(workspace / "profile" / "market_context_link.json") or {}

    report: dict = {"collected": [], "failed": [], "generated_at":
                    datetime.now(timezone.utc).isoformat(), "source": "api.nasdaq.com"}

    def grab(sym: str, out: Path, schema: str, label: str, assetclass: str | None = None):
        try:
            rows, prov = fetch_daily(sym, months=months, assetclass=assetclass, end=date)
            write_csv(rows, out, schema)
            prov["file"] = str(out.relative_to(workspace)) if out.is_relative_to(workspace) else str(out)
            prov["label"] = label
            report["collected"].append(prov)
            print(f"  ok   {label:<16} {sym:<10} {prov['rows']:>4} rows  {prov['first']}..{prov['last']}")
        except Exception as exc:  # noqa: BLE001
            report["failed"].append({"symbol": sym, "label": label, "reason": str(exc)})
            print(f"  FAIL {label:<16} {sym:<10} {exc}")
        time.sleep(REQUEST_DELAY_S)

    # Target — the file the chip/quant layers look for first.
    grab(ticker, prices / f"{ticker}_3mo.csv", "history", "target", "stocks")

    for peer in _peer_tickers(workspace):
        grab(peer, prices / f"peer_{peer}.csv", "download", f"peer:{peer}")

    secondary = mc.get("secondary_indices") or []
    # secondary_indices mixes broad-market and sector trackers; the sector ETF is
    # the one that is NOT a broad index (picking the first entry would make QQQ
    # the "sector" for a semiconductor name and flatten relative strength).
    etf = next((s for s in secondary
                if not str(s).startswith("^") and str(s).upper() not in BROAD_INDEX_ETFS), None)
    if etf is None:
        etf = next((s for s in secondary if not str(s).startswith("^")), None)
    if etf:
        grab(etf, prices / "sector_etf.csv", "download", "sector_etf", "etf")

    # Shared market context: indices + macro assets.
    shared = workspace.parent / "shared" / "market_context" / date / "raw"
    for sym in filter(None, [mc.get("primary_index"), *secondary]):
        safe = str(sym).lstrip("^").replace(".", "_").replace("-", "_")
        grab(str(sym), shared / f"{safe}_prices.csv", "download", f"index:{sym}")
    for sym in (mc.get("macro_assets") or ["GLD", "USO"]):
        safe = str(sym).lstrip("^").replace(".", "_").replace("-", "_")
        grab(str(sym), shared / f"{safe}_prices.csv", "download", f"macro:{sym}", "etf")

    (prices / "_sources.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily OHLCV via api.nasdaq.com (Yahoo-free)")
    ap.add_argument("symbol", nargs="?", help="single symbol, e.g. MU")
    ap.add_argument("--out", type=Path, help="output CSV path (single-symbol mode)")
    ap.add_argument("--schema", choices=("history", "download"), default="download")
    ap.add_argument("--assetclass", choices=ASSET_CLASSES)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--end", help="end date YYYY-MM-DD (default: today)")
    ap.add_argument("--workspace", type=Path, help="batch mode: collect the whole workspace")
    ap.add_argument("--date", help="run date YYYY-MM-DD (batch mode)")
    args = ap.parse_args()

    if args.workspace:
        if not args.date:
            ap.error("--workspace requires --date")
        print(f"NASDAQ price collection -> {args.workspace} @ {args.date}")
        rep = run_workspace(args.workspace, args.date, args.months)
        print(f"collected={len(rep['collected'])} failed={len(rep['failed'])}")
        return 0 if rep["collected"] else 1

    if not args.symbol:
        ap.error("provide a symbol or --workspace")
    try:
        rows, prov = fetch_daily(args.symbol, months=args.months,
                                 assetclass=args.assetclass, end=args.end)
    except FetchError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return 1
    if args.out:
        write_csv(rows, args.out, args.schema)
        prov["file"] = str(args.out)
    print(json.dumps({"ok": True, **prov}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

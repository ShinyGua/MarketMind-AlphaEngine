#!/usr/bin/env python3
"""Deterministic macro series collector for MarketMind-AlphaEngine.

Fetches the configured FRED series (CPI, Fed funds, Treasury curve, USD index,
HY credit spread, VIX) into the shared market context, FRED-first with a
per-series yfinance proxy fallback when FRED_API_KEY is absent or a request
fails. Self-degrading: series with no proxy are recorded in `inputs_missing`
with a reason; the script never raises and always exits 0.

Units note: CSVs store values in native FRED units (rates/spreads in percent,
CPI/USD as index levels, VIX as a level). yfinance treasury tickers quote
yield x 10 (^TNX 45.4 == 4.54%), so proxy values are divided by 10 to match
the FRED percent convention before being saved under the FRED series name.

Usage:
    .venv/bin/python3 scripts/collect_macro_series.py <workspace> <date> [--force]

Writes:
    workspaces/shared/market_context/{date}/raw/macro/{SERIES_ID}.csv
    workspaces/shared/market_context/{date}/raw/macro/macro_sources.json

If macro_sources.json already exists for the date, the fetch is skipped
(idempotent across same-day runs of different tickers) unless --force is given.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from shared.dotenv import load_dotenv  # noqa: E402

# Default series set; overridable via resolved_config.json data_sources.macro.series
MACRO_SERIES = [
    "CPIAUCSL",      # CPI, all items (monthly index)
    "CPILFESL",      # Core CPI (monthly index)
    "FEDFUNDS",      # Effective Fed funds rate (monthly, percent)
    "DGS2",          # 2Y Treasury (daily, percent)
    "DGS5",          # 5Y Treasury (daily, percent)
    "DGS10",         # 10Y Treasury (daily, percent)
    "DGS30",         # 30Y Treasury (daily, percent)
    "DTWEXBGS",      # Trade-weighted broad USD index (daily, index)
    "BAMLH0A0HYM2",  # ICE BofA HY OAS (daily, percent)
    "VIXCLS",        # VIX (daily, level)
]

# Keyless self-degrade map: FRED series -> yfinance ticker.
# ^IRX is a 13-week bill, an approximate stand-in for DGS2 (flagged as proxy).
YF_PROXY_MAP = {
    "DGS2": "^IRX",
    "DGS5": "^FVX",
    "DGS10": "^TNX",
    "DGS30": "^TYX",
    "DTWEXBGS": "DX-Y.NYB",
    "VIXCLS": "^VIX",
}

# yfinance treasury tickers (^IRX/^FVX/^TNX/^TYX) historically quote yield x 10
# (CBOE convention: 45.4 == 4.54%), but current yfinance returns the display
# value already in percent (4.54). Scale adaptively: values above this threshold
# are the x10 form and get divided to FRED percent units. (A true US Treasury
# yield above 12% is implausible; the x10 form sits above 12 for any yield >1.2%.)
_TREASURY_PROXIES = {"^IRX", "^FVX", "^TNX", "^TYX"}
_YIELD_X10_THRESHOLD = 12.0

# CPI releases lag ~6 weeks (month M publishes mid M+1), so 14 months can leave
# fewer than the 13 monthly obs the YoY needs during the pre-release window of
# each month — 16 keeps a safe margin (plus the 1y VIX percentile window).
DEFAULT_LOOKBACK_MONTHS = 16


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via temp file + os.replace so concurrent same-date readers never
    see a truncated shared artifact."""
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def shared_macro_dir(workspace: str | Path, date: str) -> Path:
    return Path(workspace).resolve().parent / "shared" / "market_context" / date / "raw" / "macro"


def _classify(exc=None, resp=None) -> str:
    """Same reason vocabulary as scripts/check_data_sources._classify."""
    import requests

    if exc is not None:
        if isinstance(exc, requests.exceptions.Timeout):
            return "network_failed"
        if isinstance(exc, (requests.exceptions.ConnectionError, OSError)):
            return "dns_failed" if "resolv" in str(exc).lower() or "getaddrinfo" in str(exc).lower() else "network_failed"
        return "network_failed"
    if resp is not None:
        if resp.status_code in (401, 403):
            return "auth_failed"
        if resp.status_code == 429:
            return "rate_limited"
        if resp.status_code >= 400:
            return "http_failed"
        return "auth_ok"
    return "http_failed"


def fetch_fred_series(series_id: str, api_key: str, lookback_months: int) -> tuple[list, str | None]:
    """Return ([(iso_date, float), ...], None) or ([], reason). Skips '.' gaps."""
    import requests

    start = (datetime.now(timezone.utc) - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series_id, "api_key": api_key,
                    "file_type": "json", "observation_start": start},
            timeout=15,
        )
        if resp.status_code >= 400:
            return [], _classify(resp=resp)
        body = resp.json()
    except (requests.exceptions.RequestException, OSError, ValueError) as exc:
        return [], _classify(exc=exc)

    rows = []
    for obs in body.get("observations", []):
        raw = obs.get("value")
        if raw in (None, "."):
            continue
        try:
            rows.append((obs["date"], round(float(raw), 4)))
        except (ValueError, TypeError, KeyError):
            continue
    if not rows:
        return [], "no_data"
    return rows, None


def fetch_yf_proxy(symbol: str, lookback_months: int) -> tuple[list, str | None]:
    """Daily closes for a yfinance proxy ticker, scaled to FRED units."""
    try:
        import yfinance as yf

        period = f"{max(lookback_months, 1)}mo"
        hist = yf.Ticker(symbol).history(period=period, interval="1d")
    except Exception as exc:  # yfinance raises a zoo of types; never abort
        return [], f"network_failed ({type(exc).__name__})"
    if hist is None or hist.empty or "Close" not in hist:
        return [], "no_data"

    closes = [(ts, c) for ts, c in hist["Close"].items() if c == c]  # drop NaN
    # Scale per-row, not per-window: Yahoo has historically flipped treasury
    # tickers between the x10 and percent forms, so a single window-wide factor
    # would corrupt half of a mixed-convention series.
    is_treasury = symbol in _TREASURY_PROXIES
    rows = []
    for ts, close in closes:
        v = float(close)
        if is_treasury and v > _YIELD_X10_THRESHOLD:
            v *= 0.1
        rows.append((ts.strftime("%Y-%m-%d"), round(v, 4)))
    if not rows:
        return [], "no_data"
    return rows, None


def write_series_csv(path: Path, rows: list) -> None:
    lines = ["date,value"] + [f"{d},{v}" for d, v in rows]
    _atomic_write_text(path, "\n".join(lines) + "\n")


def _load_macro_config(workspace: Path) -> dict:
    cfg_path = workspace / "resolved_config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        macro = cfg.get("data_sources", {}).get("macro", {}) or {}
    except (OSError, ValueError):
        macro = {}
    series = macro.get("series") or MACRO_SERIES
    # Old 3-series configs predate the macro layer; widen to the full default set.
    if set(series) <= {"DGS10", "DTWEXBGS", "VIXCLS"}:
        series = MACRO_SERIES
    return {
        "series": list(series),
        "proxies": macro.get("yf_proxies") or YF_PROXY_MAP,
        "lookback_months": int(macro.get("lookback_months") or DEFAULT_LOOKBACK_MONTHS),
    }


def collect(workspace: Path, date: str, force: bool = False) -> dict:
    import os

    out_dir = shared_macro_dir(workspace, date)
    sources_path = out_dir / "macro_sources.json"
    if sources_path.exists() and not force:
        # Honor the same-day cache only when the recorded run actually got data —
        # a transient network blip on the first ticker of the day must not pin
        # an empty macro layer for every later ticker.
        try:
            cached = json.loads(sources_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if cached and cached.get("mode") != "unavailable":
            print(f"collect_macro_series: {sources_path} exists, skipping "
                  f"(use --force to refetch)")
            return cached
        print("collect_macro_series: cached result is unavailable/corrupt — refetching")

    load_dotenv()
    cfg = _load_macro_config(workspace)
    api_key = os.environ.get("FRED_API_KEY") or ""

    series_status: dict[str, dict] = {}
    inputs_missing: list[str] = []

    for sid in cfg["series"]:
        rows, reason = ([], "no_key")
        if api_key:
            rows, reason = fetch_fred_series(sid, api_key, cfg["lookback_months"])
            if rows:
                write_series_csv(out_dir / f"{sid}.csv", rows)
                series_status[sid] = {"source": "fred", "rows": len(rows), "reason": None}
                continue
        proxy = cfg["proxies"].get(sid)
        if proxy:
            prows, preason = fetch_yf_proxy(proxy, cfg["lookback_months"])
            if prows:
                write_series_csv(out_dir / f"{sid}.csv", prows)
                series_status[sid] = {"source": f"yfinance:{proxy}", "rows": len(prows),
                                      "reason": None if not api_key else f"fred_{reason}"}
                continue
            reason = preason if not api_key else f"fred_{reason}; proxy_{preason}"
        series_status[sid] = {"source": "missing", "rows": 0, "reason": reason}
        inputs_missing.append(sid)

    modes = {v["source"].split(":")[0] for v in series_status.values() if v["source"] != "missing"}
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": date,
        "mode": "fred" if modes == {"fred"} else ("yfinance_proxy" if modes == {"yfinance"} else
                                                  ("mixed" if modes else "unavailable")),
        "series": series_status,
        "inputs_missing": inputs_missing,
    }
    _atomic_write_text(sources_path, json.dumps(result, indent=2))
    print(f"collect_macro_series: wrote {sources_path} (mode={result['mode']}, "
          f"missing={len(inputs_missing)})")
    return result


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    if len(args) < 2:
        print("usage: collect_macro_series.py <workspace> <date> [--force]")
        return
    try:
        collect(Path(args[0]), args[1], force=force)
    except Exception as exc:  # advisory stage — never fail the pipeline
        print(f"collect_macro_series: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

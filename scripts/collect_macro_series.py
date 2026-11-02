#!/usr/bin/env python3
"""Deterministic macro series collector for MarketMind-AlphaEngine.

Fetches the configured FRED series (CPI, Fed funds, Treasury curve, USD index,
HY credit spread, VIX) into the shared market context: the keyed FRED API when
FRED_API_KEY is set, else (or per series on failure) the keyless public
fredgraph CSV — the same series in the same units. Yahoo is not used.
Self-degrading: a series neither route serves is recorded in `inputs_missing`
with a reason; the script never raises and always exits 0.

Units note: CSVs store values in native FRED units (rates/spreads in percent,
CPI/USD as index levels, VIX as a level).

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


def fetch_fred_csv(series_id: str, lookback_months: int) -> tuple[list, str | None]:
    """Keyless FRED fallback via the public fredgraph CSV endpoint.

    The *real* FRED series in FRED units, so CPI, Fed funds and the HY OAS are
    available without an API key. Yahoo is not used anywhere in this pipeline.
    """
    import requests

    start = (datetime.now(timezone.utc) - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv",
            params={"id": series_id, "cosd": start},
            timeout=20,
        )
        if resp.status_code >= 400:
            return [], _classify(resp=resp)
        body = resp.text
    except (requests.exceptions.RequestException, OSError) as exc:
        return [], _classify(exc=exc)

    # FRED's maintenance page is served as HTTP 200 text/html; parsing it as CSV
    # once yielded a bogus "400" HY spread (from a font URL's "300,400").
    lines = body.splitlines()
    if not lines or series_id not in lines[0] or "<" in lines[0]:
        return [], "not_csv"

    rows = []
    for line in lines[1:]:  # skip the "observation_date,SERIES" header
        parts = line.split(",")
        if len(parts) < 2:
            continue
        raw = parts[1].strip()
        if raw in ("", "."):  # FRED gap marker
            continue
        try:
            datetime.strptime(parts[0].strip(), "%Y-%m-%d")
            rows.append((parts[0].strip(), round(float(raw), 4)))
        except ValueError:
            continue
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
        # Keyless fallback: the same FRED series over the public CSV endpoint.
        crows, creason = fetch_fred_csv(sid, cfg["lookback_months"])
        if crows:
            write_series_csv(out_dir / f"{sid}.csv", crows)
            series_status[sid] = {"source": "fred_csv", "rows": len(crows),
                                  "reason": None if not api_key else f"fred_{reason}"}
            continue
        reason = creason if not api_key else f"fred_{reason}; csv_{creason}"
        series_status[sid] = {"source": "missing", "rows": 0, "reason": reason}
        inputs_missing.append(sid)

    modes = {v["source"].split(":")[0] for v in series_status.values() if v["source"] != "missing"}
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": date,
        # fred_csv is FRED data in FRED units — same quality tier as the keyed API.
        "mode": "fred" if modes and modes <= {"fred", "fred_csv"} else (
            "mixed" if modes else "unavailable"),
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

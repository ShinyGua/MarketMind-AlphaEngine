#!/usr/bin/env python3
"""Preflight data-source diagnostics for MarketMind-AlphaEngine.

Answers the question a fallback-heavy run can't otherwise answer: *why* did a
source degrade — missing key, blocked DNS, auth failure, or rate limit? Loads the
project `.env` (so it sees the same keys the MCP server will), checks DNS for each
data host, and does one minimal live probe per keyed source, classifying the
result. It reports key **presence and length only — never the value**.

Usage:
    .venv/bin/python3 scripts/check_data_sources.py [--no-values]
    .venv/bin/python3 scripts/check_data_sources.py <workspace> <date> [--no-values]

With <workspace> <date>, writes sanitized JSON to
    {workspace}/raw/{date}/diagnostics/data_sources.json
Otherwise prints a summary to stdout. Exit code is always 0 — this is advisory.
"""
from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "scripts"))
from shared.dotenv import load_dotenv  # noqa: E402
from collect_macro_series import MACRO_SERIES  # noqa: E402

KEYS = ("NEWSAPI_KEY", "FRED_API_KEY")

# logical source -> hostname to resolve.
# Yahoo (query1.finance.yahoo.com) is deliberately absent: yfinance is no longer
# used anywhere in this pipeline. Prices come from api.nasdaq.com, fundamentals
# from data.sec.gov XBRL, macro from FRED (keyed API or the keyless CSV host).
HOSTS = {
    "newsapi": "newsapi.org",
    "nasdaq": "api.nasdaq.com",
    "sec": "www.sec.gov",
    "sec_data": "data.sec.gov",
    "sec_efts": "efts.sec.gov",
    "fred": "api.stlouisfed.org",
    "fred_csv": "fred.stlouisfed.org",
}

_DNS_MARKERS = (
    "name or service not known", "nodename nor servname", "failed to resolve",
    "getaddrinfo", "temporary failure in name resolution", "name resolution",
)


def _classify(exc=None, resp=None) -> str:
    """Same reason vocabulary as mcp/market_data_server._classify_request."""
    import requests

    if exc is not None:
        if isinstance(exc, socket.gaierror):
            return "dns_failed"
        if isinstance(exc, requests.exceptions.Timeout):
            return "network_failed"
        if isinstance(exc, (requests.exceptions.ConnectionError, OSError)):
            return "dns_failed" if any(m in str(exc).lower() for m in _DNS_MARKERS) else "network_failed"
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


def check_dns(host: str) -> str:
    try:
        socket.gethostbyname(host)
        return "ok"
    except OSError:
        return "failed"


def _probe(url: str, params: dict) -> str:
    import requests

    try:
        resp = requests.get(url, params=params, timeout=10)
        return _classify(resp=resp)
    except (requests.exceptions.RequestException, OSError) as exc:
        return _classify(exc=exc)


def probe_newsapi(present: bool, dns_ok: bool) -> str:
    import os

    if not present:
        return "no_key"
    if not dns_ok:
        return "dns_failed"
    return _probe("https://newsapi.org/v2/top-headlines",
                  {"country": "us", "pageSize": 1, "apiKey": os.environ["NEWSAPI_KEY"]})


def probe_fred(present: bool, dns_ok: bool) -> str:
    import os

    if not present:
        return "no_key"
    if not dns_ok:
        return "dns_failed"
    return _probe("https://api.stlouisfed.org/fred/series/observations",
                  {"series_id": "DGS10", "api_key": os.environ["FRED_API_KEY"],
                   "file_type": "json", "limit": 1})


def macro_plan(fred_probe: str, series=None, proxies=None) -> dict:
    """How the macro collector will source each series given the FRED probe result.

    Keyless runs no longer degrade to yfinance proxies (Yahoo is not used anywhere
    in this pipeline). They fall back to the public fredgraph CSV endpoint, which
    serves the *same* FRED series in FRED units — so a keyless run is now
    full-coverage rather than missing CPI / Fed funds / HY spread, and nothing is
    reported unavailable purely for lack of a key.
    """
    series = list(series if series is not None else MACRO_SERIES)
    if fred_probe == "auth_ok":
        return {"mode": "fred", "series_via_fred": series,
                "series_via_fred_csv": [], "series_unavailable": []}
    return {"mode": "fred_csv", "series_via_fred": [],
            "series_via_fred_csv": series, "series_unavailable": []}


def run(show_lengths: bool = True) -> dict:
    import os

    load_dotenv()  # fallback only — never overrides an already-set var

    keys = {}
    for k in KEYS:
        v = os.environ.get(k) or ""
        entry = {"present": bool(v)}
        if show_lengths:
            entry["length"] = len(v)
        keys[k] = entry

    dns = {host: check_dns(host) for host in HOSTS.values()}
    sources = {
        "newsapi": probe_newsapi(keys["NEWSAPI_KEY"]["present"], dns["newsapi.org"] == "ok"),
        "fred": probe_fred(keys["FRED_API_KEY"]["present"], dns["api.stlouisfed.org"] == "ok"),
    }
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "keys": keys,
        "dns": dns,
        "sources": sources,
        "macro": macro_plan(sources["fred"]),
    }


def main():
    argv = [a for a in sys.argv[1:] if a != "--no-values"]
    no_values = "--no-values" in sys.argv[1:]
    result = run(show_lengths=not no_values)

    ws = Path(argv[0]) if len(argv) >= 1 else None
    date = argv[1] if len(argv) >= 2 else None

    if ws is not None and date is not None:
        out = ws / "raw" / date / "diagnostics" / "data_sources.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"check_data_sources: wrote {out}")

    # human summary (no secret values ever)
    for k, info in result["keys"].items():
        tail = f" (len {info['length']})" if "length" in info else ""
        print(f"  key   {k}: {'present' if info['present'] else 'MISSING'}{tail}")
    for host, st in result["dns"].items():
        print(f"  dns   {host}: {st}")
    for src, st in result["sources"].items():
        print(f"  probe {src}: {st}")


if __name__ == "__main__":
    main()

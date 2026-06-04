#!/usr/bin/env python3
"""Resolve the run_date (last trading session) for a workspace, market-aware.

Usage:
    .venv/bin/python3 scripts/trading_day.py {workspace} [--market US|HK|CN|JP|UK|EU]

Reads ``company.market_profile`` from the workspace's resolved_config.json
(falling back to config.yaml, then to US) and prints the ISO date (YYYY-MM-DD)
of the most recent trading session on/before "now" in that exchange's
timezone, honouring exchange holidays via ``exchange_calendars``.

Cutoff rule (mirrors the legacy NY logic, generalised per market):
  * after the session's open on a trading day  -> that day
  * before the open, weekend, or a holiday     -> the previous session

Self-degrading: if ``exchange_calendars`` is unavailable or the market is
unknown, falls back to the original US weekend-only logic so the pipeline
never breaks.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# market_profile -> exchange_calendars code
MARKET_TO_CALENDAR = {
    "US": "XNYS",   # New York Stock Exchange
    "HK": "XHKG",   # Hong Kong
    "CN": "XSHG",   # Shanghai
    "JP": "XTKS",   # Tokyo
    "UK": "XLON",   # London
    "EU": "XETR",   # Xetra (Frankfurt)
}


def load_market(workspace: Path, override: str | None) -> str:
    """Resolve the market_profile (US|HK|CN|JP|UK|EU), defaulting to US."""
    if override:
        return override.upper()
    # Prefer resolved_config.json (JSON); fall back to config.yaml (YAML).
    rc = workspace / "resolved_config.json"
    if rc.exists():
        try:
            cfg = json.loads(rc.read_text(encoding="utf-8"))
            mp = (cfg.get("company", {}) or {}).get("market_profile")
            if mp:
                return str(mp).upper()
        except (json.JSONDecodeError, OSError):
            pass
    cy = workspace / "config.yaml"
    if cy.exists():
        try:
            import yaml
            cfg = yaml.safe_load(cy.read_text(encoding="utf-8")) or {}
            mp = (cfg.get("company", {}) or {}).get("market_profile")
            if mp:
                return str(mp).upper()
        except Exception:
            pass
    return "US"


def _ny_fallback() -> str:
    """Legacy US weekend-only logic (no holiday awareness)."""
    now = datetime.now(ZoneInfo("America/New_York"))
    today = now.date()
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    d = today if now >= market_open else today - timedelta(days=1)
    while d.weekday() >= 5:  # skip weekends
        d -= timedelta(days=1)
    return d.isoformat()


def resolve_trading_day(market: str) -> str:
    """Last trading session on/before now for the given market_profile."""
    code = MARKET_TO_CALENDAR.get(market.upper())
    if code is None:
        return _ny_fallback()
    try:
        import exchange_calendars as xcals
        import pandas as pd

        cal = xcals.get_calendar(code)
        now = pd.Timestamp.now(tz="UTC")
        local_today = now.tz_convert(cal.tz).normalize().tz_localize(None)

        if cal.is_session(local_today) and now >= cal.session_open(local_today):
            session = local_today
        else:
            # nearest session on/before today; if that is today (open not
            # reached yet) step back to the prior session.
            session = cal.date_to_session(local_today, direction="previous")
            if session == local_today:
                session = cal.previous_session(local_today)
        return session.date().isoformat()
    except Exception:
        # Any failure (missing dep, bad code, API change) -> never break the
        # pipeline; fall back to the US weekend logic.
        return _ny_fallback()


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve last trading session for a workspace.")
    ap.add_argument("workspace", help="path to workspaces/{TICKER}")
    ap.add_argument("--market", default=None, help="override market_profile (US|HK|CN|JP|UK|EU)")
    args = ap.parse_args()

    market = load_market(Path(args.workspace), args.market)
    print(resolve_trading_day(market))
    return 0


if __name__ == "__main__":
    sys.exit(main())

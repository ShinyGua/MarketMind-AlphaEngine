"""Descriptive history: decisions issued before the ledger existed.

These calls have no verified issuance time and no forecast, so they never
enter the preregistered evaluation and are not written to the ledger. This
rebuilds the earlier scoreboard from approved sources (no Yahoo) with matched
conventions — price return versus price-index return — as a one-off CSV plus a
short summary, every row marked ``provenance: unverified``.

Reference = the close of the run date's session (the last benchmark session on
or before ``run_date``), the convention of the original scoreboard. The
position state comes from today's undated investor profile, so it is flagged.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from . import prices, report
from .score import _closes

HORIZONS = (5, 20, 60)
FIELDS = ["ticker", "run_date", "label", "overlay", "market", "position_state_now", "horizon_sessions",
          "reference_date", "endpoint_date", "stock_return", "benchmark_return", "excess_return",
          "status", "provenance"]


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def rows_for(dec: dict, ws: Path, run_date: str, stock: list[dict], bench: list[dict]) -> list[dict]:
    profile = _load(ws / "profile" / "company_profile.json")
    inv = _load(ws / "profile" / "investor_profile.json")
    sessions = [r["Date"] for r in bench if r.get("Close") is not None]
    s, b = _closes(stock), _closes(bench)
    base = {"ticker": ws.name, "run_date": run_date, "label": dec.get("decision"),
            "overlay": dec.get("risk_overlay"),
            "market": (profile.get("market_profile") or "US").upper(),
            "position_state_now": inv.get("position_state"), "provenance": "unverified"}
    prior = [i for i, d in enumerate(sessions) if d <= run_date]
    out = []
    for h in HORIZONS:
        row = {**base, "horizon_sessions": h}
        if not prior or prior[-1] + h >= len(sessions):
            out.append({**row, "status": "immature"})
            continue
        d0, de = sessions[prior[-1]], sessions[prior[-1] + h]
        row.update(reference_date=d0, endpoint_date=de)
        if d0 not in s or de not in s:
            out.append({**row, "status": "unscorable"})
            continue
        sr, br = s[de] / s[d0] - 1, b[de] / b[d0] - 1
        out.append({**row, "stock_return": round(sr, 6), "benchmark_return": round(br, 6),
                    "excess_return": round(sr - br, 6), "status": "scored"})
    return out


def run(workspaces: Path, root: Path, fetch=None) -> dict:
    fetch = fetch or prices.fetch
    cache: dict[str, tuple] = {}

    def get(symbol, since):
        if symbol not in cache:
            cache[symbol] = fetch(symbol, since)
        return cache[symbol]

    rows = []
    for path in sorted(workspaces.glob("*/decision/*/final_decision.json")):
        ws, run_date = path.parents[2], path.parent.name
        dec = _load(path)
        profile = _load(ws / "profile" / "company_profile.json")
        market = (profile.get("market_profile") or "US").upper()
        if market not in prices.BENCHMARKS or not dec.get("decision"):
            continue
        symbol = profile.get("yf_ticker") or ws.name
        try:
            stock, _ = get(symbol, "2026-05-01")
            bench, _ = get(prices.BENCHMARKS[market], "2026-05-01")
        except Exception as exc:  # noqa: BLE001 - record and continue
            rows += [{"ticker": ws.name, "run_date": run_date, "horizon_sessions": h,
                      "status": f"price_fetch_failed: {str(exc)[:80]}", "provenance": "unverified"}
                     for h in HORIZONS]
            continue
        rows += rows_for(dec, ws, run_date, stock, bench)

    out_dir = root / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "backfill_descriptive.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    summary = {}
    for h in HORIZONS:
        for label in ("BUY", "HOLD", "SELL"):
            pairs = [(r["ticker"], r["excess_return"]) for r in rows
                     if r.get("status") == "scored" and r["horizon_sessions"] == h and r["label"] == label]
            if pairs:
                summary[f"{label}@{h}"] = report._stat(pairs)
    (out_dir / "backfill_summary.json").write_text(json.dumps(
        {"provenance": "unverified — descriptive only, outside the preregistered evaluation",
         "reference": "run-date session close; price return vs price-index return",
         "rows": len(rows), "by_label_horizon": summary}, indent=2), encoding="utf-8")
    return {"rows": len(rows), "summary": summary, "dir": str(out_dir)}

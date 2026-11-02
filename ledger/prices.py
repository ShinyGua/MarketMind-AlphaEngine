"""Prices for scoring, from approved sources only, archived with hashes.

Every series a score depends on is saved under ``{root}/prices/`` with its
source and acquisition time, and the outcome record carries the file's sha256 —
so a later reader can re-score from exactly the bars used, even after FRED's
rolling ten-year SP500 window has moved on.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

BENCHMARKS = {"US": "SP500", "CN": "sh000300", "HK": "^HSI"}
HEDGES = {"US": "SPY", "HK": "2800.HK"}


def _months_since(day: str, pad_days: int = 160) -> float:
    start = date.fromisoformat(day[:10])
    return ((date.today() - start).days + pad_days) / 30.0


def fetch(symbol: str, since: str) -> tuple[list[dict], dict]:
    """Daily bars from ``since`` (minus warm-up) to today."""
    if symbol == "SP500":
        from collect_macro_series import fetch_fred_csv

        # fredgraph.csv answers in 6-17 s, near its 20 s timeout: retry a timeout once.
        for _attempt in range(2):
            rows, reason = fetch_fred_csv("SP500", int(_months_since(since)) + 1)
            if reason != "network_failed":
                break
        if reason:
            raise RuntimeError(f"FRED SP500: {reason}")
        bars = [{"Date": d, "Open": None, "High": None, "Low": None, "Close": float(v), "Volume": 0.0}
                for d, v in rows]
        return bars, {"symbol": "SP500", "source": "fred:fredgraph.csv", "adjusted": False,
                      "close_only": True}
    from price_router import fetch_any

    return fetch_any(symbol, months=_months_since(since))


def archive(root: Path, label: str, symbol: str, rows: list[dict], prov: dict) -> str:
    """Write the bars as CSV; return the file's sha256."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["Date", "Open", "High", "Low", "Close", "Volume"])
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k) for k in w.fieldnames})
    data = buf.getvalue().encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    safe = symbol.replace("^", "").replace("/", "_").replace("=", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / "prices" / label / safe / f"{stamp}_{sha[:12]}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.with_suffix(".source.json").write_text(
        json.dumps({**prov, "sha256": sha, "acquired_at": stamp}, indent=2), encoding="utf-8")
    return sha

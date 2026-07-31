#!/usr/bin/env python3
"""Peer-cohort path divergence for MarketMind-AlphaEngine.

The feedback this implements: a sector label is too coarse — pick 5–10 truly
comparable names and WATCH how each one actually trades. Some move with the
sector; some walk their own path; the divergence (and the product-niche
difference behind it) is where the next opportunity usually hides.

For the target + every peer with a price CSV, computes 20/60/120/250d returns,
correlation to the sector benchmark and to the target, then classifies each
name's path:

    follows_sector   跟随板块 — high benchmark correlation, return near sector
    independent_up   独立走强 — outperforming with low benchmark correlation
    independent_down 独立走弱 — underperforming with low benchmark correlation
    basing           横盘蓄势 — long flat range, low realized movement
    launched         已启动   — strong recent run that broke away from the cohort

Also reports cohort dispersion (leader-vs-laggard spread) and the current
leader. Output: {workspace}/quant/{date}/peer_divergence.json.

Self-degrading: missing CSVs are skipped and listed; <2 usable peers → a
minimal artifact with `available: false`. Never fails the pipeline.

Usage:
    .venv/bin/python3 scripts/peer_divergence.py <workspace> <date>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from shared import contracts  # noqa: E402

WINDOWS = (20, 60, 120, 250)
_CORR_WINDOW = 60          # daily-return correlation window
_HIGH_CORR = 0.6           # ≥ → moves with the cohort/sector
_BASING_WIDTH_PCT = 15.0   # 60d close range ≤ this → basing candidate
_LAUNCH_RET_20D = 15.0     # 20d return ≥ this → launched candidate
_OUTPERF_PP = 10.0         # 60d return gap vs benchmark → independent


def _load_csv(path: Path):
    import pandas as pd

    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    date_col = "Date" if "Date" in df.columns else ("Datetime" if "Datetime" in df.columns else None)
    if date_col is None or "Close" not in df.columns:
        return None
    try:
        idx = pd.to_datetime(df[date_col], utc=True).dt.tz_localize(None)
    except (ValueError, TypeError):
        try:
            idx = pd.to_datetime(df[date_col])
        except Exception:
            return None
    df.index = pd.DatetimeIndex(idx).normalize()
    close = df["Close"].dropna()
    return close if len(close) >= 20 else None


def _find_price_csv(prices: Path, ticker: str):
    cands = [f"{ticker}_3mo.csv", f"{ticker}_medium.csv", f"peer_{ticker}.csv",
             f"peer_{ticker.replace('.', '_')}.csv",
             f"{ticker.replace('.', '')}_3mo.csv", f"{ticker.replace('.', '_')}_3mo.csv"]
    for name in cands:
        p = prices / name
        if p.exists():
            return p
    return None


def _ret(close, days):
    if len(close) <= days:
        return None
    return round(float(close.iloc[-1] / close.iloc[-(days + 1)] - 1) * 100, 2)


def _corr(a, b, window=_CORR_WINDOW):
    import pandas as pd

    if a is None or b is None:
        return None
    df = pd.concat([a.pct_change(), b.pct_change()], axis=1, join="inner").dropna()
    if len(df) < 20:
        return None
    tail = df.tail(window)
    c = tail.iloc[:, 0].corr(tail.iloc[:, 1])
    return round(float(c), 3) if c == c else None  # NaN guard


def classify_path(returns: dict, corr_bench, close, bench_r60=None) -> str:
    """Deterministic path label from returns + benchmark gap + correlation + range.

    Outcome beats co-movement: a name 20pp behind a rising sector is walking its
    own (bad) path even if its daily returns correlate with the index."""
    r20 = returns.get("20") or 0.0
    r60 = returns.get("60")
    # recent breakaway run dominates
    if r20 is not None and r20 >= _LAUNCH_RET_20D:
        return "launched"
    # long flat range?
    if len(close) >= 60:
        seg = close.iloc[-60:]
        mid = float(seg.median())
        width_pct = (float(seg.max()) - float(seg.min())) / mid * 100 if mid else 999
        if width_pct <= _BASING_WIDTH_PCT:
            return "basing"
    # outcome gap vs the sector benchmark decides "independent"
    if r60 is not None and bench_r60 is not None:
        gap = r60 - bench_r60
        if gap >= _OUTPERF_PP:
            return "independent_up"
        if gap <= -_OUTPERF_PP:
            return "independent_down"
        return "follows_sector"
    # no benchmark: fall back to correlation, then absolute return
    if corr_bench is not None and corr_bench >= _HIGH_CORR:
        return "follows_sector"
    if r60 is not None:
        if r60 >= _OUTPERF_PP:
            return "independent_up"
        if r60 <= -_OUTPERF_PP:
            return "independent_down"
    return "follows_sector" if (corr_bench or 0) >= 0.4 else "independent_down" \
        if (r60 or 0) < 0 else "independent_up"


def run(workspace: Path, date: str) -> dict:
    ws = Path(workspace)
    prices = ws / "raw" / date / "prices"
    if not prices.is_dir():
        prices = ws / "raw" / "prices"

    try:
        peer_set = json.loads((ws / "profile" / "peer_set.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        peer_set = {}
    peers = peer_set.get("peers") or []

    target = Path(str(ws)).name
    bench = _load_csv(prices / "sector_etf.csv") if (prices / "sector_etf.csv").exists() else None
    # Sanity: a spliced/mis-adjusted benchmark file (e.g. an overnight -50% "bar"
    # where the desk switched ETFs) poisons every gap-based classification.
    if bench is not None:
        day_moves = bench.pct_change().abs().dropna()
        if not day_moves.empty and float(day_moves.max()) > 0.30:
            bench = None
    target_path = _find_price_csv(prices, target)
    target_close = _load_csv(target_path) if target_path else None

    members, skipped = [], []
    entries = [{"ticker": target, "name": target, "is_target": True,
                "product_niche": None}] + [
        {"ticker": p.get("ticker"), "name": p.get("name") or p.get("ticker"),
         "is_target": False, "product_niche": p.get("product_niche"),
         "differentiation": p.get("differentiation"), "cap_tier": p.get("cap_tier")}
        for p in peers if p.get("ticker")]

    bench_r60 = _ret(bench, 60) if bench is not None else None
    for entry in entries:
        close = target_close if entry["is_target"] else None
        if close is None:
            path = _find_price_csv(prices, entry["ticker"])
            close = _load_csv(path) if path else None
        if close is None:
            skipped.append(entry["ticker"])
            continue
        returns = {str(w): _ret(close, w) for w in WINDOWS}
        corr_bench = _corr(close, bench)
        corr_target = None if entry["is_target"] else _corr(close, target_close)
        members.append({
            **{k: v for k, v in entry.items() if v is not None or k in ("ticker", "name")},
            "returns_pct": returns,
            "corr_benchmark_60d": corr_bench,
            "corr_target_60d": corr_target,
            "path_class": classify_path(returns, corr_bench, close, bench_r60),
        })

    artifact = {
        "date": date,
        "ticker": target,
        "benchmark": "sector_etf" if bench is not None else None,
        "available": len(members) >= 3,  # target + ≥2 peers
        "members": members,
        "skipped": skipped,
    }
    r60s = [(m["ticker"], m["returns_pct"].get("60"))
            for m in members if m["returns_pct"].get("60") is not None]
    if len(r60s) >= 2:
        r60s.sort(key=lambda t: -t[1])
        artifact["leader_60d"] = {"ticker": r60s[0][0], "return_pct": r60s[0][1]}
        artifact["laggard_60d"] = {"ticker": r60s[-1][0], "return_pct": r60s[-1][1]}
        artifact["dispersion_60d_pp"] = round(r60s[0][1] - r60s[-1][1], 2)
    if not artifact["available"]:
        artifact["reason"] = "fewer than 3 members with usable price history"

    out = contracts.peer_divergence_path(ws, date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"peer_divergence: wrote {out} (members={len(members)}, skipped={skipped})")
    return artifact


def main():
    if len(sys.argv) < 3:
        print("usage: peer_divergence.py <workspace> <date>")
        return
    try:
        run(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:  # advisory stage — never fail the pipeline
        print(f"peer_divergence: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

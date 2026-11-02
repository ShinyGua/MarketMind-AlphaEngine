"""Read the ledger's outcomes back out, under the preregistered rules.

Every point estimate weighs tickers equally (a name called eleven times counts
once); intervals come from a ticker-clustered bootstrap; immature or
unscorable outcomes are counted, never zero-filled. The verdict line comes
from the protocol's review points: below the first one, nothing is claimed.
"""
from __future__ import annotations

import random
from collections import defaultdict

from . import store

PRIMARY = 20


def ticker_mean(pairs: list[tuple[str, float]]) -> float | None:
    by = defaultdict(list)
    for t, v in pairs:
        by[t].append(v)
    if not by:
        return None
    return sum(sum(v) / len(v) for v in by.values()) / len(by)


MIN_TICKERS_FOR_CI = 5  # a bootstrap over 2-4 tickers gives a meaningless interval


def cluster_ci(pairs: list[tuple[str, float]], reps: int = 2000, seed: int = 0) -> tuple | None:
    by = defaultdict(list)
    for t, v in pairs:
        by[t].append(v)
    tickers = sorted(by)
    if len(tickers) < MIN_TICKERS_FOR_CI:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        draw = [rng.choice(tickers) for _ in tickers]
        vals = [sum(by[t]) / len(by[t]) for t in draw]
        means.append(sum(vals) / len(vals))
    means.sort()
    return round(means[int(0.025 * reps)], 6), round(means[int(0.975 * reps) - 1], 6)


def _stat(pairs):
    m = ticker_mean(pairs)
    return {"mean": round(m, 6) if m is not None else None, "ci95": cluster_ci(pairs),
            "calls": len(pairs), "tickers": len({t for t, _ in pairs})}


def build(root) -> dict:
    records = store.read(root)
    protocol = records[0]["body"]["protocol"] if records and records[0]["type"] == "protocol" else {}
    decisions = {r["body"]["decision_id"]: r["body"] for r in records if r["type"] == "decision"}
    outcomes = [r["body"] for r in records if r["type"] == "outcome"]
    out = {"decisions_sealed": len(decisions), "outcomes": len(outcomes), "horizons": {}}
    for h in sorted({o["horizon_sessions"] for o in outcomes}):
        rows = [o for o in outcomes if o["horizon_sessions"] == h]
        scored = [o for o in rows if o["forecast"].get("status") == "scored"]
        excess = [(o["ticker"], o["forecast"]["excess_return"]) for o in scored]
        brier = [(o["ticker"], o["forecast"]["brier"]) for o in scored if "brier" in o["forecast"]]
        # lagged base rate: outperformance share among forecasts already matured at issuance
        ordered = sorted(scored, key=lambda o: decisions[o["decision_id"]]["issued_at"])
        base_pairs = []
        for o in ordered:
            issued = decisions[o["decision_id"]]["issued_at"][:10]
            prior = [p["forecast"]["outperformed"] for p in ordered
                     if p["forecast"]["endpoint_date"] < issued]
            base = sum(prior) / len(prior) if prior else 0.5
            if "brier" in o["forecast"]:
                y = 1.0 if o["forecast"]["outperformed"] else 0.0
                base_pairs.append((o["ticker"], (base - y) ** 2))
        b = ticker_mean(brier)
        bb = ticker_mean(base_pairs)
        tracks = {}
        for track in ("if_flat", "if_long"):
            done = [o for o in rows if o["tracks"][track].get("status") == "scored"]
            tracks[track] = {
                "net_return": _stat([(o["ticker"], o["tracks"][track]["net_return"]) for o in done]),
                "vs_do_nothing": _stat([(o["ticker"], o["tracks"][track]["vs_do_nothing"]) for o in done]),
                "vs_benchmark_buy_hold": _stat([(o["ticker"], o["tracks"][track]["vs_benchmark_buy_hold"])
                                                for o in done
                                                if o["tracks"][track]["vs_benchmark_buy_hold"] is not None]),
            }
        out["horizons"][h] = {
            "outcomes": len(rows), "scored": len(scored),
            "unscorable": sum(1 for o in rows if o["forecast"].get("status") == "unscorable"),
            "excess_return": _stat(excess),
            "brier": round(b, 6) if b is not None else None,
            "brier_skill_vs_half": round(1 - b / 0.25, 6) if b is not None else None,
            "brier_skill_vs_lagged_base_rate": round(1 - b / bb, 6) if b is not None and bb else None,
            "tracks": tracks,
        }
    primary = out["horizons"].get(PRIMARY, {})
    matured = primary.get("scored", 0)
    tickers = (primary.get("excess_return") or {}).get("tickers", 0)
    reached = [p for p in protocol.get("review_points", [])
               if matured >= p["matured_primary_forecasts"] and tickers >= p["min_tickers"]]
    out["review"] = {
        "matured_primary_forecasts": matured, "tickers": tickers,
        "review_point_reached": reached[-1] if reached else None,
        "verdict": reached[-1]["allowed_claims"] if reached else protocol.get(
            "default_verdict", "inconclusive"),
    }
    return out


def render(rep: dict) -> str:
    lines = ["# MarketMind outcome report", "",
             f"Sealed decisions: {rep['decisions_sealed']}; outcome records: {rep['outcomes']}.",
             f"Review status: {rep['review']['matured_primary_forecasts']} matured "
             f"{PRIMARY}-session forecasts across {rep['review']['tickers']} tickers — "
             f"**{rep['review']['verdict']}**", ""]
    for h, hz in rep["horizons"].items():
        ex = hz["excess_return"]
        lines += [f"## {h} sessions", "",
                  f"- scored {hz['scored']} / {hz['outcomes']} (unscorable {hz['unscorable']})",
                  f"- excess return, ticker-weighted: {ex['mean']} CI95 {ex['ci95']} "
                  f"({ex['calls']} calls, {ex['tickers']} tickers)",
                  f"- Brier {hz['brier']}; skill vs 0.5 {hz['brier_skill_vs_half']}; "
                  f"vs lagged base rate {hz['brier_skill_vs_lagged_base_rate']}"]
        for track, t in hz["tracks"].items():
            lines.append(f"- {track}: net {t['net_return']['mean']} CI95 {t['net_return']['ci95']}; "
                         f"vs do-nothing {t['vs_do_nothing']['mean']} CI95 {t['vs_do_nothing']['ci95']}")
        lines.append("")
    return "\n".join(lines)

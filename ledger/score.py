"""Score sealed decisions against what happened, under ledger/protocol_v1.json.

Pure functions over daily bars (``{"Date","Open","High","Low","Close","Volume"}``
rows, ascending) plus a runner that fetches prices from approved sources,
archives them with hashes, and appends one ``outcome`` record per decision and
matured horizon.

Forecast: reference = the first session close strictly after ``issued_at``;
sessions are the benchmark index's own trading days; the endpoint is ``h``
sessions later. A missing stock or benchmark close at either end makes the
outcome unscorable — never forward-filled.

Action tracks (from flat and from fully invested): the trade happens at the
first eligible open after ``issued_at`` (CN: not at an open locked at the price
limit; suspended days skipped), the position is marked to each close up to the
forecast endpoint, a stop exits at the next eligible open after a close below
its frozen level. Costs come from ledger/fees.yaml; dividends are not included
in v1 (flagged on every outcome).
"""
from __future__ import annotations

import math
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

FEES_FILE = Path(__file__).resolve().parent / "fees.yaml"
HORIZONS = (5, 20, 60)

TZ = {"CN": ZoneInfo("Asia/Shanghai"), "HK": ZoneInfo("Asia/Hong_Kong"),
      "US": ZoneInfo("America/New_York")}
CLOSE = {"CN": time(15, 0), "HK": time(16, 0), "US": time(16, 0)}
OPEN = time(9, 30)


def load_fees() -> dict:
    return yaml.safe_load(FEES_FILE.read_text(encoding="utf-8"))


def _at(date: str, t: time, market: str) -> datetime:
    return datetime.combine(datetime.fromisoformat(date).date(), t, TZ[market])


def _issued(body: dict) -> datetime:
    return datetime.fromisoformat(body["issued_at"]).astimezone(timezone.utc)


def _closes(rows: list[dict]) -> dict[str, float]:
    return {r["Date"]: float(r["Close"]) for r in rows if r.get("Close") is not None}


def reference_index(sessions: list[str], issued_at: datetime, market: str) -> int | None:
    for i, d in enumerate(sessions):
        if _at(d, CLOSE[market], market) > issued_at:
            return i
    return None


# ── forecast ─────────────────────────────────────────────────────────

def score_forecast(body: dict, stock: list[dict], bench: list[dict], h: int) -> dict:
    market = body["market"] if body["market"] in TZ else "US"
    sessions = [r["Date"] for r in bench if r.get("Close") is not None]
    i0 = reference_index(sessions, _issued(body), market)
    if i0 is None or i0 + h >= len(sessions):
        return {"status": "immature"}
    d0, de = sessions[i0], sessions[i0 + h]
    s, b = _closes(stock), _closes(bench)
    if d0 not in s or de not in s:
        missing = d0 if d0 not in s else de
        return {"status": "unscorable", "reason": f"no stock close on {missing} (suspended or missing)",
                "reference_date": d0, "endpoint_date": de}
    stock_ret = s[de] / s[d0] - 1
    bench_ret = b[de] / b[d0] - 1
    excess = stock_ret - bench_ret
    out = {"status": "scored", "reference_date": d0, "endpoint_date": de,
           "stock_return": round(stock_ret, 6), "benchmark_return": round(bench_ret, 6),
           "excess_return": round(excess, 6), "outperformed": excess > 0}
    p = (body.get("forecast") or {}).get("p_outperform")
    if isinstance(p, (int, float)) and not isinstance(p, bool):
        y = 1.0 if excess > 0 else 0.0
        q = min(max(float(p), 1e-6), 1 - 1e-6)
        out.update({"p_outperform": p, "brier": round((p - y) ** 2, 6),
                    "log_loss": round(-(y * math.log(q) + (1 - y) * math.log(1 - q)), 6)})
    quant = (body.get("forecast") or {}).get("excess_quantiles")
    if isinstance(quant, dict) and all(isinstance(quant.get(k), (int, float)) for k in ("p10", "p90")):
        out["inside_p10_p90"] = quant["p10"] <= excess <= quant["p90"]
    return out


# ── helpers for the action tracks ────────────────────────────────────

def atr14(rows: list[dict], upto: str) -> float | None:
    """Mean true range of the 14 bars on/before ``upto``."""
    bars = [r for r in rows if r["Date"] <= upto and None not in (r.get("High"), r.get("Low"), r.get("Close"))]
    if len(bars) < 15:
        return None
    trs = []
    for prev, cur in zip(bars[-15:-1], bars[-14:]):
        trs.append(max(cur["High"] - cur["Low"], abs(cur["High"] - prev["Close"]),
                       abs(cur["Low"] - prev["Close"])))
    return sum(trs) / len(trs)


def beta60(stock: list[dict], ref: list[dict], upto: str) -> float | None:
    s, r = _closes(stock), _closes(ref)
    dates = sorted(d for d in s if d in r and d <= upto)[-61:]
    if len(dates) < 41:
        return None
    sr = [s[b] / s[a] - 1 for a, b in zip(dates, dates[1:])]
    rr = [r[b] / r[a] - 1 for a, b in zip(dates, dates[1:])]
    mr = sum(rr) / len(rr)
    var = sum((x - mr) ** 2 for x in rr)
    if var <= 0:
        return None
    ms = sum(sr) / len(sr)
    return sum((x - ms) * (y - mr) for x, y in zip(sr, rr)) / var


def board_limit(symbol: str, fees: dict) -> float:
    code = symbol.split(".")[0][-6:]
    limits = fees["CN"]["price_limit"]
    if code[:3] in ("300", "301", "688", "689"):
        return limits["chinext_star"]
    if code[0] in "48" or symbol.upper().endswith(".BJ"):
        return limits["beijing"]
    return limits["main"]


def _locked(bar: dict, prev_close: float | None, side: str, limit: float) -> bool:
    """CN: a buy cannot fill at an open locked limit-up, a sell at limit-down."""
    if prev_close is None or bar.get("Open") is None:
        return False
    up = round(prev_close * (1 + limit), 2)
    down = round(prev_close * (1 - limit), 2)
    return bar["Open"] >= up - 0.005 if side == "buy" else bar["Open"] <= down + 0.005


def trade_cost(market: str, side: str, value: float, price: float, fees: dict) -> tuple[float, list[str]]:
    """Cost of trading ``value`` (fraction of portfolio) at ``price``."""
    if value <= 0:
        return 0.0, []
    cfg = fees[market]
    cost = value * (cfg["assumptions"]["commission"] + cfg["assumptions"]["slippage"])
    unverified = []
    for item in cfg["statutory"]:
        if item["side"] not in (side, "both"):
            continue
        if item.get("per_share"):
            cost += value / price * item["rate"] if price else 0.0
        else:
            cost += value * item["rate"]
        if not item.get("verified"):
            unverified.append(item["name"])
    return cost, unverified


# ── action tracks ────────────────────────────────────────────────────

def target_exposure(track: str, label: str) -> float:
    if track == "if_flat":
        return 1.0 if label == "BUY" else 0.0
    return 0.0 if label == "SELL" else 1.0


def simulate_track(body: dict, track: str, stock: list[dict], bench: list[dict], h: int,
                   fees: dict, hedge: list[dict] | None = None) -> dict:
    market = body["market"] if body["market"] in TZ else "US"
    action = (body.get("actions") or {}).get(track) or {}
    label, overlay = action.get("label"), action.get("overlay") or "none"
    e0 = 0.0 if track == "if_flat" else 1.0
    flags = ["dividends_not_included"]
    unverified: set[str] = set()

    sessions = [r["Date"] for r in bench if r.get("Close") is not None]
    issued = _issued(body)
    i0 = reference_index(sessions, issued, market)
    if label not in ("BUY", "HOLD", "SELL"):
        return {"status": "unscorable", "reason": f"no {track} action"}
    if i0 is None or i0 + h >= len(sessions):
        return {"status": "immature"}
    d0, de = sessions[i0], sessions[i0 + h]
    bars = [r for r in stock if r.get("Close") is not None and r["Date"] <= de]
    closes = _closes(stock)
    if d0 not in closes:
        return {"status": "unscorable", "reason": f"no stock close on reference {d0}"}

    e1 = target_exposure(track, label)
    hedge_ratio = 0.0
    stop_level = None
    if e1 > 0 and overlay == "trim":
        e1 *= 0.5
    elif e1 > 0 and overlay == "hedge":
        ref_rows = bench if market == "CN" else (hedge or [])
        b = beta60(stock, ref_rows, d0)
        if b is None:
            flags.append("hedge_unavailable_beta")
        elif market == "CN":
            e1 *= 1 - 0.5 * min(max(b, 0.0), 1.0)
            flags.append("hedge_mapped_to_trim")
        else:
            hedge_ratio = min(max(b, 0.0), fees["hedge"]["max_ratio"])
    elif e1 > 0 and overlay == "stop":
        atr = atr14(stock, d0)
        if atr is None:
            flags.append("stop_unavailable_atr")
        else:
            stop_level = closes[d0] - 2 * atr

    limit = board_limit(body.get("symbol", ""), fees) if market == "CN" else None
    side = "buy" if e1 > e0 else ("sell" if e1 < e0 else None)

    # first eligible open after issuance (skipping limit-locked opens in CN)
    j = None
    prev_close = None
    for k, bar in enumerate(bars):
        if _at(bar["Date"], OPEN, market) <= issued:
            prev_close = bar["Close"]
            continue
        if side and market == "CN" and _locked(bar, prev_close, side, limit):
            flags.append(f"no_fill_limit_locked_{bar['Date']}")
            prev_close = bar["Close"]
            continue
        if bar.get("Open") is None:
            prev_close = bar["Close"]
            continue
        j = k
        break
    if j is None:
        return {"status": "unscorable", "reason": "no eligible fill before the horizon end",
                "flags": flags}

    p_open = bars[j]["Open"]
    cost, unv = trade_cost(market, side or "buy", abs(e1 - e0), p_open, fees) if side else (0.0, [])
    unverified.update(unv)
    hedge_open = None
    if hedge_ratio > 0 and hedge:
        hmap = {r["Date"]: r for r in hedge}
        hb = hmap.get(bars[j]["Date"])
        if hb is None or hb.get("Open") is None:
            flags.append("hedge_unavailable_prices")
            hedge_ratio = 0.0
        else:
            hedge_open = hb["Open"]
            hc, unv = trade_cost(market, "sell", hedge_ratio * e1, hedge_open, fees)
            cost += hc
            unverified.update(unv)

    units = e1 / p_open            # stock units per unit of starting value
    cash = (1.0 - e1) - cost       # starting value 1 at the fill open
    hmap = {r["Date"]: r for r in (hedge or [])}
    borrow_daily = fees["hedge"]["borrow_fee_annual"] / 252
    values, exit_on, pending_stop = [1.0 - cost], None, False
    for k in range(j, len(bars)):
        bar = bars[k]
        if pending_stop and units > 0:
            if market == "CN" and _locked(bar, bars[k - 1]["Close"], "sell", limit):
                flags.append(f"stop_no_fill_limit_locked_{bar['Date']}")
            elif bar.get("Open") is not None:
                proceeds = units * bar["Open"]
                c, unv = trade_cost(market, "sell", proceeds, bar["Open"], fees)
                unverified.update(unv)
                cash += proceeds - c
                units, exit_on, pending_stop = 0.0, bar["Date"], False
        hedge_pnl = 0.0
        if hedge_ratio > 0 and hedge_open:
            hc = (hmap.get(bar["Date"]) or {}).get("Close")
            if hc is not None:
                hedge_pnl = -hedge_ratio * e1 * (hc / hedge_open - 1)
            cash -= hedge_ratio * e1 * borrow_daily
        values.append(cash + units * bar["Close"] + hedge_pnl)
        if stop_level is not None and units > 0 and exit_on is None and bar["Close"] < stop_level:
            pending_stop = True
    end_value = values[-1]
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)

    s_bh = closes[de] / p_open - 1 if de in closes else None
    bench_closes = _closes(bench)
    prior = [d for d in sessions if d < bars[j]["Date"]]
    b_bh = bench_closes[de] / bench_closes[prior[-1]] - 1 if prior else None
    net = end_value - 1
    if units > 0 and exit_on is None and stop_level is not None:
        flags.append("stop_not_triggered")
    return {
        "status": "scored", "label": label, "overlay": overlay,
        "exposure_before": e0, "exposure_after": round(e1, 4), "hedge_ratio": round(hedge_ratio, 4),
        "stop_level": round(stop_level, 4) if stop_level is not None else None,
        "filled_on": bars[j]["Date"], "fill_price": p_open, "stopped_out_on": exit_on,
        "endpoint_date": de, "net_return": round(net, 6), "max_drawdown": round(mdd, 6),
        "costs": round(cost, 6),
        "vs_stock_buy_hold": round(net - s_bh, 6) if s_bh is not None else None,
        "vs_benchmark_buy_hold": round(net - b_bh, 6) if b_bh is not None else None,
        "vs_do_nothing": round(net - (0.0 if track == "if_flat" else (s_bh or 0.0)), 6),
        "flags": flags, "unverified_fee_items": sorted(unverified),
    }


# ── runner ───────────────────────────────────────────────────────────

def _scored_keys(records: list[dict]) -> set[tuple[str, int]]:
    return {(r["body"]["decision_id"], r["body"]["horizon_sessions"])
            for r in records if r["type"] == "outcome"}


def score_all(root: Path, fetch=None, horizons=HORIZONS, now: datetime | None = None) -> list[dict]:
    """Append an outcome record for every sealed decision x matured horizon not
    yet scored. ``fetch(symbol, since) -> (rows, provenance)`` is injectable."""
    from . import prices, store

    fetch = fetch or prices.fetch
    now = now or datetime.now(timezone.utc)
    fees = load_fees()
    records = store.read(root)
    done = _scored_keys(records)
    appended = []
    for rec in records:
        if rec["type"] != "decision":
            continue
        body = rec["body"]
        todo = [h for h in horizons if (body["decision_id"], h) not in done]
        if not todo:
            continue
        # h sessions after the reference close take at least h calendar days, so
        # a decision younger than its shortest open horizon cannot have matured:
        # skip it instead of fetching four price series for nothing.
        if (now - _issued(body)).days < min(todo):
            continue
        market = body["market"] if body["market"] in TZ else "US"
        since = body["issued_at"]
        try:
            stock, sprov = fetch(body["symbol"], since)
            bench, bprov = fetch(prices.BENCHMARKS[market], since)
            hedge, hprov = (fetch(prices.HEDGES[market], since)
                            if market in prices.HEDGES else ([], None))
            cross = fetch("SPY", since) if market == "US" else None
        except Exception as exc:  # noqa: BLE001 - try again on the next run
            print(f"score: {body['decision_id']}: price fetch failed ({exc})")
            continue
        shas = {"stock": prices.archive(root, "stock", body["symbol"], stock, sprov),
                "benchmark": prices.archive(root, "benchmark", prices.BENCHMARKS[market], bench, bprov)}
        if hedge:
            shas["hedge"] = prices.archive(root, "hedge", prices.HEDGES[market], hedge, hprov)
        for h in todo:
            forecast = score_forecast(body, stock, bench, h)
            if forecast["status"] == "immature":
                continue
            outcome = {
                "decision_id": body["decision_id"], "decision_seq": rec["seq"],
                "ticker": body["ticker"], "market": market, "horizon_sessions": h,
                "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "forecast": forecast,
                "tracks": {t: simulate_track(body, t, stock, bench, h, fees, hedge)
                           for t in ("if_flat", "if_long")},
                "price_sha256": shas, "protocol_version": 1, "fees_checked_on": fees["checked_on"],
            }
            if cross:
                outcome["cross_check_spy"] = score_forecast(body, stock, cross[0], h)
            appended.append(store.append(root, "outcome", outcome))
    return appended

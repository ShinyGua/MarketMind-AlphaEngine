#!/usr/bin/env python3
"""Deterministic volume & chip-structure engine for MarketMind-AlphaEngine.

Computes, from daily OHLCV, the closest reproducible proxy for the buying and
selling force behind price — the layer MA crossovers were pretending to be:

- volume regime: volume MAs (5/10/20/60), 量比 (daily proxy), up-day vs
  down-day volume, OBV trend + price divergence, CMF(20), turnover when float
  is known
- chip distribution (VPVR proxy): volume-at-price histogram over ~120d →
  main/secondary peak, 90% cost band, concentration, profit vs trapped ratio,
  volume-weighted average cost
- support/resistance: high-volume price nodes + recent swing pivots, each with
  a strength score
- platform/box detection: base range, width, streak, breakout with/without
  volume confirmation

Writes {workspace}/quant/{date}/chip_structure.json.

DIRECTIONAL CONTRACT (`usage: "directional"`): unlike macro (context), the
intraday block (timing_only) and trend_regime (context_only), this artifact is
first-class directional evidence — analysts and panelists MAY ground a stance
or vote in it, exactly as they would in fundamentals. Deliberately asymmetric
with the other deterministic layers.

CN flow enrichment: if scripts/collect_cn_chips.py has written
{workspace}/raw/{date}/chips/cn_flows.json (主力资金/北向/融资/龙虎榜/股东户数/解禁),
it is embedded verbatim under `cn_flows` with its per-block data_quality.

Self-degrading: missing profile, no network AND no raw CSV, or fewer than 60
daily bars → `available: false` artifact, exit 0. Never fails the pipeline.

Usage:
    .venv/bin/python3 scripts/compute_chip_structure.py <workspace> <date>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from shared import contracts  # noqa: E402

DEFAULTS = {
    "enabled": True,
    "history_period": "400d",     # yfinance fetch window (need 250d + buffer)
    "chip_window_days": 120,      # VPVR window
    "histogram_bins": 40,
    "swing_pivot_window": 5,
    "platform_window_days": 60,
    "platform_max_width_pct": 18.0,
    "breakout_volume_mult": 1.5,  # 5d avg vol vs base avg vol → volume-confirmed
    "volume_ratio_expanding": 1.5,
    "volume_ratio_contracting": 0.6,
    "min_rows": 60,
}

_MIN_ROWS_HARD = 30  # below this, even the volume block is meaningless


# ── data loading ─────────────────────────────────────────────────────

def fetch_daily(symbol: str, period: str, run_date: str):
    """Daily OHLCV via yfinance, trimmed to run_date. None on any failure."""
    try:
        import pandas as pd
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period=period, interval="1d")
    except Exception:
        return None
    if hist is None or getattr(hist, "empty", True) or "Close" not in hist:
        return None
    hist = hist.dropna(subset=["Close"])
    if hasattr(hist.index, "tz") and hist.index.tz is not None:
        hist.index = hist.index.tz_localize(None)
    try:
        hist = hist[hist.index <= pd.Timestamp(run_date)]
    except Exception:
        pass
    return hist if not hist.empty else None


def load_raw_csv(workspace: Path, date: str, ticker_dir: str):
    """Fallback: the company desk's raw price CSV (no network needed)."""
    import pandas as pd

    prices = workspace / "raw" / date / "prices"
    if not prices.is_dir():
        prices = workspace / "raw" / "prices"
    for name in (f"{ticker_dir}_3mo.csv", f"{ticker_dir}_medium.csv"):
        path = prices / name
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if "Date" not in df.columns or "Close" not in df.columns:
            continue
        try:
            idx = pd.to_datetime(df["Date"], utc=True).dt.tz_localize(None)
        except (ValueError, TypeError):
            try:
                idx = pd.to_datetime(df["Date"])
            except Exception:
                continue
        df.index = pd.DatetimeIndex(idx).normalize()
        df = df.drop(columns=["Date"]).dropna(subset=["Close"])
        # Suspension days come through as zero-volume flat bars; they distort
        # every volume statistic, so drop them here once.
        if "Volume" in df.columns:
            df = df[df["Volume"] > 0]
        if not df.empty:
            return df
    return None


# ── volume block ─────────────────────────────────────────────────────

def _round(x, n=2):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def compute_volume_block(df, cfg, float_shares):
    import numpy as np

    close = df["Close"]
    vol = df["Volume"].astype(float)
    out = {"vol_ma": {}, "inputs_missing": []}
    for w in (5, 10, 20, 60):
        out["vol_ma"][str(w)] = _round(vol.tail(w).mean(), 0) if len(vol) >= w else None

    # 量比 (daily proxy): today's volume vs mean of the PRIOR 5 sessions
    if len(vol) >= 6 and vol.iloc[-6:-1].mean() > 0:
        vr = float(vol.iloc[-1] / vol.iloc[-6:-1].mean())
        out["volume_ratio"] = _round(vr, 2)
        if vr >= cfg["volume_ratio_expanding"]:
            out["volume_regime"] = "expanding"
        elif vr <= cfg["volume_ratio_contracting"]:
            out["volume_regime"] = "contracting"
        else:
            out["volume_regime"] = "normal"
    else:
        out["volume_ratio"] = None
        out["volume_regime"] = "unknown"

    # up-day vs down-day volume over 20d — who is pressing, buyers or sellers
    tail = df.tail(21)
    chg = tail["Close"].diff().dropna()
    v_tail = tail["Volume"].astype(float).iloc[1:]
    up_vol = v_tail[chg.values > 0]
    dn_vol = v_tail[chg.values < 0]
    if len(up_vol) and len(dn_vol) and dn_vol.mean() > 0:
        out["up_down_volume_ratio_20d"] = _round(up_vol.mean() / dn_vol.mean(), 2)
    else:
        out["up_down_volume_ratio_20d"] = None

    # OBV trend + divergence vs price over 20d
    direction = np.sign(close.diff().fillna(0.0).values)
    obv = (direction * vol.values).cumsum()
    if len(obv) >= 21:
        obv_chg = obv[-1] - obv[-21]
        obv_scale = np.abs(np.diff(obv[-21:])).sum() or 1.0
        obv_norm = obv_chg / obv_scale
        out["obv_trend"] = ("rising" if obv_norm > 0.15
                            else "falling" if obv_norm < -0.15 else "flat")
        price_chg = float(close.iloc[-1] - close.iloc[-21])
        if price_chg > 0 and out["obv_trend"] == "falling":
            out["obv_price_divergence"] = "bearish"   # price up on distribution
        elif price_chg < 0 and out["obv_trend"] == "rising":
            out["obv_price_divergence"] = "bullish"   # price down on accumulation
        else:
            out["obv_price_divergence"] = "none"
    else:
        out["obv_trend"] = "unknown"
        out["obv_price_divergence"] = "none"

    # CMF(20) — close-in-range weighted volume (active buy/sell proxy)
    t20 = df.tail(20)
    hl = (t20["High"] - t20["Low"]).replace(0, np.nan)
    mfm = ((t20["Close"] - t20["Low"]) - (t20["High"] - t20["Close"])) / hl
    mfv = (mfm * t20["Volume"]).sum()
    denom = float(t20["Volume"].sum())
    out["cmf_20"] = _round(mfv / denom, 3) if denom > 0 else None

    # turnover (换手率) — needs float
    if float_shares and float_shares > 0:
        out["turnover"] = {
            "turnover_pct": _round(vol.iloc[-1] / float_shares * 100, 2),
            "avg_20d_pct": _round(vol.tail(20).mean() / float_shares * 100, 2)
            if len(vol) >= 20 else None,
            "source": "float_shares",
        }
    else:
        out["turnover"] = {"turnover_pct": None, "avg_20d_pct": None, "source": None}
        out["inputs_missing"].append("float_shares (turnover unavailable)")
    return out


# ── chip distribution (VPVR proxy) ───────────────────────────────────

def compute_chip_distribution(df, cfg):
    import numpy as np

    window = min(int(cfg["chip_window_days"]), len(df))
    tail = df.tail(window)
    # typical price per bar, weighted by that bar's volume
    price = (tail["High"] + tail["Low"] + tail["Close"]) / 3.0
    vol = tail["Volume"].astype(float).values
    prices = price.values
    total = vol.sum()
    if total <= 0 or len(prices) < 20:
        return None
    bins = int(cfg["histogram_bins"])
    hist, edges = np.histogram(prices, bins=bins, weights=vol)
    centers = (edges[:-1] + edges[1:]) / 2.0
    order = np.argsort(hist)[::-1]
    main_peak = float(centers[order[0]])
    secondary = None
    for i in order[1:]:
        if abs(centers[i] - main_peak) > (edges[-1] - edges[0]) / bins * 2:
            secondary = float(centers[i])
            break

    # volume-weighted percentiles for the 90% cost band
    csum = np.cumsum(hist) / hist.sum()
    p05 = float(centers[np.searchsorted(csum, 0.05)])
    p95 = float(centers[min(np.searchsorted(csum, 0.95), bins - 1)])
    mid = (p05 + p95) / 2.0
    concentration = (p95 - p05) / mid if mid > 0 else None

    last_close = float(df["Close"].iloc[-1])
    profit_ratio = float(hist[centers < last_close].sum() / hist.sum())
    avg_cost = float(np.average(prices, weights=vol))

    return {
        "method": "volume_at_price_histogram",
        "window_days": window,
        "main_peak_price": _round(main_peak),
        "secondary_peak_price": _round(secondary),
        "cost_band_90": [_round(p05), _round(p95)],
        "concentration": _round(concentration, 3),
        "profit_ratio": _round(profit_ratio, 3),
        "trapped_ratio": _round(1.0 - profit_ratio, 3),
        "avg_cost_vwap": _round(avg_cost),
        "price_vs_avg_cost_pct": _round((last_close - avg_cost) / avg_cost * 100, 2)
        if avg_cost > 0 else None,
        "histogram": {
            "centers": [_round(c) for c in centers.tolist()],
            "volume_share": [_round(v / hist.sum(), 4) for v in hist.tolist()],
        },
    }


# ── support / resistance ─────────────────────────────────────────────

def _swing_pivots(df, window):
    """Confirmed swing highs/lows (strict local extrema over ±window bars)."""
    highs, lows = [], []
    h, l = df["High"].values, df["Low"].values
    for i in range(window, len(df) - window):
        seg_h = h[i - window: i + window + 1]
        seg_l = l[i - window: i + window + 1]
        if h[i] == seg_h.max() and (seg_h == h[i]).sum() == 1:
            highs.append(float(h[i]))
        if l[i] == seg_l.min() and (seg_l == l[i]).sum() == 1:
            lows.append(float(l[i]))
    return highs, lows


def compute_support_resistance(df, chip, cfg):
    last_close = float(df["Close"].iloc[-1])
    supports, resistances = [], []

    # volume nodes from the chip histogram — top bins by traded volume
    if chip and chip.get("histogram"):
        centers = chip["histogram"]["centers"]
        shares = chip["histogram"]["volume_share"]
        ranked = sorted(zip(centers, shares), key=lambda cs: -cs[1])[:6]
        for price, share in ranked:
            if price is None:
                continue
            entry = {"price": price, "strength": round(share, 4), "basis": "volume_node"}
            (supports if price < last_close else resistances).append(entry)

    # recent swing pivots (structure levels)
    tail = df.tail(min(len(df), int(cfg["chip_window_days"])))
    highs, lows = _swing_pivots(tail, int(cfg["swing_pivot_window"]))
    for price in lows[-2:]:
        if price < last_close:
            supports.append({"price": _round(price), "strength": None, "basis": "swing_low"})
    for price in highs[-2:]:
        if price > last_close:
            resistances.append({"price": _round(price), "strength": None, "basis": "swing_high"})

    # nearest-first, deduplicated within 1%
    def _dedup(levels, reverse):
        levels = sorted(levels, key=lambda x: x["price"], reverse=reverse)
        out = []
        for lvl in levels:
            if out and abs(lvl["price"] - out[-1]["price"]) / max(out[-1]["price"], 1e-9) < 0.01:
                if (lvl.get("strength") or 0) > (out[-1].get("strength") or 0):
                    out[-1] = lvl
                continue
            out.append(lvl)
        return out[:3]

    return {"supports": _dedup(supports, reverse=True),
            "resistances": _dedup(resistances, reverse=False)}


# ── platform / box detection ─────────────────────────────────────────

def compute_platform(df, cfg):
    n = len(df)
    base_win = int(cfg["platform_window_days"])
    if n < base_win + 10:
        return {"in_range": None, "reason": "insufficient_history"}
    base = df.iloc[-(base_win + 10):-10]
    range_low = float(base["Close"].min())
    range_high = float(base["Close"].max())
    mid = (range_low + range_high) / 2.0
    width_pct = (range_high - range_low) / mid * 100 if mid > 0 else None

    last_close = float(df["Close"].iloc[-1])
    breakout = "none"
    if last_close > range_high * 1.01:
        breakout = "up"
    elif last_close < range_low * 0.99:
        breakout = "down"
    vol5 = float(df["Volume"].tail(5).astype(float).mean())
    base_vol = float(base["Volume"].astype(float).mean())
    volume_confirmed = bool(base_vol > 0 and vol5 >= cfg["breakout_volume_mult"] * base_vol)

    # trailing streak of closes inside the base band (before the last 10 bars)
    days_in_range = 0
    closes = df["Close"].values
    for i in range(n - 11, -1, -1):
        if range_low * 0.99 <= closes[i] <= range_high * 1.01:
            days_in_range += 1
        else:
            break

    return {
        "in_range": bool(width_pct is not None
                         and width_pct <= cfg["platform_max_width_pct"]
                         and breakout == "none"),
        "range_low": _round(range_low),
        "range_high": _round(range_high),
        "range_width_pct": _round(width_pct, 1),
        "days_in_range": days_in_range,
        "breakout": {"direction": breakout,
                     "volume_confirmed": volume_confirmed if breakout != "none" else None},
    }


# ── bilingual note ───────────────────────────────────────────────────

_REGIME_CH = {"expanding": "放量", "contracting": "缩量", "normal": "量能平稳", "unknown": "量能不明"}
_REGIME_EN = {"expanding": "expanding volume", "contracting": "contracting volume",
              "normal": "normal volume", "unknown": "volume unknown"}


def build_note(volume, chip, sr):
    vr = volume.get("volume_ratio")
    regime = volume.get("volume_regime", "unknown")
    parts_en, parts_ch = [], []
    parts_en.append(f"{_REGIME_EN.get(regime, regime)}"
                    + (f" (ratio {vr})" if vr is not None else ""))
    parts_ch.append(f"{_REGIME_CH.get(regime, regime)}"
                    + (f"（量比{vr}）" if vr is not None else ""))
    if chip:
        pr = chip.get("profit_ratio")
        peak = chip.get("main_peak_price")
        conc = chip.get("concentration")
        if pr is not None:
            parts_en.append(f"{round(pr * 100)}% of {chip['window_days']}d volume in profit")
            parts_ch.append(f"获利盘约{round(pr * 100)}%")
        if peak is not None:
            parts_en.append(f"main chip peak {peak}")
            parts_ch.append(f"筹码主峰{peak}")
        if conc is not None:
            parts_en.append(f"concentration {conc}")
            parts_ch.append(f"集中度{conc}")
    sups = (sr or {}).get("supports") or []
    ress = (sr or {}).get("resistances") or []
    if sups:
        parts_en.append(f"support {sups[0]['price']}")
        parts_ch.append(f"支撑{sups[0]['price']}")
    if ress:
        parts_en.append(f"resistance {ress[0]['price']}")
        parts_ch.append(f"压力{ress[0]['price']}")
    return {"en": "; ".join(parts_en) + ".", "ch": "；".join(parts_ch) + "。"}


# ── assembly ─────────────────────────────────────────────────────────

def build(workspace: Path, date: str, cfg: dict) -> dict:
    symbol = contracts.resolve_yf_symbol(workspace)
    base = {
        "ticker": symbol,
        "date": date,
        "usage": "directional",
        "usage_note": ("First-class directional evidence: volume and chip structure "
                       "may ground a stance/vote on their own (unlike macro=context, "
                       "intraday=timing_only, trend_regime=context_only)."),
    }

    # Prefer the run-day raw CSV: it is the exact price series the rest of the
    # report was written from (a live re-fetch months later returns
    # dividend-adjusted history and would shift every chip level). Fetch from
    # yfinance only when the CSV is missing or too short for the chip window.
    df = load_raw_csv(workspace, date, Path(str(workspace)).name)
    source = "raw_csv"
    if df is None or len(df) < cfg["min_rows"]:
        fetched = fetch_daily(symbol, cfg["history_period"], date)
        if fetched is not None and (df is None or len(fetched) > len(df)):
            df, source = fetched, "yfinance"
    if df is None or len(df) < _MIN_ROWS_HARD or "Volume" not in df.columns:
        return {**base, "available": False,
                "reason": "no_daily_data" if df is None else "insufficient_history",
                "rows": 0 if df is None else int(len(df))}
    if float(df["Volume"].astype(float).sum()) <= 0:
        return {**base, "available": False, "reason": "no_volume_data",
                "rows": int(len(df))}
    # drop zero-volume suspension bars (they poison every volume statistic)
    df = df[df["Volume"].astype(float) > 0]

    float_shares = None
    float_source = None
    try:
        profile = json.loads((workspace / "profile" / "company_profile.json")
                             .read_text(encoding="utf-8"))
        for key in ("float_shares", "shares_outstanding"):
            val = profile.get(key)
            if isinstance(val, (int, float)) and val > 0:
                float_shares, float_source = float(val), key
                break
    except (OSError, ValueError):
        pass

    volume = compute_volume_block(df, cfg, float_shares)
    inputs_missing = volume.pop("inputs_missing", [])
    if float_source == "shares_outstanding":
        volume["turnover"]["source"] = "shares_outstanding (float unavailable — upper-bound proxy)"
    chip = compute_chip_distribution(df, cfg)
    if chip is None:
        inputs_missing.append("chip_distribution (volume histogram not computable)")
    sr = compute_support_resistance(df, chip, cfg)
    platform = compute_platform(df, cfg)

    # CN flow enrichment (written earlier by scripts/collect_cn_chips.py)
    cn_flows = None
    cn_path = workspace / "raw" / date / "chips" / "cn_flows.json"
    if cn_path.exists():
        try:
            cn_flows = json.loads(cn_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            inputs_missing.append("cn_flows (unreadable)")
    # official exchange turnover beats the float-based proxy when available
    cn_turn = (cn_flows or {}).get("turnover") or {}
    if cn_turn.get("data_quality") == "akshare" and cn_turn.get("turnover_pct") is not None:
        volume["turnover"] = {
            "turnover_pct": cn_turn["turnover_pct"],
            "avg_20d_pct": cn_turn.get("avg_20d_pct"),
            "source": "akshare_official",
        }

    artifact = {
        **base,
        "available": True,
        "source": source,
        "rows": int(len(df)),
        "last_close": _round(df["Close"].iloc[-1]),
        "volume": volume,
        "chip_distribution": chip,
        "support_resistance": sr,
        "platform": platform,
        "cn_flows": cn_flows,
        "inputs_missing": inputs_missing,
        "note": build_note(volume, chip, sr),
    }
    return artifact


def run(workspace: Path, date: str) -> dict:
    cfg = dict(DEFAULTS)
    try:
        rc = json.loads((workspace / "resolved_config.json").read_text(encoding="utf-8"))
        cfg.update((rc.get("quant") or {}).get("chips") or {})
    except (OSError, ValueError):
        pass

    if not cfg.get("enabled", True):
        artifact = {"available": False, "reason": "disabled", "usage": "directional"}
    else:
        artifact = build(Path(workspace), date, cfg)
    out = contracts.chip_structure_path(Path(workspace), date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"chip_structure: wrote {out} (available={artifact.get('available')}, "
          f"reason={artifact.get('reason', 'ok')})")
    return artifact


def main():
    if len(sys.argv) < 3:
        print("usage: compute_chip_structure.py <workspace> <date>")
        return
    try:
        run(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:  # advisory stage — never fail the pipeline
        print(f"chip_structure: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

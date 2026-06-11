#!/usr/bin/env python3
"""Deterministic 1h/4h intraday timing block for MarketMind-AlphaEngine.

Pulls ~90 days of 1h bars via yfinance, resamples to 4h, and computes RSI(14),
MACD(12,26,9), and recent swing levels. Writes
{workspace}/quant/{date}/intraday_timing.json.

TIMING-ONLY CONTRACT (`usage: "timing_only"`): this artifact frames staged
entry/exit price zones and risk-overlay language in the decision/report stages.
It must NEVER be cited as a reason to flip a BUY/HOLD/SELL vote or change
conviction — intraday momentum is noise at the research horizon.

Self-degrading: tickers without 1h coverage (common for .SS/.SZ names) or a
network failure produce an `available: false` artifact and exit 0; downstream
price zones fall back to daily ATR. The naive 4h resample splits US sessions
at fixed clock boundaries (not the 9:30 open) — acceptable for timing color,
not exchange-aligned bars.

Usage:
    .venv/bin/python3 scripts/intraday_timing.py <workspace> <date>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULTS = {
    "enabled": True,
    "period": "90d",
    "interval": "1h",
    "resample": "4h",
    "rsi_window": 14,
    "macd": [12, 26, 9],
    "swing_pivot_window": 5,
}

_EXTENDED_PROXIMITY = 0.02  # close within 2% of a swing extreme counts as "near"


def fetch_hourly(ticker: str, period: str = "90d", interval: str = "1h"):
    """1h OHLCV DataFrame or None — yfinance raises a zoo of types, never abort."""
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(period=period, interval=interval)
    except Exception:
        return None
    if hist is None or getattr(hist, "empty", True) or "Close" not in hist:
        return None
    # yfinance 1h data contains NaN closes (halts, partial bars); they would
    # serialize as invalid JSON (`NaN`) and silently corrupt the RSI/state.
    hist = hist.dropna(subset=["Close"])
    if hist.empty:
        return None
    return hist


def resample_4h(df, rule: str = "4h"):
    # closed="left": yfinance stamps bars at their OPEN, so a 12:00 bar covers
    # 12:00-13:00 and belongs to the bin starting at 12:00 (right-closed bins
    # would pull each bin's first bar into the previous bin).
    out = df.resample(rule, label="right", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    return out.dropna(subset=["Close"])


def rsi(close, n: int = 14) -> float | None:
    """Wilder RSI of the last bar. Guards: no losses -> 100, no gains -> 0, flat -> 50."""
    if close is None or len(close) < n + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / n, min_periods=n).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / n, min_periods=n).mean()
    g, l = float(gain.iloc[-1]), float(loss.iloc[-1])
    if l == 0.0:
        return 50.0 if g == 0.0 else 100.0
    if g == 0.0:
        return 0.0
    rs = g / l
    return round(100.0 - 100.0 / (1.0 + rs), 1)


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    if close is None or len(close) < slow + signal:
        return None
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    return {"macd": round(float(line.iloc[-1]), 4),
            "macd_signal": round(float(sig.iloc[-1]), 4),
            "macd_histogram": round(float(line.iloc[-1] - sig.iloc[-1]), 4)}


def swing_levels(df4h, pivot_window: int = 5) -> dict:
    """Last confirmed pivot high/low on 4h bars + 30-day range. Nulls when thin."""
    out = {"swing_high": None, "swing_high_at": None,
           "swing_low": None, "swing_low_at": None,
           "range_30d_high": None, "range_30d_low": None}
    if df4h is None or len(df4h) == 0:
        return out
    highs, lows = df4h["High"], df4h["Low"]
    w = pivot_window
    if len(df4h) >= 2 * w + 1:
        for i in range(len(df4h) - 1 - w, w - 1, -1):  # newest confirmed pivot first
            # Accept the most recent occurrence of the window extreme: bars
            # AFTER the pivot must be strictly below/above it, but an exact
            # double-top/bottom earlier in the window still confirms (strict
            # uniqueness would never detect flat tops at round-number levels).
            window = highs.iloc[i - w:i + w + 1]
            if out["swing_high"] is None and highs.iloc[i] == window.max() \
                    and (window.iloc[w + 1:] < highs.iloc[i]).all():
                out["swing_high"] = round(float(highs.iloc[i]), 2)
                out["swing_high_at"] = str(df4h.index[i])
            lwindow = lows.iloc[i - w:i + w + 1]
            if out["swing_low"] is None and lows.iloc[i] == lwindow.min() \
                    and (lwindow.iloc[w + 1:] > lows.iloc[i]).all():
                out["swing_low"] = round(float(lows.iloc[i]), 2)
                out["swing_low_at"] = str(df4h.index[i])
            if out["swing_high"] is not None and out["swing_low"] is not None:
                break
    import pandas as pd

    tail = df4h[df4h.index >= df4h.index[-1] - pd.Timedelta(days=30)]
    if len(tail):
        out["range_30d_high"] = round(float(tail["High"].max()), 2)
        out["range_30d_low"] = round(float(tail["Low"].min()), 2)
    return out


def timing_state(rsi_4h: float | None, close: float | None, levels: dict) -> str:
    if rsi_4h is None or close is None:
        return "unknown"
    high, low = levels.get("swing_high"), levels.get("swing_low")
    if rsi_4h >= 70:
        if high and close >= high * (1 - _EXTENDED_PROXIMITY):
            return "extended_overbought"
        return "overbought"
    if rsi_4h <= 30:
        if low and close <= low * (1 + _EXTENDED_PROXIMITY):
            return "oversold_near_support"
        return "oversold"
    return "neutral"


_NOTES = {
    "extended_overbought": {
        "en": "4h RSI {rsi:.1f} with price near the {high} swing high — extended; "
              "staged entries favored, first lot toward the lower zone.",
        "ch": "4小时RSI {rsi:.1f}，价格接近{high}前高——偏超买；宜分批建仓，首批靠近下方区间。"},
    "overbought": {
        "en": "4h RSI {rsi:.1f} — short-term overbought; patience on entry timing.",
        "ch": "4小时RSI {rsi:.1f}——短线超买；入场时点宜耐心等待。"},
    "oversold_near_support": {
        "en": "4h RSI {rsi:.1f} with price near the {low} swing low — oversold at "
              "support; a scheduled lot may execute toward this zone.",
        "ch": "4小时RSI {rsi:.1f}，价格接近{low}前低——支撑位附近超卖；计划内批次可在该区间附近执行。"},
    "oversold": {
        "en": "4h RSI {rsi:.1f} — short-term oversold.",
        "ch": "4小时RSI {rsi:.1f}——短线超卖。"},
    "neutral": {
        "en": "Intraday momentum neutral (4h RSI {rsi:.1f}); no timing adjustment.",
        "ch": "盘中动能中性（4小时RSI {rsi:.1f}）；无需调整入场时点。"},
    "unknown": {"en": "Intraday indicators incomplete.", "ch": "盘中指标不完整。"},
}


def _unavailable(ticker: str, reason: str) -> dict:
    return {"schema_version": 1, "ticker": ticker, "available": False,
            "reason": reason, "usage": "timing_only"}


def build(ticker: str, cfg: dict) -> dict:
    if not cfg.get("enabled", True):
        return _unavailable(ticker, "disabled")
    df1h = fetch_hourly(ticker, cfg["period"], cfg["interval"])
    if df1h is None:
        return _unavailable(ticker, "no_intraday_data")

    df4h = resample_4h(df1h, cfg["resample"])
    n = cfg["rsi_window"]
    fast, slow, signal = cfg["macd"]
    tf = {}
    for label, frame in (("1h", df1h), ("4h", df4h)):
        r = rsi(frame["Close"], n)
        m = macd(frame["Close"], fast, slow, signal) or {}
        tf[label] = {"rsi_14": r, **m} if (r is not None or m) else {"rsi_14": None}

    levels = swing_levels(df4h, cfg["swing_pivot_window"])
    close = round(float(df1h["Close"].iloc[-1]), 2)
    state = timing_state(tf["4h"].get("rsi_14"), close, levels)
    note_fmt = {"rsi": tf["4h"].get("rsi_14") or 0.0,
                "high": levels.get("swing_high"), "low": levels.get("swing_low")}
    return {
        "schema_version": 1,
        "ticker": ticker,
        "available": True,
        "reason": None,
        "as_of": str(df1h.index[-1]),
        "bars_1h": int(len(df1h)),
        "source": "yfinance",
        "usage": "timing_only",
        "last_close": close,
        "timeframes": tf,
        "levels": levels,
        "timing_state": state,
        "note": {lang: _NOTES[state][lang].format(**note_fmt) for lang in ("en", "ch")},
    }


def run(workspace: Path, date: str) -> dict:
    ticker = os.path.basename(os.path.normpath(str(workspace)))
    cfg = dict(DEFAULTS)
    try:
        rc = json.loads((workspace / "resolved_config.json").read_text(encoding="utf-8"))
        cfg.update((rc.get("quant") or {}).get("intraday") or {})
    except (OSError, ValueError):
        pass

    artifact = build(ticker, cfg)
    out = workspace / "quant" / date / "intraday_timing.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"intraday_timing: wrote {out} (available={artifact['available']}, "
          f"state={artifact.get('timing_state', 'n/a')})")
    return artifact


def main():
    if len(sys.argv) < 3:
        print("usage: intraday_timing.py <workspace> <date>")
        return
    try:
        run(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:  # advisory stage — never fail the pipeline
        print(f"intraday_timing: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

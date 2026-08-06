#!/usr/bin/env python3
"""Deterministic macro regime computation for MarketMind-AlphaEngine.

Pure post-processing (no network, no LLM) over the CSVs written by
scripts/collect_macro_series.py. Classifies the rate trend, yield-curve slope,
inflation trend, Fed policy stance, VIX percentile, USD trend, and credit-spread
regime into workspaces/shared/market_context/{date}/indicators/macro_regime.json.

Every block carries `data_quality` ("fred" | "proxy" | "missing" |
"insufficient_history"); the artifact is always written (worst case an
all-missing skeleton with a populated `inputs_missing` list) and the script
always exits 0. Rates and spreads are converted from FRED percent units to
decimals in the JSON (4.21 -> 0.0421). The summary is emitted in BOTH en and ch
because the artifact is shared across same-day workspaces with different
language configs.

Usage:
    .venv/bin/python3 scripts/compute_macro_regime.py <workspace> <date>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_THRESHOLDS = {
    "rate_stable_band": 0.0025,      # |3m yield delta| below -> "stable"
    "curve_flat_band": 0.0025,       # 0 <= 2s10s < band -> "flat"
    "vix_percentile_window": 252,
    "vix_labels": {"calm": 0.30, "normal": 0.70, "elevated": 0.90},
    "hy_regime": {"tight": 0.030, "normal": 0.045, "wide": 0.060},
    "policy_band": 0.00125,          # |3m fedfunds delta| below -> "on_hold"
    "usd_trend_band_pct": 2.0,       # |3m % change| below -> "stable"
    "inflation_band": 0.002,         # |3m-annualized - YoY| below -> "stable"
}

_MIN_VIX_OBS = 60
_D1M, _D3M = 21, 63  # trading-day offsets for daily series


def read_series(path: Path) -> list[tuple[str, float]]:
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()[1:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            d, v = line.split(",", 1)
            rows.append((d, float(v)))
        except ValueError:
            continue
    return rows


def _delta(values: list[float], n: int) -> float | None:
    if len(values) <= n:
        return None
    return round(values[-1] - values[-1 - n], 6)


def _pct_change(values: list[float], n: int) -> float | None:
    if len(values) <= n or values[-1 - n] == 0:
        return None
    return round((values[-1] / values[-1 - n] - 1.0) * 100.0, 2)


def rate_trend(values: list[float], stable_band: float) -> dict:
    """Daily yields in decimal. Direction from the 3m delta vs the stable band."""
    if not values:
        return {"value": None, "delta_1m": None, "delta_3m": None, "direction": None}
    d1, d3 = _delta(values, _D1M), _delta(values, _D3M)
    direction = None
    if d3 is not None:
        direction = "stable" if abs(d3) < stable_band else ("rising" if d3 > 0 else "falling")
    return {"value": round(values[-1], 6), "delta_1m": d1, "delta_3m": d3, "direction": direction}


def curve_slope(short_last: float | None, long_last: float | None, flat_band: float) -> dict:
    if short_last is None or long_last is None:
        return {"slope_2s10s": None, "label": None}
    slope = round(long_last - short_last, 6)
    label = "inverted" if slope < 0 else ("flat" if slope < flat_band else "normal")
    return {"slope_2s10s": slope, "label": label}


def inflation_trend(cpi: list[float], core: list[float], band: float) -> dict:
    """Monthly index levels; needs >= 13 obs for YoY."""
    out = {"cpi_yoy": None, "core_cpi_yoy": None, "cpi_3m_annualized": None, "direction": None}
    if len(cpi) >= 13 and cpi[-13] > 0:
        out["cpi_yoy"] = round(cpi[-1] / cpi[-13] - 1.0, 4)
    if len(core) >= 13 and core[-13] > 0:
        out["core_cpi_yoy"] = round(core[-1] / core[-13] - 1.0, 4)
    if len(cpi) >= 4 and cpi[-4] > 0:
        out["cpi_3m_annualized"] = round((cpi[-1] / cpi[-4]) ** 4 - 1.0, 4)
    if out["cpi_yoy"] is not None and out["cpi_3m_annualized"] is not None:
        gap = out["cpi_3m_annualized"] - out["cpi_yoy"]
        out["direction"] = "stable" if abs(gap) < band else ("heating" if gap > 0 else "cooling")
    return out


def policy_stance(values: list[float], band: float) -> dict:
    """Monthly fed funds in decimal; 3m delta = 3 observations back."""
    if not values:
        return {"fedfunds": None, "delta_3m": None, "stance": None}
    d3 = _delta(values, 3)
    stance = None
    if d3 is not None:
        stance = "on_hold" if abs(d3) < band else ("tightening" if d3 > 0 else "easing")
    return {"fedfunds": round(values[-1], 6), "delta_3m": d3, "stance": stance}


def vix_percentile(values: list[float], window: int, labels: dict) -> dict:
    if not values:
        return {"vix": None, "percentile_1y": None, "label": None}
    out = {"vix": round(values[-1], 2), "percentile_1y": None, "label": None}
    tail = values[-window:]
    if len(tail) < _MIN_VIX_OBS:
        out["label"] = "insufficient_history"
        return out
    current = tail[-1]
    pct = sum(1 for v in tail if v <= current) / len(tail)
    out["percentile_1y"] = round(pct, 2)
    if pct < labels["calm"]:
        out["label"] = "calm"
    elif pct < labels["normal"]:
        out["label"] = "normal"
    elif pct < labels["elevated"]:
        out["label"] = "elevated"
    else:
        out["label"] = "stressed"
    return out


def usd_trend(values: list[float], band_pct: float) -> dict:
    if not values:
        return {"level": None, "chg_1m_pct": None, "chg_3m_pct": None, "direction": None}
    c1, c3 = _pct_change(values, _D1M), _pct_change(values, _D3M)
    direction = None
    if c3 is not None:
        direction = "stable" if abs(c3) < band_pct else ("strengthening" if c3 > 0 else "weakening")
    return {"level": round(values[-1], 2), "chg_1m_pct": c1, "chg_3m_pct": c3, "direction": direction}


def credit_regime(values: list[float], thresholds: dict) -> dict:
    if not values:
        return {"hy_spread": None, "delta_1m": None, "regime": None}
    spread = values[-1]
    if spread < thresholds["tight"]:
        regime = "tight"
    elif spread < thresholds["normal"]:
        regime = "normal"
    elif spread < thresholds["wide"]:
        regime = "wide"
    else:
        regime = "stressed"
    return {"hy_spread": round(spread, 6), "delta_1m": _delta(values, _D1M), "regime": regime}


_CH = {
    "rising": "上行", "falling": "下行", "stable": "企稳",
    "inverted": "倒挂", "flat": "平坦", "normal": "正常",
    "cooling": "回落", "heating": "升温",
    "easing": "宽松", "tightening": "紧缩", "on_hold": "按兵不动",
    "calm": "低位", "elevated": "偏高", "stressed": "高压",
    "tight": "收窄", "wide": "走阔",
    "strengthening": "走强", "weakening": "走弱",
}


def build_summary(regime: dict, lang: str | None = None):
    """Deterministic one-line summary from the available blocks.

    `lang=None` returns the bilingual `{"en": ..., "ch": ...}` CACHE — which is
    what this artifact stores, deliberately. macro_regime.json lives under
    workspaces/shared/ and every workspace's run overwrites it for the same
    date; with four `ch` and two `en` workspaces, a single-language file would
    take the language of whichever workspace ran last. The language boundary
    therefore sits at the per-workspace bundle (build_shared_context.py), not
    here. Pass an explicit lang to get one string.
    """
    en, ch = [], []
    r = regime["rates"]["us10y"]
    if r.get("value") is not None and r.get("direction"):
        en.append(f"10Y {r['value'] * 100:.2f}% {r['direction']}")
        ch.append(f"美债10年期{r['value'] * 100:.2f}%{_CH[r['direction']]}")
    c = regime["curve"]
    if c.get("label"):
        en.append(f"curve {c['label']}")
        ch.append(f"收益率曲线{_CH[c['label']]}")
    i = regime["inflation"]
    if i.get("cpi_yoy") is not None and i.get("direction"):
        en.append(f"CPI {i['cpi_yoy'] * 100:.1f}% {i['direction']}")
        ch.append(f"CPI同比{i['cpi_yoy'] * 100:.1f}%{_CH[i['direction']]}")
    p = regime["policy"]
    if p.get("stance"):
        en.append(f"Fed {p['stance'].replace('_', ' ')}")
        ch.append(f"联储{_CH[p['stance']]}")
    v = regime["volatility"]
    if v.get("percentile_1y") is not None:
        en.append(f"VIX {v['percentile_1y'] * 100:.0f}th pct ({v['label']})")
        ch.append(f"VIX处于{v['percentile_1y'] * 100:.0f}分位（{_CH.get(v['label'], v['label'])}）")
    u = regime["usd"]
    if u.get("direction"):
        en.append(f"USD {u['direction']}")
        ch.append(f"美元{_CH[u['direction']]}")
    cr = regime["credit"]
    if cr.get("regime"):
        en.append(f"HY spreads {cr['regime']}")
        ch.append(f"高收益利差{_CH.get(cr['regime'], cr['regime'])}")
    if not en:
        both = {"en": "Macro data unavailable for this run (free-tier degradation).",
                "ch": "本次运行宏观数据不可用（免费数据源降级）。"}
    else:
        both = {"en": "; ".join(en) + ".", "ch": "；".join(ch) + "。"}
    if lang is None:
        return both
    return both.get(lang, both["en"])


def _quality(sources: dict, sid: str) -> str:
    src = (sources.get("series", {}).get(sid) or {}).get("source", "missing")
    # fred_csv = the same FRED series over the keyless public CSV endpoint; it is
    # real FRED data in FRED units, so it carries full "fred" quality, not "proxy".
    if src in ("fred", "fred_csv"):
        return "fred"
    if src.startswith("yfinance"):
        return "proxy"
    return "missing"


def _load_thresholds(workspace: Path) -> dict:
    th = dict(DEFAULT_THRESHOLDS)
    try:
        cfg = json.loads((workspace / "resolved_config.json").read_text(encoding="utf-8"))
        user = cfg.get("macro_regime") or {}
    except (OSError, ValueError):
        user = {}
    for k, v in user.items():
        if k not in th:
            continue
        if isinstance(th[k], dict) != isinstance(v, dict):
            # malformed override (scalar where a dict is expected, or vice
            # versa) would TypeError mid-compute and drop the whole artifact —
            # keep the default instead ("artifact is always written" contract).
            continue
        th[k] = {**th[k], **v} if isinstance(v, dict) else v
    return th


def compute(workspace: Path, date: str) -> dict:
    macro_dir = workspace.resolve().parent / "shared" / "market_context" / date / "raw" / "macro"
    out_path = workspace.resolve().parent / "shared" / "market_context" / date / "indicators" / "macro_regime.json"
    th = _load_thresholds(workspace)

    try:
        sources = json.loads((macro_dir / "macro_sources.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sources = {"series": {}, "inputs_missing": []}

    def load(sid: str, pct_to_decimal: bool = False) -> tuple[list[float], dict]:
        rows = read_series(macro_dir / f"{sid}.csv")
        values = [v / 100.0 if pct_to_decimal else v for _, v in rows]
        meta = {"source": (sources.get("series", {}).get(sid) or {}).get("source", "missing"),
                "as_of": rows[-1][0] if rows else None,
                "data_quality": _quality(sources, sid) if rows else "missing"}
        return values, meta

    us10y, m10 = load("DGS10", pct_to_decimal=True)
    us2y, m2 = load("DGS2", pct_to_decimal=True)
    cpi, mcpi = load("CPIAUCSL")
    core, _mcore = load("CPILFESL")
    ff, mff = load("FEDFUNDS", pct_to_decimal=True)
    vix, mvix = load("VIXCLS")
    usd, musd = load("DTWEXBGS")
    hy, mhy = load("BAMLH0A0HYM2", pct_to_decimal=True)

    regime = {
        "schema_version": 1,
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rates": {"us10y": {**rate_trend(us10y, th["rate_stable_band"]), **m10}},
        "curve": {**curve_slope(us2y[-1] if us2y else None, us10y[-1] if us10y else None,
                                th["curve_flat_band"]),
                  "short_leg": m2["source"], "data_quality": (
                      "missing" if not (us2y and us10y)
                      else ("proxy" if "proxy" in (m2["data_quality"], m10["data_quality"]) else "fred"))},
        "inflation": {**inflation_trend(cpi, core, th["inflation_band"]),
                      "data_quality": mcpi["data_quality"] if len(cpi) >= 13 else "missing"},
        "policy": {**policy_stance(ff, th["policy_band"]), "data_quality": mff["data_quality"]},
        "volatility": {**vix_percentile(vix, th["vix_percentile_window"], th["vix_labels"]),
                       "source": mvix["source"], "data_quality": (
                           "insufficient_history" if vix and len(vix[-th["vix_percentile_window"]:]) < _MIN_VIX_OBS
                           else mvix["data_quality"])},
        "usd": {**usd_trend(usd, th["usd_trend_band_pct"]), "source": musd["source"],
                "data_quality": musd["data_quality"]},
        "credit": {**credit_regime(hy, th["hy_regime"]), "data_quality": mhy["data_quality"]},
        "inputs_missing": list(sources.get("inputs_missing", [])),
    }
    # `summary_i18n`, not `summary`: the name says "cache, pick one" rather than
    # "report-ready". A surviving dict-valued `summary` is then a detectable
    # pre-fix artifact. build_shared_context.py resolves it per workspace.
    regime["summary_i18n"] = build_summary(regime)

    # temp file + os.replace: concurrent same-date readers (other tickers'
    # valuation runs) must never see a truncated shared artifact
    import os

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(regime, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out_path)
    print(f"compute_macro_regime: wrote {out_path}")
    return regime


def main():
    if len(sys.argv) < 3:
        print("usage: compute_macro_regime.py <workspace> <date>")
        return
    try:
        compute(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:  # advisory stage — never fail the pipeline
        print(f"compute_macro_regime: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

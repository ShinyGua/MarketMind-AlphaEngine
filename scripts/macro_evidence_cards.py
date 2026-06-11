#!/usr/bin/env python3
"""Deterministic macro evidence cards for MarketMind-AlphaEngine.

Projects material observations from the shared macro_regime.json into the
per-ticker evidence audit trail (normalized/{date}/evidence_cards/), so macro
context that shapes analyst memos is citable by `ev_…` id like any other card.
Rule-based and templated — no LLM. A benign regime produces zero cards (normal).

Card text honors the workspace `language` config (en | ch); JSON keys stay
English. Materiality scores come from a fixed table (0.5–0.8).

Usage:
    .venv/bin/python3 scripts/macro_evidence_cards.py <workspace> <date>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_10Y_MOVE_BAND = 0.0025   # |1m 10Y delta| at/above -> card
_VIX_PCT_BAND = 0.80      # VIX 1y percentile at/above -> card
_USD_MOVE_PCT = 2.0       # |1m USD % change| at/above -> card

_T = {
    "curve_inverted": {
        "materiality": 0.6, "sentiment": "negative", "tag": "curve",
        "en": ("Treasury yield curve inverted (2s10s {slope_bp:+.0f}bp)",
               "The 2s10s spread is {slope_bp:+.0f}bp — short rates above long rates.",
               "An inverted curve is a classic late-cycle / recession-risk signal and "
               "pressures long-duration equity valuations."),
        "ch": ("美债收益率曲线倒挂（2s10s {slope_bp:+.0f}bp）",
               "2年期与10年期利差为 {slope_bp:+.0f}bp，短端利率高于长端。",
               "曲线倒挂是典型的周期尾部/衰退风险信号，并对长久期股票估值构成压力。"),
    },
    "vix_elevated": {
        "materiality": 0.7, "sentiment": "negative", "tag": "volatility",
        "en": ("VIX at {vix:.1f}, {pct:.0f}th percentile of the past year ({label})",
               "Implied equity volatility sits in the top of its 1-year range.",
               "A {label} volatility regime widens drawdown tails and argues for "
               "staged entries and tighter risk overlays."),
        "ch": ("VIX 报 {vix:.1f}，处于过去一年 {pct:.0f} 分位（{label}）",
               "股票隐含波动率位于一年区间的高位。",
               "高波动状态加大回撤尾部风险，支持分批建仓与更严格的风险对冲。"),
    },
    "credit_wide": {
        "materiality": 0.7, "sentiment": "negative", "tag": "credit",
        "en": ("High-yield credit spreads {regime} at {spread_pct:.2f}%",
               "The HY OAS is {spread_pct:.2f}%, in the '{regime}' regime.",
               "Widening credit spreads signal funding stress and typically lead "
               "equity risk-off moves."),
        "ch": ("高收益债利差{regime_ch}，报 {spread_pct:.2f}%",
               "高收益债期权调整利差为 {spread_pct:.2f}%，处于“{regime_ch}”区间。",
               "信用利差走阔意味着融资压力上升，通常领先于股票市场的避险行情。"),
    },
    "rates_moved": {
        "materiality": 0.6, "sentiment": None, "tag": "rates",  # sign-dependent
        "en": ("US 10Y yield moved {delta_bp:+.0f}bp over the past month to {value_pct:.2f}%",
               "A {delta_bp:+.0f}bp one-month move in the 10Y Treasury yield.",
               "Rate moves of this size re-price discount rates and rotate "
               "growth/value leadership; the DCF risk-free input shifts with it."),
        "ch": ("美债10年期收益率近一个月变动 {delta_bp:+.0f}bp 至 {value_pct:.2f}%",
               "10年期美债收益率单月变动 {delta_bp:+.0f}bp。",
               "该量级的利率变动会重定价贴现率并切换成长/价值风格；DCF 的无风险利率输入随之变化。"),
    },
    "policy_shift": {
        "materiality": 0.6, "sentiment": None, "tag": "policy",  # easing+, tightening-
        "en": ("Fed policy stance: {stance} (fed funds {ff_pct:.2f}%, {delta_bp:+.0f}bp over 3m)",
               "The effective fed funds rate is {ff_pct:.2f}%, {delta_bp:+.0f}bp over three months.",
               "An {stance} Fed shifts the liquidity backdrop for equity multiples "
               "and the cost of leverage."),
        "ch": ("联储政策取向：{stance_ch}（联邦基金利率 {ff_pct:.2f}%，3个月变动 {delta_bp:+.0f}bp）",
               "有效联邦基金利率为 {ff_pct:.2f}%，三个月变动 {delta_bp:+.0f}bp。",
               "联储{stance_ch}改变股票估值倍数的流动性环境与杠杆成本。"),
    },
    "usd_moved": {
        "materiality": 0.5, "sentiment": "neutral", "tag": "usd",
        "en": ("Broad USD index moved {chg:+.1f}% over the past month",
               "The trade-weighted dollar moved {chg:+.1f}% in a month.",
               "Dollar swings of this size move overseas revenue translation and "
               "EM/commodity-linked earnings."),
        "ch": ("广义美元指数近一个月变动 {chg:+.1f}%",
               "贸易加权美元指数单月变动 {chg:+.1f}%。",
               "该幅度的美元波动影响海外收入折算及新兴市场/商品相关盈利。"),
    },
}

_STANCE_CH = {"easing": "宽松", "tightening": "紧缩", "on_hold": "按兵不动"}
_REGIME_CH = {"wide": "走阔", "stressed": "高度承压"}


def material_observations(regime: dict) -> list[dict]:
    """Rule-based materiality screen over the regime blocks. Deterministic order.

    Each observation carries the `data_quality` of the block it came from so the
    card's source provenance is per-signal, not copied from the 10Y block."""
    obs = []

    c = regime.get("curve") or {}
    # Keyless runs proxy the short leg with ^IRX (a 3m bill): 3m10s inverts far
    # earlier and deeper than 2s10s, so a proxy-quality "inverted" reading must
    # NOT become a recession-signal card (the regime JSON still discloses it).
    if (c.get("label") == "inverted" and c.get("slope_2s10s") is not None
            and c.get("data_quality") == "fred"):
        obs.append({"key": "curve_inverted", "quality": c.get("data_quality"),
                    "fmt": {"slope_bp": c["slope_2s10s"] * 10000}})

    v = regime.get("volatility") or {}
    if (v.get("percentile_1y") or 0) >= _VIX_PCT_BAND:
        obs.append({"key": "vix_elevated", "quality": v.get("data_quality"),
                    "fmt": {"vix": v.get("vix") or 0.0, "pct": v["percentile_1y"] * 100,
                            "label": v.get("label") or "elevated"}})

    cr = regime.get("credit") or {}
    if cr.get("regime") in ("wide", "stressed") and cr.get("hy_spread") is not None:
        obs.append({"key": "credit_wide", "materiality": 0.8 if cr["regime"] == "stressed" else 0.7,
                    "quality": cr.get("data_quality"),
                    "fmt": {"regime": cr["regime"], "regime_ch": _REGIME_CH[cr["regime"]],
                            "spread_pct": cr["hy_spread"] * 100}})

    r = (regime.get("rates") or {}).get("us10y") or {}
    if r.get("delta_1m") is not None and abs(r["delta_1m"]) >= _10Y_MOVE_BAND:
        obs.append({"key": "rates_moved", "quality": r.get("data_quality"),
                    "sentiment": "negative" if r["delta_1m"] > 0 else "positive",
                    "fmt": {"delta_bp": r["delta_1m"] * 10000, "value_pct": (r.get("value") or 0) * 100}})

    p = regime.get("policy") or {}
    if p.get("stance") in ("easing", "tightening") and p.get("fedfunds") is not None:
        obs.append({"key": "policy_shift", "quality": p.get("data_quality"),
                    "sentiment": "positive" if p["stance"] == "easing" else "negative",
                    "fmt": {"stance": p["stance"], "stance_ch": _STANCE_CH[p["stance"]],
                            "ff_pct": p["fedfunds"] * 100, "delta_bp": (p.get("delta_3m") or 0) * 10000}})

    u = regime.get("usd") or {}
    if u.get("chg_1m_pct") is not None and abs(u["chg_1m_pct"]) >= _USD_MOVE_PCT:
        obs.append({"key": "usd_moved", "quality": u.get("data_quality"),
                    "fmt": {"chg": u["chg_1m_pct"]}})

    return obs


def build_cards(regime: dict, date: str, lang: str) -> list[dict]:
    lang = lang if lang in ("en", "ch") else "en"
    cards = []
    for n, ob in enumerate(material_observations(regime), start=1):
        t = _T[ob["key"]]
        title, summary, why = (s.format(**ob["fmt"]) for s in t[lang])
        quality = ob.get("quality")  # per-signal provenance, not the 10Y block's
        cards.append({
            "id": f"ev_{date}_macro_{n:03d}",
            "desk": "macro",
            "source_type": "macro_series",
            "source_name": "FRED" if quality == "fred" else "yfinance",
            "url": None,
            "published_at": date,
            "ticker": "MARKET",
            "title": title,
            "summary": summary,
            "why_it_matters": why,
            "materiality_score": ob.get("materiality", t["materiality"]),
            "sentiment": ob.get("sentiment") or t["sentiment"],
            "topic_tags": ["macro", t["tag"]],
            "time_horizon": "daily",
        })
    return cards


def run(workspace: Path, date: str) -> list[dict]:
    regime_path = (workspace.resolve().parent / "shared" / "market_context" / date
                   / "indicators" / "macro_regime.json")
    try:
        regime = json.loads(regime_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"macro_evidence_cards: no regime at {regime_path}, skipping")
        return []

    try:
        cfg = json.loads((workspace / "resolved_config.json").read_text(encoding="utf-8"))
        lang = cfg.get("language", "en")
    except (OSError, ValueError):
        lang = "en"

    cards = build_cards(regime, date, lang)
    out_dir = workspace / "normalized" / date / "evidence_cards"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Re-runs (e.g. after a --force macro refetch) can produce fewer/reordered
    # observations; stale higher-numbered cards would otherwise linger and the
    # evidence grader would enforce citation of outdated claims.
    for stale in out_dir.glob(f"ev_{date}_macro_*.json"):
        stale.unlink()
    for card in cards:
        (out_dir / f"{card['id']}.json").write_text(
            json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"macro_evidence_cards: wrote {len(cards)} card(s) to {out_dir}")
    return cards


def main():
    if len(sys.argv) < 3:
        print("usage: macro_evidence_cards.py <workspace> <date>")
        return
    try:
        run(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:  # advisory stage — never fail the pipeline
        print(f"macro_evidence_cards: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

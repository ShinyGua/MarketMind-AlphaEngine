#!/usr/bin/env python3
"""Deterministic chip/volume evidence cards for MarketMind-AlphaEngine.

Projects material observations from quant/{date}/chip_structure.json (and its
embedded cn_flows) into the per-ticker evidence audit trail
(normalized/{date}/evidence_cards/), so the chip read that grounds analyst
stances is citable by `ev_…` id like any other card. Rule-based and templated —
no LLM. A quiet tape produces zero cards (normal).

Unlike the macro cards (context-only), chip cards are DIRECTIONAL evidence —
their materiality scores run to 0.75 and analysts may build a stance on them.

Card titles embed concrete numbers: the dedup pass clusters URL-less cards by
title, so a numberless template would collapse across days/desks.

Usage:
    .venv/bin/python3 scripts/chip_evidence_cards.py <workspace> <date>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from shared import contracts  # noqa: E402

_HOLDER_DISPERSE_PCT = 5.0    # holder count +5% in one period → dispersing card
_NB_MOVE_PP = 0.10            # northbound ±0.10pp over 5 sessions → card
_UNLOCK_HORIZON_DAYS = 60     # upcoming release within this window → card
_UNLOCK_MIN_PCT = 1.0         # ... and at least 1% of market cap
_PROFIT_EXTREME_LO = 0.10     # ≤10% of chips in profit → deep-trap card
_PROFIT_EXTREME_HI = 0.90     # ≥90% in profit → escape-velocity card
_INFLOW_STREAK = 3            # main-force consecutive inflow days → card

_T = {
    "holder_concentrating": {
        "materiality": 0.7, "sentiment": "positive", "tag": "holders",
        "en": ("Holder count fell {chg:.1f}% to {holders:,.0f} (as of {as_of}) — chips concentrating",
               "Two consecutive reporting periods of shrinking shareholder count.",
               "A falling holder count means chips are moving from many small hands into "
               "fewer larger ones — the classic reproducible signature of accumulation."),
        "ch": ("股东户数降 {chg:.1f}% 至 {holders:,.0f} 户（截至 {as_of}）——筹码趋于集中",
               "股东户数连续两期下降。",
               "户数下降表明筹码由分散的小户流向少数大户，是可复现的吸筹/集中信号——筹码正在变干净。"),
    },
    "holder_dispersing": {
        "materiality": 0.65, "sentiment": "negative", "tag": "holders",
        "en": ("Holder count rose {chg:.1f}% to {holders:,.0f} (as of {as_of}) — chips dispersing",
               "Shareholder count expanded sharply in the latest reporting period.",
               "A jump in holder count means chips scattered into retail hands — "
               "distribution pressure and a dirtier chip structure."),
        "ch": ("股东户数增 {chg:.1f}% 至 {holders:,.0f} 户（截至 {as_of}）——筹码趋于分散",
               "最近一期股东户数明显上升。",
               "户数大增表明筹码散入小户，往往对应派发压力，筹码结构变“脏”。"),
    },
    "main_force_inflow": {
        "materiality": 0.7, "sentiment": "positive", "tag": "money_flow",
        "en": ("Main-force net inflow {streak} straight sessions, {net_5d_yi:+.2f}00M CNY over 5d",
               "Large/extra-large orders have been net buyers for {streak} consecutive sessions.",
               "Sustained main-force inflow is direct evidence of directed accumulation, "
               "not retail churn."),
        "ch": ("主力资金连续 {streak} 日净流入，5日净额 {net_5d_yi:+.2f} 亿元",
               "大单/超大单连续 {streak} 个交易日净买入。",
               "持续的主力净流入是定向吸筹的直接证据，而非散户换手。"),
    },
    "main_force_outflow": {
        "materiality": 0.7, "sentiment": "negative", "tag": "money_flow",
        "en": ("Main-force net outflow {streak} straight sessions, {net_5d_yi:+.2f}00M CNY over 5d",
               "Large/extra-large orders have been net sellers for {streak} consecutive sessions.",
               "Sustained main-force outflow while price holds is distribution — "
               "the chips are leaving stronger hands."),
        "ch": ("主力资金连续 {streak} 日净流出，5日净额 {net_5d_yi:+.2f} 亿元",
               "大单/超大单连续 {streak} 个交易日净卖出。",
               "主力持续净流出（尤其价格未跌时）是派发信号——筹码正离开强手。"),
    },
    "lhb_appearance": {
        "materiality": 0.65, "sentiment": "neutral", "tag": "lhb",
        "en": ("On the dragon-tiger list {n}x in 90d; latest {date}: {interp} (net {net_yi:+.2f}00M)",
               "The name printed on the exchange's daily top-movers list.",
               "LHB seats reveal WHO is trading — hot money vs institutional seats — "
               "the most direct public read on the nature of the chips."),
        "ch": ("90日内 {n} 次登上龙虎榜；最近 {date}：{interp}（净买 {net_yi:+.2f} 亿）",
               "该股登上交易所龙虎榜。",
               "龙虎榜席位直接揭示交易对手性质（游资 vs 机构），是判断筹码干净与否最直接的公开数据。"),
    },
    "northbound_add": {
        "materiality": 0.6, "sentiment": "positive", "tag": "northbound",
        "en": ("Northbound holding rose {chg:+.2f}pp in 5 sessions to {pct:.2f}% of float",
               "Stock Connect holdings increased to {pct:.2f}% of the A-share float.",
               "Northbound money is slow, disclosure-tracked capital — its adds read as "
               "long-horizon conviction, not hot-money churn."),
        "ch": ("北向持股5日增 {chg:+.2f} 个百分点至 {pct:.2f}%",
               "陆股通持股占A股流通比例升至 {pct:.2f}%。",
               "北向为披露可追踪的慢钱，其增持代表长线资金的态度而非游资博弈。"),
    },
    "northbound_trim": {
        "materiality": 0.6, "sentiment": "negative", "tag": "northbound",
        "en": ("Northbound holding fell {chg:+.2f}pp in 5 sessions to {pct:.2f}% of float",
               "Stock Connect holdings declined to {pct:.2f}% of the A-share float.",
               "Sustained northbound trimming removes a stabilizing holder from the "
               "chip structure."),
        "ch": ("北向持股5日减 {chg:+.2f} 个百分点至 {pct:.2f}%",
               "陆股通持股占A股流通比例降至 {pct:.2f}%。",
               "北向持续减持意味着筹码结构中失去一类稳定持有人。"),
    },
    "unlock_overhang": {
        "materiality": 0.75, "sentiment": "negative", "tag": "unlock",
        "en": ("Restricted-share unlock on {date}: {pct:.1f}% of market cap",
               "A lock-up expiry adds {pct:.1f}% of market cap to the sellable float.",
               "Unlock overhang is a scheduled chip supply shock — the cleanest form of "
               "'dirty chips ahead' and a hard date the market trades in advance."),
        "ch": ("{date} 限售解禁：占总市值约 {pct:.1f}%",
               "解禁将使可卖出筹码增加约总市值的 {pct:.1f}%。",
               "解禁是有明确日期的筹码供给冲击——最典型的“筹码悬顶”，市场会提前交易。"),
    },
    "breakout_volume": {
        "materiality": 0.7, "sentiment": "positive", "tag": "volume_price",
        "en": ("Volume-confirmed breakout above the {days}d box top {range_high} (close {close})",
               "Price cleared a {days}-day base on ≥1.5x base volume.",
               "A platform breakout WITH volume is the price-volume signature of new "
               "money committing — the same move on thin volume would be suspect."),
        "ch": ("放量突破 {days} 日箱体上沿 {range_high}（现价 {close}）",
               "价格在≥1.5倍基区间量能配合下突破平台。",
               "带量突破平台是新资金进场的量价特征；缩量突破则可信度存疑。"),
    },
    "breakdown": {
        "materiality": 0.7, "sentiment": "negative", "tag": "volume_price",
        "en": ("Broke below the {days}d box floor {range_low} (close {close}, volume-confirmed: {vc})",
               "Price lost a {days}-day base support.",
               "A base breakdown converts the whole box into overhead trapped supply — "
               "every bounce now meets sellers getting out even."),
        "ch": ("跌破 {days} 日箱体下沿 {range_low}（现价 {close}，量能确认：{vc}）",
               "价格失守平台支撑。",
               "跌破箱体使整个平台变为上方套牢盘，其后每次反弹都会遭遇解套卖压。"),
    },
    "obv_divergence": {
        "materiality": 0.6, "sentiment": None, "tag": "volume_price",  # sign-dependent
        "en": ("OBV-price divergence: price {pdir} while OBV {odir} over 20 sessions",
               "Cumulative volume flow disagrees with the price move.",
               "When volume flow leads against price, the price move is running on "
               "fumes — divergences resolve toward the volume, not the price."),
        "ch": ("量价背离：20日价格{pdir_ch}而OBV{odir_ch}",
               "累积量能流向与价格走势相反。",
               "量在价先——背离通常向量能的方向修复，价格的走势缺乏筹码支持。"),
    },
    "profit_extreme": {
        "materiality": 0.6, "sentiment": None, "tag": "chip_distribution",
        "en": ("Only {pr:.0f}% of {win}d traded volume is in profit at {close}",
               "The volume-at-price distribution sits almost entirely {rel} the current price.",
               "An extreme profit ratio defines the game: deep-trapped supply caps every "
               "rally at the cost peaks; near-total profit means holders have no urgency to sell."),
        "ch": ("现价 {close} 下，{win} 日成交仅约 {pr:.0f}% 获利",
               "成交量分布几乎全部位于现价{rel_ch}。",
               "极端获利盘比例定义了博弈结构：深度套牢盘在上方成本峰处封压每次反弹；"
               "而近乎全获利则意味着持有人无急售压力。"),
    },
}


def _fmt_close(v):
    return f"{v:.2f}" if isinstance(v, (int, float)) else "?"


def material_observations(chip: dict) -> list[dict]:
    """Rule-based screen over chip_structure.json. Deterministic order."""
    obs = []
    if not chip or not chip.get("available"):
        return obs
    close = chip.get("last_close")
    cn = chip.get("cn_flows") or {}

    def _q(block):
        return (cn.get(block) or {}).get("data_quality") == "akshare"

    # 1. holder count (筹码干不干净 #1)
    if _q("holder_count"):
        hc = cn["holder_count"]
        latest = hc.get("latest") or {}
        chg = latest.get("change_pct")
        if hc.get("trend") == "concentrating" and chg is not None:
            obs.append({"key": "holder_concentrating",
                        "fmt": {"chg": abs(chg), "holders": latest.get("holders") or 0,
                                "as_of": latest.get("as_of", "?")}})
        elif chg is not None and chg >= _HOLDER_DISPERSE_PCT:
            obs.append({"key": "holder_dispersing",
                        "fmt": {"chg": chg, "holders": latest.get("holders") or 0,
                                "as_of": latest.get("as_of", "?")}})

    # 2. main-force flow
    if _q("main_force"):
        mf = cn["main_force"]
        net5 = mf.get("net_5d")
        if (mf.get("consecutive_inflow_days") or 0) >= _INFLOW_STREAK and net5:
            obs.append({"key": "main_force_inflow",
                        "fmt": {"streak": mf["consecutive_inflow_days"],
                                "net_5d_yi": net5 / 1e8}})
        elif (mf.get("consecutive_outflow_days") or 0) >= _INFLOW_STREAK and net5:
            obs.append({"key": "main_force_outflow",
                        "fmt": {"streak": mf["consecutive_outflow_days"],
                                "net_5d_yi": net5 / 1e8}})

    # 3. dragon-tiger list
    if _q("lhb") and (cn["lhb"].get("appearances_90d") or 0) > 0:
        events = cn["lhb"].get("events") or []
        last = events[-1] if events else {}
        obs.append({"key": "lhb_appearance",
                    "fmt": {"n": cn["lhb"]["appearances_90d"],
                            "date": last.get("date", "?"),
                            "interp": last.get("interpretation") or last.get("reason") or "—",
                            "net_yi": (last.get("net_buy") or 0) / 1e8}})

    # 4. northbound
    if _q("northbound"):
        nb = cn["northbound"]
        chg = nb.get("change_5d_pp")
        if chg is not None and abs(chg) >= _NB_MOVE_PP:
            obs.append({"key": "northbound_add" if chg > 0 else "northbound_trim",
                        "fmt": {"chg": chg, "pct": nb.get("holding_pct") or 0}})

    # 5. unlock overhang
    if _q("restricted_release"):
        for u in cn["restricted_release"].get("upcoming") or []:
            pct = u.get("pct_of_total")
            day = u.get("date", "")
            if pct and pct >= _UNLOCK_MIN_PCT and day:
                try:
                    import datetime as _dt
                    gap = (_dt.date.fromisoformat(day)
                           - _dt.date.fromisoformat(chip["date"])).days
                except ValueError:
                    continue
                if 0 <= gap <= _UNLOCK_HORIZON_DAYS:
                    obs.append({"key": "unlock_overhang", "fmt": {"date": day, "pct": pct}})
                    break

    # 6. platform breakout / breakdown
    plat = chip.get("platform") or {}
    br = plat.get("breakout") or {}
    if br.get("direction") == "up" and br.get("volume_confirmed"):
        obs.append({"key": "breakout_volume",
                    "fmt": {"days": plat.get("days_in_range") or 0,
                            "range_high": plat.get("range_high"),
                            "close": _fmt_close(close)}})
    elif br.get("direction") == "down":
        obs.append({"key": "breakdown",
                    "fmt": {"days": plat.get("days_in_range") or 0,
                            "range_low": plat.get("range_low"),
                            "close": _fmt_close(close),
                            "vc": br.get("volume_confirmed")}})

    # 7. OBV divergence
    vol = chip.get("volume") or {}
    div = vol.get("obv_price_divergence")
    if div in ("bullish", "bearish"):
        rising = div == "bullish"  # price down, OBV up
        obs.append({"key": "obv_divergence",
                    "sentiment": "positive" if rising else "negative",
                    "fmt": {"pdir": "fell" if rising else "rose",
                            "odir": "rose" if rising else "fell",
                            "pdir_ch": "下跌" if rising else "上涨",
                            "odir_ch": "上行" if rising else "下行"}})

    # 8. extreme profit ratio
    dist = chip.get("chip_distribution") or {}
    pr = dist.get("profit_ratio")
    if pr is not None and (pr <= _PROFIT_EXTREME_LO or pr >= _PROFIT_EXTREME_HI):
        deep_trap = pr <= _PROFIT_EXTREME_LO
        obs.append({"key": "profit_extreme",
                    "sentiment": "negative" if deep_trap else "positive",
                    "fmt": {"pr": pr * 100, "win": dist.get("window_days") or 0,
                            "close": _fmt_close(close),
                            "rel": "above" if deep_trap else "below",
                            "rel_ch": "上方" if deep_trap else "下方"}})
    return obs


def build_cards(chip: dict, ticker: str, date: str, lang: str) -> list[dict]:
    lang = lang if lang in ("en", "ch") else "en"
    cards = []
    for n, ob in enumerate(material_observations(chip), start=1):
        t = _T[ob["key"]]
        title, summary, why = (s.format(**ob["fmt"]) for s in t[lang])
        cards.append({
            "id": f"ev_{date}_chip_{n:03d}",
            "desk": "chips",
            "source_type": "chip_structure",
            "source_name": "akshare" if ob["key"] in (
                "holder_concentrating", "holder_dispersing", "main_force_inflow",
                "main_force_outflow", "lhb_appearance", "northbound_add",
                "northbound_trim", "unlock_overhang") else "yfinance",
            "url": None,
            "published_at": date,
            "ticker": ticker,
            "title": title,
            "summary": summary,
            "why_it_matters": why,
            "materiality_score": ob.get("materiality", t["materiality"]),
            "sentiment": ob.get("sentiment") or t["sentiment"] or "neutral",
            "topic_tags": ["chips", t["tag"]],
            "time_horizon": "daily",
            "usage": "directional",
        })
    return cards


def run(workspace: Path, date: str) -> list[dict]:
    chip_path = contracts.chip_structure_path(workspace, date)
    try:
        chip = json.loads(chip_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"chip_evidence_cards: no chip structure at {chip_path}, skipping")
        return []

    try:
        cfg = json.loads((workspace / "resolved_config.json").read_text(encoding="utf-8"))
        lang = cfg.get("language", "en")
    except (OSError, ValueError):
        lang = "en"

    ticker = Path(str(workspace)).name
    cards = build_cards(chip, ticker, date, lang)
    out_dir = workspace / "normalized" / date / "evidence_cards"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Re-runs can produce fewer/reordered observations; delete our own stale
    # prefix so the evidence grader never enforces citation of outdated claims.
    for stale in out_dir.glob(f"ev_{date}_chip_*.json"):
        stale.unlink()
    for card in cards:
        (out_dir / f"{card['id']}.json").write_text(
            json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"chip_evidence_cards: wrote {len(cards)} card(s) to {out_dir}")
    return cards


def main():
    if len(sys.argv) < 3:
        print("usage: chip_evidence_cards.py <workspace> <date>")
        return
    try:
        run(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:  # advisory stage — never fail the pipeline
        print(f"chip_evidence_cards: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

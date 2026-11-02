"""Classify an evidence card: is it chip evidence, how old is it, can it count
as independent corroboration?

Pure functions over a card dict — used by normalize/dedup_evidence.py (to
annotate the digest) and by eval/pre_release_gate.py (to enforce freshness and
the K1 corroboration rule). Nothing here mutates a card file.

Three chip classes, because an evidence id or desk name alone cannot prove
where data came from (desks file SZSE margin data under `company` ids):

  chip            the deterministic chip generator wrote it, the chips desk
                  wrote it, its lineage names a chip block/dataset, or the
                  author tagged it ``chips``
  suspected_chip  an exact chip phrase in its title / tags / source fields
                  (融资余额, 龙虎榜, "northbound", ...). Never counts as
                  independent corroboration, but is not freshness-blocked
  other           everything else, by desk

Bare words like 主力 / 融资 / margin / turnover are deliberately NOT matched:
主力产品 (main product), 定增融资 (equity raise) and gross margin are company
facts, not chip data.
"""
from __future__ import annotations

import re
from datetime import date as _date
from datetime import datetime, timedelta, timezone

CHIP_SOURCE_TYPES = {"chip_structure", "margin_data", "fund_flow", "northbound_holding",
                     "lhb", "dragon_tiger", "holder_count", "turnover"}

# akshare dataset names that only carry chip/flow data
CHIP_DATASETS = ("stock_margin_detail", "stock_margin_", "stock_individual_fund_flow",
                 "stock_hsgt_", "stock_lhb_", "stock_zh_a_gdhs", "stock_restricted_release",
                 "fund_flow", "margin_detail")

CHIP_TAGS = {"chips", "chip", "筹码"}

_CHIP_PHRASES_CN = ("融资余额", "融资融券", "两融", "融资买入", "融资净买", "主力资金",
                    "主力净流入", "主力净流出", "主力净买", "主力净卖", "大单净流入", "大单净流出",
                    "北向资金", "北向持股", "沪股通", "深股通", "陆股通", "龙虎榜", "股东户数",
                    "股东人数", "限售股解禁", "解禁", "换手率", "筹码")
_CHIP_PHRASES_EN = ("margin balance", "margin financing", "margin debt", "main-force",
                    "main force", "northbound", "dragon tiger", "dragon-tiger",
                    "shareholder count", "holder count", "lock-up expir", "lockup expir",
                    "turnover rate", "chip distribution", "money flow", "fund flow")

FILING_HOSTS = ("sec.gov", "cninfo.com.cn", "sse.com.cn", "szse.cn", "hkexnews.hk",
                "bse.cn")
_FILING_TYPES = ("filing", "10-k", "10-q", "8-k", "6-k", "20-f", "announcement", "公告",
                 "annual_report", "quarterly_report", "exchange_disclosure")

# Families that may corroborate a chip-grounded BUY/SELL (K1). Market, sector,
# macro, trend and intraday context never can; nor can CN/HK reference valuation.
INDEPENDENT_FAMILIES = {"company", "filing", "catalyst", "company_news"}


def _text(card: dict) -> str:
    parts = [card.get("title"), card.get("source_name"), card.get("source_type")]
    parts += [str(t) for t in (card.get("topic_tags") or [])]
    return " ".join(str(p) for p in parts if p)


def chip_class(card: dict) -> str:
    meta = card.get("meta") or {}
    lineage = meta.get("lineage") or card.get("lineage") or {}
    if (meta.get("family") == "chip" or str(card.get("desk", "")).lower() == "chips"
            or str(card.get("source_type", "")).lower() in CHIP_SOURCE_TYPES):
        return "chip"
    source_fields = " ".join(str(x) for x in (card.get("source_name"), lineage.get("dataset"),
                                              lineage.get("block")) if x).lower()
    if any(ds in source_fields for ds in CHIP_DATASETS):
        return "chip"
    if CHIP_TAGS & {str(t).lower() for t in (card.get("topic_tags") or [])}:
        return "chip"
    text = _text(card)
    low = text.lower()
    if any(p in text for p in _CHIP_PHRASES_CN) or any(p in low for p in _CHIP_PHRASES_EN):
        return "suspected_chip"
    return "other"


def family(card: dict, target_ticker: str | None = None) -> str:
    """Evidence family for the K1 independence test."""
    cls = chip_class(card)
    if cls == "chip":
        return "chip"
    stype = str(card.get("source_type", "")).lower()
    url = str(card.get("url") or card.get("source_url") or "").lower()
    if any(t in stype for t in _FILING_TYPES) or any(h in url for h in FILING_HOSTS):
        return "filing"
    desk = str(card.get("desk", "")).lower()
    tags = {str(t).lower() for t in (card.get("topic_tags") or [])}
    if desk == "company":
        return "catalyst" if {"catalyst", "earnings_date", "calendar"} & tags else "company"
    if desk == "web":
        ticker = str(card.get("ticker") or "")
        if target_ticker and ticker and ticker.upper() == str(target_ticker).upper():
            return "company_news"
        return "news"
    return desk or "unknown"


def parse_when(value) -> tuple[datetime | None, str]:
    """Parse a published/as_of value -> (UTC datetime of its latest instant, precision).

    precision ∈ datetime | date | month | year | unknown. Date-only values map
    to the end of that day so they are never judged "after" the same day.
    """
    if value is None:
        return None, "unknown"
    s = str(value).strip()
    if not s:
        return None, "unknown"
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        s = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    try:
        if len(s) > 10 and ("T" in s or " " in s):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:  # local wall time of unknown zone: judge by date
                d = dt.date()
                return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc), "date"
            return dt.astimezone(timezone.utc), "datetime"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            d = _date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc), "date"
        if re.fullmatch(r"\d{4}-\d{2}", s):
            y, mth = map(int, s.split("-"))
            nxt = _date(y + (mth == 12), 1 if mth == 12 else mth + 1, 1)
            return datetime.combine(nxt - timedelta(days=1), datetime.max.time(),
                                    tzinfo=timezone.utc), "month"
        if re.fullmatch(r"\d{4}", s):
            return datetime(int(s), 12, 31, 23, 59, 59, tzinfo=timezone.utc), "year"
    except ValueError:
        pass
    return None, "unknown"


def published_after(card: dict, issued_at: datetime) -> bool:
    """True only when the card is unambiguously dated after issuance.

    Date-only values get a one-day tolerance: US runs are dated by the prior
    New York session while cards carry the (later) Asian calendar date.
    """
    when, precision = parse_when(card.get("published_at"))
    if when is None:
        return False
    if precision in ("month", "year"):  # only when the whole period is later
        start = when.replace(day=1) if precision == "month" else when.replace(month=1, day=1)
        return start.replace(hour=0, minute=0, second=0, microsecond=0) > issued_at
    slack = timedelta(days=1) if precision == "date" else timedelta(0)
    return when - slack > issued_at


def as_of(card: dict) -> str | None:
    meta = card.get("meta") or {}
    for v in (meta.get("as_of"), card.get("as_of")):
        when, precision = parse_when(v)
        if when is not None and precision in ("date", "datetime"):
            return str(v).strip()[:10]
    return None


# Dated documents: their publication date is a fair observation date.
_NEWS_TYPES = {"news", "nasdaq", "web", "article", "media", "press", "newsapi",
               "filing", "announcement", "8-k", "10-q", "10-k", "form_4", "公告"}


def observation_date(card: dict) -> tuple[str | None, str | None]:
    """(date, basis) for freshness checks.

    An explicit as_of always wins. A news or filing card may fall back to its
    publication date — a dated document reports what was current when it ran,
    and an old one is still old. A data card may not: desks stamp data cards
    with the run date, which is how 2024 northbound numbers looked current in 2026.
    """
    explicit = as_of(card)
    if explicit:
        return explicit, "as_of"
    if str(card.get("source_type", "")).lower() in _NEWS_TYPES:
        when, precision = parse_when(card.get("published_at"))
        if when is not None and precision in ("date", "datetime"):
            return when.date().isoformat(), "published_at"
    return None, None


def classify(card: dict, target_ticker: str | None = None) -> dict:
    """Everything the digest and the gate need to know about one card."""
    cls = chip_class(card)
    fam = family(card, target_ticker)
    return {
        "chip_class": cls,
        "family": fam,
        "as_of": as_of(card),
        "generator": (card.get("meta") or {}).get("generator"),
        "independent": cls == "other" and fam in INDEPENDENT_FAMILIES,
    }

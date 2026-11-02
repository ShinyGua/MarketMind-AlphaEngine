"""Tests for mcp/shared/evidence_meta.py — chip classification, families and
publication-time checks, on cards modelled on real workspace artifacts."""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from shared import evidence_meta as em  # noqa: E402

# workspaces/300308/normalized/2026-10-22/evidence_cards/ev_2026-10-22_company_003.json
MARGIN_AS_COMPANY = {
    "id": "ev_2026-10-22_company_003", "desk": "company", "source_type": "data",
    "source_name": "深交所融资融券明细（akshare stock_margin_detail_szse）；证券时报网 2026-10-22",
    "url": "http://finance.eastmoney.com/a/202610223903780374.html",
    "published_at": "2026-10-22", "ticker": "300308",
    "title": "融资余额连降至256.58亿元：较10-08减少63.24亿元",
    "topic_tags": ["margin", "deleveraging", "chips", "fund_flow"],
}


def test_desk_filed_margin_data_is_chip():
    assert em.chip_class(MARGIN_AS_COMPANY) == "chip"
    c = em.classify(MARGIN_AS_COMPANY, "300308")
    assert c["family"] == "chip" and c["independent"] is False
    assert c["as_of"] is None  # observation date only in prose -> unknown


def test_dataset_name_alone_marks_chip():
    card = {**MARGIN_AS_COMPANY, "topic_tags": ["leverage"]}
    assert em.chip_class(card) == "chip"


def test_generated_chip_card_is_chip_with_as_of():
    card = {"id": "ev_2026-10-22_chip_001", "desk": "chips", "source_type": "chip_structure",
            "meta": {"as_of": "2026-06-30", "family": "chip", "generator": "chip_evidence_cards"}}
    c = em.classify(card)
    assert c["chip_class"] == "chip" and c["as_of"] == "2026-06-30"


def test_exact_phrase_is_only_suspected():
    card = {"desk": "web", "ticker": "300308", "title": "中际旭创龙虎榜：机构净买入3亿元",
            "source_type": "news", "topic_tags": []}
    assert em.chip_class(card) == "suspected_chip"
    assert em.classify(card, "300308")["independent"] is False


def test_company_words_that_look_like_chips_are_not():
    for title in ("公司主力产品800G光模块出货量翻倍",       # 主力 = main product
                  "定增融资35亿元获证监会批复",             # 融资 = equity raise
                  "Gross margin expanded to 46% on product mix",
                  "Inventory turnover improved for a third quarter"):
        card = {"desk": "company", "title": title, "source_type": "news", "topic_tags": []}
        assert em.chip_class(card) == "other", title
        assert em.classify(card)["independent"] is True


def test_families():
    assert em.family({"desk": "company", "source_type": "filing"}) == "filing"
    assert em.family({"desk": "web", "url": "https://www.sec.gov/Archives/x"}) == "filing"
    assert em.family({"desk": "web", "ticker": "MU", "source_type": "news"}, "MU") == "company_news"
    assert em.family({"desk": "web", "ticker": "MARKET", "source_type": "news"}, "MU") == "news"
    assert em.family({"desk": "sector", "source_type": "news"}) == "sector"
    assert em.family({"desk": "company", "topic_tags": ["catalyst"]}) == "catalyst"
    sector = {"desk": "sector", "title": "HBM pricing up", "source_type": "news"}
    assert em.classify(sector)["independent"] is False


def test_real_mu_cards_are_not_future_dated():
    # Each was available when its run happened; date-only values get one day.
    cases = [
        ("2026-08-11", datetime(2026, 8, 11, 8, 3, tzinfo=timezone.utc)),
        ("2026-08-16", datetime(2026, 8, 17, 1, 8, tzinfo=timezone.utc)),
        ("2026-08-27T00:00:00+09:00", datetime(2026, 8, 27, 2, 21, tzinfo=timezone.utc)),
    ]
    for published, issued in cases:
        assert em.published_after({"published_at": published}, issued) is False, published


def test_genuinely_later_card_is_caught():
    issued = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)
    assert em.published_after({"published_at": "2026-10-24"}, issued) is True
    assert em.published_after({"published_at": "2026-10-22T09:00:00Z"}, issued) is True
    assert em.published_after({"published_at": "2026-10"}, issued) is False
    assert em.published_after({"published_at": "下周一"}, issued) is False  # unparseable: unknown


def test_parse_when_precisions():
    assert em.parse_when("2026-10-21")[1] == "date"
    assert em.parse_when("20261021")[1] == "date"
    assert em.parse_when("2026-10-21T15:00:00+08:00")[1] == "datetime"
    assert em.parse_when("2026-10-21 15:00:00")[1] == "date"   # naive: judged by date
    assert em.parse_when("2026")[1] == "year"
    assert em.parse_when(None)[1] == "unknown"

"""One language per report — writer contract + the grader's false-positive rule.

The FP rule is the important half. A naive "any CJK in an `en` run is a defect"
check fires on a Chinese company name, on akshare source records, and on quoted
foreign headlines — all legitimate. These tests pin the distinction so a future
tightening cannot silently start failing honest runs.
"""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATE = "2026-08-03"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grader = _load("language_purity_grader", "eval/graders/language_purity_grader.py")
chips = _load("compute_chip_structure", "scripts/compute_chip_structure.py")
intraday = _load("intraday_timing", "scripts/intraday_timing.py")
macro = _load("compute_macro_regime", "scripts/compute_macro_regime.py")
cards = _load("chip_evidence_cards", "scripts/chip_evidence_cards.py")

CJK = re.compile(r"[一-鿿]")

_VOL = {"volume_ratio": 0.72, "volume_regime": "normal"}
_CHIP = {"profit_ratio": 0.585, "main_peak_price": 405.41,
         "concentration": 1.027, "window_days": 120}
_SR = {"supports": [{"price": 804.0}], "resistances": [{"price": 928.95}]}


def _ws(tmp_path: Path, lang: str) -> Path:
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    (ws / "resolved_config.json").write_text(json.dumps({"language": lang}), encoding="utf-8")
    return ws


# ── writer contract: ONE language, but BOTH still work ───────────────

def test_chip_note_is_single_language_and_bilingual_capability_survives():
    en = chips.build_note(_VOL, _CHIP, _SR, "en")
    ch = chips.build_note(_VOL, _CHIP, _SR, "ch")
    assert isinstance(en, str) and isinstance(ch, str)
    assert not CJK.search(en)
    assert "获利盘" in ch          # the ch branch was kept, not deleted
    assert en != ch


def test_intraday_note_is_single_language():
    art_en = intraday.build("MU", {"enabled": False}, "en")
    assert art_en["note_lang"] == "en"
    art_ch = intraday.build("MU", {"enabled": False}, "ch")
    assert art_ch["note_lang"] == "ch"


def test_macro_summary_keeps_the_bilingual_cache_but_can_resolve():
    regime = {"rates": {"us10y": {"value": 4.75, "direction": "rising"}},
              "curve": {}, "inflation": {}, "policy": {}, "volatility": {},
              "usd": {}, "credit": {}}
    both = macro.build_summary(regime)
    assert isinstance(both, dict) and {"en", "ch"} <= set(both)
    one = macro.build_summary(regime, "en")
    assert isinstance(one, str) and not CJK.search(one)


def test_lhb_card_en_carries_no_scraped_chinese():
    """The genuine bug: akshare 解读/上榜原因 spliced into an English title."""
    assert cards._lhb_reason_en("日涨幅偏离值达7%的证券") == "+7% daily price deviation"
    assert cards._lhb_reason_en("连续三个交易日内，涨幅偏离值累计达20%的证券") \
        == "3-day cumulative +20% deviation"
    assert cards._lhb_seat_en("机构专用席位买入") == "institutional seat"
    assert cards._lhb_seat_en("知名游资营业部") == "hot-money seat"
    assert cards._lhb_seat_en("深股通专用") == "northbound seat"
    assert cards._lhb_seat_en("") == "unclassified seat"
    # the closed vocabulary always yields ASCII, even on an unknown rule
    for probe in ("未知的新规则", "", "日振幅值达15%的证券"):
        assert not CJK.search(cards._lhb_reason_en(probe))


# ── the false-positive rule ──────────────────────────────────────────

def test_chinese_prose_in_en_run_is_caught_by_fullwidth_punctuation():
    hits = grader.scan_text("量比 1.8；获利盘约 70%。", "en", set(), grader.DEFAULTS)
    assert any(h["signal"] == "fullwidth_punctuation" for h in hits)


def test_chinese_company_name_in_en_run_is_not_a_violation():
    """A proper noun never carries a full stop — this is the whole design."""
    nouns = {"金力永磁"}
    assert grader.scan_text("JL MAG (金力永磁) reported record results.",
                            "en", nouns, grader.DEFAULTS) == []
    # …and without the allowlist it WOULD fire, proving the suppression matters
    assert grader.scan_text("JL MAG (金力永磁) reported record results.",
                            "en", set(), grader.DEFAULTS) != []


def test_cn_flows_subtree_is_exempt_on_an_en_run():
    doc = {"cn_flows": {"lhb": {"events": [{"reason": "日涨幅偏离值达7%的证券"}]}},
           "note": "normal volume (ratio 0.72)."}
    assert grader.scan_json(doc, "en", set(), grader.DEFAULTS) == []


def test_english_enums_and_keys_do_not_fail_a_ch_run():
    doc = {"usage": "directional", "timing_state": "neutral",
           "decision": "BUY", "note": "量能平稳（量比0.72）。"}
    assert grader.scan_json(doc, "ch", set(), grader.DEFAULTS) == []


def test_english_sentence_in_a_ch_run_is_caught():
    hits = grader.scan_text(
        "The panel converged on a bearish lean after two rounds of debate.",
        "ch", set(), grader.DEFAULTS)
    assert any(h["signal"] == "english_prose_in_ch_run" for h in hits)


def test_markdown_image_caption_does_not_fail_a_ch_run():
    """Chart embeds name a file the template renders — renderer furniture."""
    assert grader.scan_text("![Price action & technicals](charts/price_chart.svg)",
                            "ch", set(), grader.DEFAULTS) == []


def test_legacy_bilingual_artifact_warns_but_does_not_fail(tmp_path):
    ws = _ws(tmp_path, "en")
    q = ws / "quant" / DATE
    q.mkdir(parents=True)
    (q / "chip_structure.json").write_text(
        json.dumps({"note": {"en": "normal volume.", "ch": "量能平稳。"}}), encoding="utf-8")
    res = grader.grade(str(ws), DATE)
    assert res["pass"] is True
    assert any("legacy" in w for w in res["warnings"])

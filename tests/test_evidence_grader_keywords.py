"""Guard tests for evidence_grader.extract_keywords CJK support: Chinese titles
must yield enough keywords that the >=3-overlap citation fallback is reachable
(previously 0 keywords → only an exact-id citation could pass).
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "evidence_grader", ROOT / "eval" / "graders" / "evidence_grader.py")
eg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eg)


def test_english_title_keywords_unchanged():
    kws = eg.extract_keywords("VIX at 32.0, 95th percentile of the past year")
    assert "vix" in kws and "percentile" in kws
    assert "the" not in kws  # stop word
    assert "32" not in kws   # short token


def test_chinese_title_yields_bigram_keywords():
    kws = eg.extract_keywords("高收益债利差走阔，报 4.60%")
    cjk = [k for k in kws if any("一" <= ch <= "鿿" for ch in k)]
    assert len(cjk) >= 3  # min_overlap=3 is now reachable
    assert "利差" in cjk and "高收" in cjk


def test_chinese_paraphrase_counts_as_citation():
    title = "高收益债利差走阔，报 4.60%"
    report = "本周高收益债利差明显走阔，信用环境趋紧。".lower()
    kws = eg.extract_keywords(title)
    assert eg.check_keyword_overlap(kws, report, min_overlap=3)


def test_unrelated_chinese_text_not_cited():
    kws = eg.extract_keywords("高收益债利差走阔，报 4.60%")
    assert not eg.check_keyword_overlap(kws, "公司发布新款家电产品。".lower(), min_overlap=3)

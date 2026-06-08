"""Regression tests: graders must judge values currency- and magnitude-independently.

A KRW report (comma-grouped 7-digit prices, no ``$``) once produced false factuality
failures (numbers unparseable) and a false valuation failure (a ~1.7 KRW rounding diff
tripping an absolute tolerance). These guard both fixes while confirming USD behavior
is unchanged.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(rel_path, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fact = _load("eval/graders/factuality_grader.py", "factuality_grader")
val = _load("eval/graders/valuation_grader.py", "valuation_grader")


# ── factuality: currency-agnostic, comma-aware extraction ─────────────────────

def test_price_match_krw_comma_grouped_no_symbol():
    # KRW style: comma-grouped 7-digit, no "$"
    text = "最新收盘 2,298,000 KRW；价格牢站 SMA20（1,978,267）上方。"
    assert fact.search_price_in_report(text, 2298000.0, tolerance=230.0) == 2298000.0


def test_price_match_usd_dollar_prefixed_still_works():
    text = "Last close was $423.70, above the 50-day."
    assert fact.search_price_in_report(text, 423.70, tolerance=0.5) == 423.70


def test_indicator_match_krw_comma_grouped():
    text = "MACD=261,781.09 vs 信号线 240,660.03，柱状图 +21,121.06。"
    assert fact.search_indicator_in_report(text, "macd", 261781.09, tolerance=0.5) == 261781.09
    assert fact.search_indicator_in_report(text, "macd_signal", 240660.03, tolerance=0.5) == 240660.03


def test_indicator_integer_rounded_large_price_within_relative_tol():
    # report rounds SMA to an integer; relative tolerance must absorb the <1 unit gap
    text = "SMA20 1,978,267 / SMA50 1,434,459"
    tol = max(0.5, 1978266.54 * 1e-4)  # mirrors grade()'s price-tol floor
    assert fact.search_indicator_in_report(text, "sma_20", 1978266.54, tolerance=tol) == 1978267.0


def test_all_numbers_strips_commas():
    nums = fact._all_numbers("a 2,298,000 b 743,819.31 c -67.6%")
    assert 2298000.0 in nums and 743819.31 in nums


# ── valuation: relative tolerance for the sensitivity-grid center vs base ──────

def test_sensitivity_relative_tolerance_passes_for_large_krw_values():
    base_iv = 743819.31
    center = 743817.62  # 1.69 KRW diff — fails absolute 0.05, passes relative
    assert abs(center - base_iv) <= max(val._ABS_TOL, abs(base_iv) * val._REL_TOL)


def test_sensitivity_relative_tolerance_unchanged_for_usd_scale():
    # a $50 base keeps the tight absolute floor; a real 2-unit error still fails
    base_iv = 50.00
    assert abs(50.03 - base_iv) <= max(val._ABS_TOL, abs(base_iv) * val._REL_TOL)
    assert not (abs(52.00 - base_iv) <= max(val._ABS_TOL, abs(base_iv) * val._REL_TOL))

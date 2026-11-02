"""scripts/export_release.py — the export step both drivers run."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("export_release", ROOT / "scripts" / "export_release.py")
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)

GATED = {"decision": "HOLD", "risk_overlay": "trim", "confidence": 0.5}


def test_header_decision_comes_from_the_gated_decision():
    draft = ("# Robinhood (HOOD) — Daily Market Detail Report\n"
             "**Date:** 2026-10-29  |  **Decision:** BUY (bullish lean)  |  **Sector:** Financials\n\n"
             "## Executive Summary\n")
    out = ex.with_gated_header(draft, GATED, "en")
    assert "**Decision:** HOLD (risk overlay trim, confidence 0.50)  |  **Sector:** Financials" in out
    assert "drafted under a BUY view" in out and "final decision, which passed the pre-release gate, is HOLD" in out
    assert out.endswith("## Executive Summary\n")


def test_matching_header_gets_no_note_and_chinese_stays_chinese():
    draft = "**日期：** 2026-10-12 ｜ **决策：** HOLD（周期 1 个月） ｜ **行业：** 光通信\n"
    out = ex.with_gated_header(draft, GATED, "ch")
    assert out == "**日期：** 2026-10-12 ｜ **决策：** HOLD（风险叠加 trim，信心 0.50） ｜ **行业：** 光通信\n"


def test_a_header_with_no_label_is_flagged():
    draft = "**Date:** 2026-08-13  |  **Decision:** No investment-decision panel has convened yet\n"
    out = ex.with_gated_header(draft, GATED, "en")
    assert "**Decision:** HOLD (risk overlay trim, confidence 0.50)\n" in out and "drafted before the panel decided" in out


def test_a_standalone_decision_line_is_corrected():
    """Codex review R4: a Decision field ending its line, followed by the report."""
    draft = "**Decision:** BUY\n\n## Executive Summary\nOld bullish thesis.\n"
    out = ex.with_gated_header(draft, {**GATED, "decision": "SELL"}, "en")
    assert out.startswith("**Decision:** SELL (risk overlay trim, confidence 0.50)\n\n> Note:")
    assert "drafted under a BUY view" in out and out.endswith("## Executive Summary\nOld bullish thesis.\n")
    ch = ex.with_gated_header("**决策：** BUY\n\n## 执行摘要\n", {**GATED, "decision": "SELL"}, "ch")
    assert ch.startswith("**决策：** SELL（风险叠加 trim，信心 0.50）\n\n> 注：")


def test_a_draft_without_a_header_gets_one_under_its_title():
    out = ex.with_gated_header("# Report v1\n\nBody", GATED, "en")
    assert out == "# Report v1\n**Decision:** HOLD (risk overlay trim, confidence 0.50)\n\n\nBody"

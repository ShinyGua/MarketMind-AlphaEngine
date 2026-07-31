"""Sanity checks for the headless Codex pipeline driver's stage plan.

Validates that the driver covers every contract stage in order and maps each to
either a deterministic Python call or a `codex exec` LLM call — without invoking
Codex or mutating anything (uses --dry-run via the in-process API).
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "run_codex_pipeline", ROOT / "scripts" / "run_codex_pipeline.py")
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)


def test_stage_plan_matches_contract():
    # the module asserts this at import, but make it an explicit test too
    assert list(driver.STAGE_FUNCS.keys()) == driver.STAGES


def test_role_to_skill():
    assert driver.role_to_skill("company_analyst") == "mm-company-analyst"
    assert driver.role_to_skill("risk_analyst") == "mm-risk-analyst"


def test_dry_run_plan_covers_all_stages():
    ws = ROOT / "workspaces" / "TSLA"
    if not ws.exists():
        # no fixture workspace in this checkout — skip without failing
        import pytest
        pytest.skip("workspaces/TSLA fixture not present")
    ctx = driver.Ctx(ws, dry=True, model=None, max_workers=4)
    driver.run_pipeline(ctx, None, None)
    flat = " ".join(" ".join(c) for c in ctx.plan)
    # every stage ran its stage_timer start
    for stage in driver.STAGES:
        assert f"stage_timer.py start {ws} {stage}" in flat, f"missing stage {stage}"
    # deterministic stages present
    assert "normalize/dedup_evidence.py" in flat
    assert "valuation/run_valuation.py" in flat
    assert "templates/render_pdf.py" in flat
    assert "eval/release_gate.py" in flat
    # macro layer + intraday timing + shared-context bundling (deterministic)
    assert "scripts/collect_macro_series.py" in flat, "collect stage missing macro collector"
    assert "scripts/compute_macro_regime.py" in flat, "collect stage missing regime computation"
    assert "scripts/macro_evidence_cards.py" in flat, "collect stage missing macro evidence cards"
    assert "scripts/intraday_timing.py" in flat, "quant stage missing intraday timing block"
    assert "scripts/build_shared_context.py" in flat, "valuation stage missing shared-context bundler"
    # chip layer: CN flows in collect, structure + cards before dedup in normalize
    assert "scripts/collect_cn_chips.py" in flat, "collect stage missing CN chip collector"
    assert "scripts/compute_chip_structure.py" in flat, "normalize stage missing chip structure"
    assert "scripts/chip_evidence_cards.py" in flat, "normalize stage missing chip evidence cards"
    norm_calls = [" ".join(c) for c in ctx.plan if "chip" in " ".join(c) or "dedup" in " ".join(c)]
    dedup_idx = next(i for i, c in enumerate(norm_calls) if "dedup_evidence" in c)
    chip_idx = next(i for i, c in enumerate(norm_calls) if "compute_chip_structure" in c)
    cards_idx = next(i for i, c in enumerate(norm_calls) if "chip_evidence_cards" in c)
    assert chip_idx < cards_idx < dedup_idx, "chip structure/cards must run before dedup"
    # LLM stages issued as codex exec
    assert "codex exec" in flat
    for skill in ("mm-company-analyst", "mm-report-writer", "mm-decision-maker",
                  "mm-discussion-moderator"):
        assert skill in flat, f"missing LLM stage {skill}"
    # discuss_debate stage runs the multi-round panel: views + tally + convergence grader
    assert "mm-discussion-panelist" in flat, "discuss stage missing panelist views"
    assert "discussion_convergence_grader.py" in flat, "discuss stage missing convergence grader"
    # the old selective-debate scan/assignments are gone
    assert "$ARGUMENTS[2]=scan" not in flat, "discuss stage still runs the legacy moderator scan"
    assert "debate_assignments" not in flat, "discuss stage still references debate_assignments"
    # decide stage runs the multi-round panel: ballots + tally + convergence grader
    assert "mm-decision-panelist" in flat, "decide stage missing panelist ballots"
    assert "mm-decision-maker $ARGUMENTS[2]=tally" in flat or "tally" in flat, \
        "decide stage missing chair tally"
    assert "panel_convergence_grader.py" in flat, "decide stage missing convergence grader"

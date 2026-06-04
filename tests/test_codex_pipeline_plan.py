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
    # LLM stages issued as codex exec
    assert "codex exec" in flat
    for skill in ("mm-company-analyst", "mm-report-writer", "mm-decision-maker",
                  "mm-discussion-moderator"):
        assert skill in flat, f"missing LLM stage {skill}"

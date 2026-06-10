"""Unit tests for the headless driver's resume-skip decision.

run_pipeline must skip stages status.json already records as completed for the
SAME run_date, and must NOT skip anything for a different run_date (fresh run).
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "run_codex_pipeline", ROOT / "scripts" / "run_codex_pipeline.py")
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)

DATE = "2026-06-09"


def _ctx(tmp_path: Path, status: dict | None) -> SimpleNamespace:
    ws = tmp_path / "WS"
    ws.mkdir(parents=True, exist_ok=True)
    if status is not None:
        (ws / "status.json").write_text(json.dumps(status), encoding="utf-8")
    return SimpleNamespace(ws=ws, date=DATE)


def test_same_run_date_skips_completed_stages(tmp_path):
    ctx = _ctx(tmp_path, {"run_date": DATE,
                          "stages_completed": ["resolve_config", "init_workspace", "collect"]})
    assert driver._completed_stages(ctx) == {"resolve_config", "init_workspace", "collect"}


def test_different_run_date_skips_nothing(tmp_path):
    ctx = _ctx(tmp_path, {"run_date": "2026-01-01",
                          "stages_completed": ["resolve_config", "collect"]})
    assert driver._completed_stages(ctx) == set()


def test_missing_or_malformed_status_skips_nothing(tmp_path):
    assert driver._completed_stages(_ctx(tmp_path, None)) == set()
    ctx = _ctx(tmp_path, None)
    (ctx.ws / "status.json").write_text("{not json", encoding="utf-8")
    assert driver._completed_stages(ctx) == set()


def test_unknown_stage_names_are_ignored(tmp_path):
    ctx = _ctx(tmp_path, {"run_date": DATE,
                          "stages_completed": ["collect", "not_a_stage"]})
    assert driver._completed_stages(ctx) == {"collect"}

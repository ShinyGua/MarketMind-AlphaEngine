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


# ---------- the run date: a finished run never pins the next one ----------

def _today_is(monkeypatch, day: str):
    monkeypatch.setattr(driver.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=day + "\n", returncode=0))


def test_a_completed_run_does_not_pin_the_next_trading_day(tmp_path, monkeypatch):
    _today_is(monkeypatch, "2026-06-10")
    ctx = _ctx(tmp_path, {"run_date": DATE, "stage": "completed",
                          "stages_completed": list(driver.STAGES)})
    assert driver.resolve_run_date(ctx) == "2026-06-10"


def test_explicit_from_keeps_the_recorded_run(tmp_path, monkeypatch):
    _today_is(monkeypatch, "2026-06-10")
    ctx = _ctx(tmp_path, {"run_date": DATE, "stage": "completed"})
    assert driver.resolve_run_date(ctx, keep_recorded=True) == DATE


def test_a_new_run_date_starts_a_fresh_status(tmp_path):
    ctx = _ctx(tmp_path, {"run_date": "2026-06-08", "stage": "completed",
                          "stages_completed": list(driver.STAGES),
                          "errors": [{"message": "pre_release_gate blocked: stale_chip"}]})
    ctx.dry, ctx.ticker, ctx.run_mode = False, "WS", "daily"
    driver.start_fresh_if_new_date(ctx)
    st = json.loads((ctx.ws / "status.json").read_text())
    assert st["run_date"] == DATE and st["stages_completed"] == [] and st["errors"] == []
    assert driver._completed_stages(ctx) == set()


def test_the_same_run_date_keeps_its_status(tmp_path):
    status = {"run_date": DATE, "stages_completed": ["collect"], "errors": [{"message": "x"}]}
    ctx = _ctx(tmp_path, status)
    ctx.dry, ctx.ticker, ctx.run_mode = False, "WS", "daily"
    driver.start_fresh_if_new_date(ctx)
    assert json.loads((ctx.ws / "status.json").read_text()) == status


def test_status_writes_are_atomic(tmp_path, monkeypatch):
    """A crash mid-write must leave the previous status, not a torn file."""
    ctx = _ctx(tmp_path, {"run_date": DATE, "stages_completed": ["collect"], "errors": []})
    ctx.dry, ctx.ticker, ctx.run_mode = False, "WS", "daily"
    real = driver.contracts.os.replace

    def crash(*a):
        raise KeyboardInterrupt
    monkeypatch.setattr(driver.contracts.os, "replace", crash)
    try:
        driver.add_error(ctx, "boom")
    except KeyboardInterrupt:
        pass
    monkeypatch.setattr(driver.contracts.os, "replace", real)
    assert json.loads((ctx.ws / "status.json").read_text())["stages_completed"] == ["collect"]


def test_a_forced_rerun_reopens_the_stages_it_redoes(tmp_path):
    """--from decide on a completed run: a crash part-way must resume decide, not find
    the earlier run's markers (found by TLC while checking the ledger-repair fix)."""
    ctx = _ctx(tmp_path, {"run_date": DATE, "stage": "completed",
                          "stages_completed": list(driver.STAGES), "errors": []})
    ctx.dry = False
    driver.reopen_stages(ctx, driver.STAGES[driver.STAGES.index("decide"):])
    st = json.loads((ctx.ws / "status.json").read_text())
    assert st["stage"] == "decide"
    assert st["stages_completed"] == driver.STAGES[:driver.STAGES.index("decide")]
    assert driver._completed_stages(ctx) == set(driver.STAGES[:driver.STAGES.index("decide")])

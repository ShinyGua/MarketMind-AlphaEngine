"""Codex driver: pre-release gate -> one revise -> gate again; blocked export.

det() and codex_skill() are replaced by fakes that write the gate JSON a real
run would write, so the control flow is tested without Codex or a network.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("run_codex_pipeline", ROOT / "scripts" / "run_codex_pipeline.py")
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)

DATE = "2026-10-22"
CARD = "ev_2026-10-22_company_001"
# A valid v2 decision: the driver re-checks a "passed" verdict, so the fixture must pass.
DECISION = {"schema_version": 2, "decision": "HOLD", "risk_overlay": "none",
            "actions": {"if_flat": {"label": "HOLD", "overlay": "none"},
                        "if_long": {"label": "HOLD", "overlay": "none"}},
            "forecast": {"p_outperform": 0.5, "benchmark": "CSI300", "horizon_sessions": 20},
            "confidence": 0.5,
            "top_reasons": [{"text": "Shipments doubled", "evidence_ids": [CARD], "direction": "up"}]}


def _ctx(tmp_path):
    ws = tmp_path / "300308"
    (ws / "decision" / DATE).mkdir(parents=True)
    (ws / "decision" / DATE / "final_decision.json").write_text(json.dumps(DECISION))
    cards = ws / "normalized" / DATE / "evidence_cards"
    cards.mkdir(parents=True)
    (cards / f"{CARD}.json").write_text(json.dumps(
        {"id": CARD, "desk": "company", "source_type": "news", "ticker": "300308",
         "title": "Q3 800G shipments doubled", "published_at": "2026-10-21"}))
    (ws / "status.json").write_text(json.dumps({"stages_completed": [], "errors": []}))
    return SimpleNamespace(ws=ws, date=DATE, dry=False, ticker="300308", run_mode="daily",
                           lang="en", cfg={}, plan=[])


def _install(monkeypatch, ctx, verdicts):
    """Each gate call pops the next verdict; record every skill dispatch."""
    calls = {"gate": 0, "skills": [], "det": []}

    def fake_det(c, module, *args, critical=True):
        calls["det"].append(module)
        if module == "eval/pre_release_gate.py":
            status = verdicts[calls["gate"]]
            calls["gate"] += 1
            dec = ctx.ws / "decision" / DATE / "final_decision.json"
            (ctx.ws / "decision" / DATE / "pre_release_gate.json").write_text(json.dumps({
                "status": status, "errors": [] if status == "passed" else
                [{"check": "k1_uncorroborated", "message": "chip-only SELL"}],
                "issued_at": "2026-10-22T09:00:00+00:00",
                "decision_sha256": hashlib.sha256(dec.read_bytes()).hexdigest()}))
            return 0 if status == "passed" else 2
        return 0

    monkeypatch.setattr(driver, "det", fake_det)
    monkeypatch.setattr(driver, "codex_skill",
                        lambda c, skill, *extra, critical=True: calls["skills"].append((skill, extra)))
    monkeypatch.setattr(driver, "log", lambda *a, **k: None)
    return calls


def test_blocked_twice_revises_once_and_records_the_error(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    calls = _install(monkeypatch, ctx, ["blocked", "blocked"])
    assert driver.gate_decision(ctx) == "blocked"
    assert calls["skills"] == [("mm-decision-maker", ("revise",))]
    assert calls["gate"] == 2
    assert json.loads((ctx.ws / "decision" / DATE / "final_decision.pre_gate.json").read_text()) == DECISION
    errors = json.loads((ctx.ws / "status.json").read_text())["errors"]
    assert errors and "k1_uncorroborated" in errors[-1]["message"]


def test_revision_that_passes_releases_normally(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    calls = _install(monkeypatch, ctx, ["blocked", "passed"])
    assert driver.gate_decision(ctx) == "passed"
    assert json.loads((ctx.ws / "status.json").read_text())["errors"] == []
    assert len(calls["skills"]) == 1


def test_first_pass_needs_no_revision(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    calls = _install(monkeypatch, ctx, ["passed"])
    assert driver.gate_decision(ctx) == "passed" and calls["skills"] == []


def test_blocked_release_exports_nothing(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    calls = _install(monkeypatch, ctx, ["blocked"])
    driver._gate(ctx)
    calls["det"].clear()
    driver.st_export(ctx)
    assert calls["det"] == []   # no charts, no PDF
    blocked = (ctx.ws / "final" / DATE / "BLOCKED.md").read_text()
    assert "k1_uncorroborated" in blocked
    assert not (ctx.ws / "final" / DATE / "daily_report.md").exists()


def test_passed_release_copies_the_report_then_renders(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    (ctx.ws / "drafts" / DATE).mkdir(parents=True)
    (ctx.ws / "drafts" / DATE / "daily_v1.md").write_text("# Report v1")
    calls = _install(monkeypatch, ctx, ["passed"])
    driver._gate(ctx)
    calls["det"].clear()
    driver.st_export(ctx)
    assert (ctx.ws / "final" / DATE / "daily_report.md").read_text() == "# Report v1"
    combined = json.loads((ctx.ws / "final" / DATE / "daily_report.json").read_text())
    assert combined["decision"] == DECISION
    assert calls["det"] == ["templates/charts.py", "templates/render_pdf.py"]


def test_user_review_lands_where_finalize_reads_it(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    driver.st_user_review(ctx)
    review = json.loads((ctx.ws / "reviews" / DATE / "user_review.json").read_text())
    assert review["skipped"] is True


def test_render_pdf_refuses_blocked_edited_or_ungated(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "templates"))
    spec = importlib.util.spec_from_file_location("render_pdf", ROOT / "templates" / "render_pdf.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    ctx = _ctx(tmp_path)
    assert rp._gate_allows(ctx.ws, DATE, allow_ungated=False) is None    # missing
    assert rp._gate_allows(ctx.ws, DATE, allow_ungated=True) == DECISION  # pre-gate re-render
    later = "2026-11-02"                                                   # a run the gate existed for
    (ctx.ws / "decision" / later).mkdir(parents=True)
    (ctx.ws / "decision" / later / "final_decision.json").write_text(json.dumps(DECISION))
    assert rp._gate_allows(ctx.ws, later, allow_ungated=True) is None
    _install(monkeypatch, ctx, ["blocked"])
    driver._gate(ctx)
    assert rp._gate_allows(ctx.ws, DATE, allow_ungated=True) is None     # never overrides a block

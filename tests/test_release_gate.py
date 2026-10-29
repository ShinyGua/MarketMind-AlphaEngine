"""Unit tests for the release gate's missing-grader handling.

A missing grader result is not a pass: an absent critical grader (factuality /
evidence) must fail the gate, an absent advisory grader must at least warn —
the gate must never report "passed" for a run that was never fully checked.
"""
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "release_gate", ROOT / "eval" / "release_gate.py")
gate_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_mod)

DATE = "2026-06-09"

GATE_GRADERS = ["factuality", "evidence", "consistency", "valuation", "depth",
                "decision_risk", "language_purity"]


def _ws(tmp_path: Path, present: list[str], pre_release: str | None = "passed") -> Path:
    ws = tmp_path / "WS"
    eval_dir = ws / "eval" / DATE
    eval_dir.mkdir(parents=True, exist_ok=True)
    for name in present:
        (eval_dir / f"{name}_result.json").write_text(
            json.dumps({"pass": True}), encoding="utf-8")
    if pre_release is not None:
        dec = ws / "decision" / DATE / "final_decision.json"
        dec.parent.mkdir(parents=True, exist_ok=True)
        dec.write_text(json.dumps({"decision": "HOLD"}), encoding="utf-8")
        sha = hashlib.sha256(dec.read_bytes()).hexdigest()
        (dec.parent / "pre_release_gate.json").write_text(
            json.dumps({"status": pre_release, "decision_sha256": sha}), encoding="utf-8")
    return ws


def test_all_graders_present_and_passing_is_passed(tmp_path):
    ws = _ws(tmp_path, GATE_GRADERS)
    gate = gate_mod.evaluate(ws, DATE)
    assert gate["release_status"] == "passed"
    assert gate["missing_graders"] == []


def test_missing_critical_grader_fails_gate(tmp_path):
    ws = _ws(tmp_path, [g for g in GATE_GRADERS if g != "factuality"])
    gate = gate_mod.evaluate(ws, DATE)
    assert gate["release_status"] == "failed"
    assert "factuality" in gate["missing_graders"]
    assert gate["grader_summary"]["factuality"] == "unknown"


def test_missing_advisory_grader_warns(tmp_path):
    ws = _ws(tmp_path, [g for g in GATE_GRADERS if g != "consistency"])
    gate = gate_mod.evaluate(ws, DATE)
    assert gate["release_status"] == "warning"
    assert gate["missing_graders"] == ["consistency"]


def test_pre_release_block_wins_over_passing_graders(tmp_path):
    ws = _ws(tmp_path, GATE_GRADERS, pre_release="blocked")
    gate = gate_mod.evaluate(ws, DATE)
    assert gate["release_status"] == "blocked"
    assert gate["pre_release_gate"] == "blocked"


def test_decision_edited_after_the_gate_is_blocked(tmp_path):
    ws = _ws(tmp_path, GATE_GRADERS)
    (ws / "decision" / DATE / "final_decision.json").write_text('{"decision": "BUY"}')
    assert gate_mod.evaluate(ws, DATE)["release_status"] == "blocked"


def test_missing_pre_release_gate_caps_at_warning(tmp_path):
    ws = _ws(tmp_path, GATE_GRADERS, pre_release=None)
    gate = gate_mod.evaluate(ws, DATE)
    assert gate["release_status"] == "warning"
    assert gate["pre_release_gate"] == "gate_missing"

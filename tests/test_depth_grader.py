"""Unit test for the depth grader's empty-section guard.

A report with a bare "## " heading (no text, no body) must be counted as a stub
section, not crash the grader with an IndexError.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "depth_grader", ROOT / "eval" / "graders" / "depth_grader.py")
grader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grader)

DATE = "2026-06-09"


def test_empty_trailing_section_is_stub_not_crash(tmp_path):
    ws = tmp_path / "WS"
    (ws / "final" / DATE).mkdir(parents=True, exist_ok=True)
    (ws / "resolved_config.json").write_text(json.dumps({}), encoding="utf-8")
    (ws / "status.json").write_text(json.dumps({"run_mode": "daily"}), encoding="utf-8")
    # trailing bare "## " with NO newline after it: re.split leaves an empty
    # block whose splitlines() is [] — the original IndexError case
    body = "# Title\n\n## Thesis\n" + "x" * 400 + "\n\n## "
    (ws / "final" / DATE / "daily_report.md").write_text(body, encoding="utf-8")
    r = grader.grade(str(ws), DATE, "report-only")
    stubs = r["checks"]["report"]["stub_sections"]
    assert any(s["section"] == "(empty)" for s in stubs)
    assert r["pass"] is False

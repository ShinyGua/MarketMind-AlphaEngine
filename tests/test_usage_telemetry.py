"""Tests for eval/usage_telemetry.py — actual token usage from transcripts."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("usage_telemetry", ROOT / "eval" / "usage_telemetry.py")
ut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ut)

DATE = "2026-10-22"


def _assistant(req, inp, out, cache_read=0, ts="2026-10-22T03:30:00Z", thinking=0):
    return json.dumps({"type": "assistant", "requestId": req, "timestamp": ts,
                       "message": {"id": f"msg_{req}", "model": "claude-opus-5",
                                   "usage": {"input_tokens": inp, "output_tokens": out,
                                             "cache_read_input_tokens": cache_read,
                                             "cache_creation_input_tokens": 0,
                                             "output_tokens_details": {"thinking_tokens": thinking}}}})


def _agent(session_dir, name, description, lines):
    sub = session_dir / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"{name}.meta.json").write_text(json.dumps({"description": description}))
    (sub / f"{name}.jsonl").write_text("\n".join(lines) + "\n")


def _setup(tmp_path):
    projects = tmp_path / "projects"
    base = ut.project_dir(projects)
    session = base / "sess-1"
    _agent(session, "agent-a", f"mm:300308:{DATE}:decide:mm-decision-panelist",
           [_assistant("r1", 10, 100, cache_read=1000, thinking=40),
            _assistant("r1", 10, 100, cache_read=1000, thinking=40),   # same request split across lines
            _assistant("r2", 5, 50)])
    _agent(session, "agent-b", f"mm:300308:{DATE}:draft:mm-report-writer", [_assistant("r3", 7, 70)])
    _agent(session, "agent-c", "some other task", [_assistant("r4", 1, 1)])           # unattributed
    _agent(session, "agent-d", f"mm:MU:{DATE}:draft:mm-report-writer", [_assistant("r5", 9, 9)])  # other run
    (base / "sess-1.jsonl").write_text(_assistant("r6", 3, 30) + "\n" +
                                       _assistant("r7", 3, 30, ts="2026-10-23T09:00:00Z") + "\n")
    ws = tmp_path / "300308"
    ws.mkdir()
    (ws / "status.json").write_text(json.dumps({"run_date": DATE, "started_at": "2026-10-22T03:00:00Z",
                                                "updated_at": "2026-10-22T06:00:00Z"}))
    return ws, projects


def test_attribution_dedup_and_unattributed(tmp_path):
    ws, projects = _setup(tmp_path)
    r = ut.run(ws, DATE, projects)
    c = r["claude"]
    assert c["agents"] == 2 and c["sessions"] == 1
    assert c["by_stage"]["decide"] == {"input": 15, "cache_write": 0, "cache_read": 1000,
                                       "output": 150, "thinking": 40, "requests": 2}
    assert c["by_skill"]["mm-report-writer"]["output"] == 70           # MU's run excluded
    assert c["by_stage"]["orchestrator"]["requests"] == 1               # r7 is outside the window
    assert c["unattributed"]["requests"] == 1 and c["unattributed"]["input"] == 1
    assert r["totals_attributed"]["output"] == 150 + 70 + 30
    assert json.loads((ws / "eval" / DATE / "usage.json").read_text())["method"] == "api_reported_usage"


def test_codex_streams_both_event_shapes_and_unparsed(tmp_path):
    ws = tmp_path / "300308"
    d = ws / "eval" / DATE / "usage"
    d.mkdir(parents=True)
    (d / "001_mm-report-writer.jsonl").write_text(
        json.dumps({"type": "item.completed"}) + "\n" +
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60,
                                                        "output_tokens": 20}}) + "\n")
    (d / "001_mm-report-writer.meta.json").write_text(json.dumps({"stage": "draft", "skill": "mm-report-writer"}))
    (d / "002_mm-decision-maker.jsonl").write_text(json.dumps({"msg": {
        "type": "token_count", "info": {"total_token_usage": {"input_tokens": 50, "output_tokens": 5,
                                                              "reasoning_output_tokens": 3}}}}) + "\n")
    (d / "002_mm-decision-maker.meta.json").write_text(json.dumps({"stage": "decide", "skill": "mm-decision-maker"}))
    (d / "003_mm-x.jsonl").write_text("not json\n")
    r = ut.collect_codex(ws, DATE)
    assert r["by_stage"]["draft"]["cache_read"] == 60 and r["by_stage"]["decide"]["thinking"] == 3
    assert r["unparsed_codex_files"] == ["003_mm-x.jsonl"]


def test_window_bound_includes_requests_within_its_last_second(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join((_assistant("r1", 1, 2, ts="2026-10-30T06:13:12.500Z"),
                               _assistant("r2", 1, 2, ts="2026-10-30T06:13:13.000Z"))),
                    encoding="utf-8")
    keys = [k for k, _m, _u in ut._transcript_requests(path, ("2026-10-30T03:45:38", "2026-10-30T06:13:12"))]
    assert keys == ["r1"]


def test_metrics_shows_api_reported_usage_next_to_the_proxy():
    spec = importlib.util.spec_from_file_location("metrics", ROOT / "eval" / "metrics.py")
    metrics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(metrics)
    runs = [{"graders": {"cost": {"estimated_total_tokens": 1592349, "estimated_cost_usd": 12.23}},
             "usage_tokens": {"output": 1152903, "cache_read": 95945903, "cache_write": 4522785,
                              "input": 1778}}]
    ca = metrics.cost_analytics(runs)
    assert ca["runs_with_usage"] == 1 and ca["avg_cache_read_tokens"] == 95945903
    assert "cache-read tokens/run (API-reported, 1 run(s))" in metrics.format_markdown(metrics.compute_all(runs))

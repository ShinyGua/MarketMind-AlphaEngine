"""Single source of truth for all cross-layer contracts.

This module is the runtime owner of pipeline stages, artifact paths,
report naming, release semantics, and memory filenames. All Python
consumers (web, eval, MCP servers) MUST import from here instead of
hardcoding paths or stage lists.

Markdown/skill consumers reference mm-orchestrator/SKILL.md as the
textual owner — but the values must match this module.
"""
from __future__ import annotations

import json
from pathlib import Path

# ── Stage contract ────────────────────────────────────────────────────

STAGES = [
    "resolve_config", "init_workspace", "collect", "normalize", "quant",
    "valuation",
    "discuss_memos", "discuss_debate", "discuss_synthesis",
    "draft", "review", "decide", "export", "user_review", "reflect",
]
STAGE_COUNT = len(STAGES)  # 15

# ── Report naming ─────────────────────────────────────────────────────

def report_basename(run_mode: str = "daily") -> str:
    """Return 'weekly_report' or 'daily_report'."""
    return "weekly_report" if run_mode == "weekly" else "daily_report"


def draft_prefix(run_mode: str = "daily") -> str:
    """Return 'weekly' or 'daily' for draft filenames like daily_v1.md."""
    return "weekly" if run_mode == "weekly" else "daily"


# ── Artifact path helpers (all dated unless noted) ─────��──────────────

def decision_path(ws: Path, date: str) -> Path:
    return ws / "decision" / date / "final_decision.json"


def final_report_path(ws: Path, date: str, run_mode: str = "daily") -> Path:
    return ws / "final" / date / f"{report_basename(run_mode)}.md"


def final_report_json_path(ws: Path, date: str, run_mode: str = "daily") -> Path:
    return ws / "final" / date / f"{report_basename(run_mode)}.json"


def score_history_path(ws: Path, date: str) -> Path:
    return ws / "reviews" / date / "score_history.json"


def review_dir(ws: Path, date: str) -> Path:
    return ws / "reviews" / date / "final_reviews"


def revision_brief_path(ws: Path, date: str) -> Path:
    return ws / "reviews" / date / "revision_briefs" / "revision_brief.json"


def release_gate_path(ws: Path, date: str) -> Path:
    return ws / "eval" / date / "release_gate.json"


def regression_flag_path(ws: Path, date: str) -> Path:
    return ws / "eval" / date / "regression_flag.json"


def memory_context_path(ws: Path, date: str, role: str) -> Path:
    return ws / f"{date}_memory_context_{role}.json"


def shared_context_path(ws: Path, date: str) -> Path:
    return ws / f"{date}_shared_context.json"


def evidence_digest_path(ws: Path, date: str) -> Path:
    return ws / "normalized" / date / "evidence_digest.json"


def quant_summary_path(ws: Path, date: str) -> Path:
    return ws / "quant" / date / "quant_summary.json"


def valuation_summary_path(ws: Path, date: str) -> Path:
    return ws / "valuation" / date / "valuation_summary.json"


def thesis_map_path(ws: Path, date: str) -> Path:
    return ws / "discussion" / date / "thesis_map.json"


def user_review_path(ws: Path, date: str) -> Path:
    return ws / "reviews" / date / "user_review.json"


# ── Grader result paths ──────────────────────────────────────────────

def grader_result_path(ws: Path, date: str, grader: str) -> Path:
    return ws / "eval" / date / f"{grader}_result.json"


# ── Auto-detect report for consumers that don't know run_mode ────────

def detect_final_report(ws: Path, date: str) -> Path | None:
    """Find the final report, trying status.json first, then scanning."""
    # 1. Try status.json run_mode
    status_path = ws / "status.json"
    if status_path.is_file():
        try:
            run_mode = json.loads(status_path.read_text(encoding="utf-8")).get("run_mode")
            if run_mode:
                p = final_report_path(ws, date, run_mode)
                if p.is_file():
                    return p
        except (json.JSONDecodeError, OSError):
            pass
    # 2. Fallback: weekly first (rarer), then daily
    for mode in ("weekly", "daily"):
        p = final_report_path(ws, date, mode)
        if p.is_file():
            return p
    return None


# ── Release semantics ────────────────────────────────────────────────

RELEASE_PASSED = "passed"
RELEASE_WARNING = "warning"
RELEASE_FAILED = "failed"

# Metrics interpretation:
#   passed  → strict success
#   warning → operational success (advisory grader failed, report is usable)
#   failed  → failure (critical grader failed = regression)

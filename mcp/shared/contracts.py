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


# ── Decision-panel (multi-round vote → converge → exit) ──────────────

def panel_dir(ws: Path, date: str) -> Path:
    return ws / "decision" / date / "panel"


def panel_round_dir(ws: Path, date: str, rnd: int) -> Path:
    return panel_dir(ws, date) / f"round_{rnd}"


def panel_ballot_path(ws: Path, date: str, rnd: int, role: str) -> Path:
    return panel_round_dir(ws, date, rnd) / f"{role}_ballot.json"


def panel_summary_path(ws: Path, date: str, rnd: int) -> Path:
    return panel_dir(ws, date) / f"panel_summary_round_{rnd}.json"


def panel_convergence_path(ws: Path, date: str, rnd: int) -> Path:
    return panel_dir(ws, date) / f"convergence_round_{rnd}.json"


# ── Discussion-panel (multi-round stance → converge → exit) ──────────

def discussion_panel_dir(ws: Path, date: str) -> Path:
    return ws / "discussion" / date / "panel"


def discussion_panel_round_dir(ws: Path, date: str, rnd: int) -> Path:
    return discussion_panel_dir(ws, date) / f"round_{rnd}"


def discussion_view_path(ws: Path, date: str, rnd: int, role: str) -> Path:
    return discussion_panel_round_dir(ws, date, rnd) / f"{role}_view.json"


def discussion_panel_summary_path(ws: Path, date: str, rnd: int) -> Path:
    return discussion_panel_dir(ws, date) / f"panel_summary_round_{rnd}.json"


def discussion_convergence_path(ws: Path, date: str, rnd: int) -> Path:
    return discussion_panel_dir(ws, date) / f"convergence_round_{rnd}.json"


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


# Run-context files live in per-workspace subfolders (memory/, shared_context/).
# The plain helpers below are the canonical *write* paths. Readers should use the
# *_read_path resolvers, which prefer the new layout but fall back to the legacy
# root-level files so un-migrated workspaces still load.

def memory_context_path(ws: Path, date: str, role: str) -> Path:
    return ws / "memory" / f"{date}_{role}.json"


def legacy_memory_context_path(ws: Path, date: str, role: str) -> Path:
    return ws / f"{date}_memory_context_{role}.json"


def shared_context_path(ws: Path, date: str) -> Path:
    return ws / "shared_context" / f"{date}.json"


def legacy_shared_context_path(ws: Path, date: str) -> Path:
    return ws / f"{date}_shared_context.json"


def _prefer_existing(primary: Path, fallback: Path) -> Path:
    """Return primary if it exists, else fallback if it exists, else primary."""
    if primary.exists():
        return primary
    if fallback.exists():
        return fallback
    return primary


def memory_context_read_path(ws: Path, date: str, role: str) -> Path:
    return _prefer_existing(
        memory_context_path(ws, date, role),
        legacy_memory_context_path(ws, date, role),
    )


def shared_context_read_path(ws: Path, date: str) -> Path:
    return _prefer_existing(
        shared_context_path(ws, date),
        legacy_shared_context_path(ws, date),
    )


def evidence_digest_path(ws: Path, date: str) -> Path:
    return ws / "normalized" / date / "evidence_digest.json"


def intraday_timing_path(ws: Path, date: str) -> Path:
    """Deterministic 1h/4h timing artifact (scripts/intraday_timing.py).
    Timing-only contract: entry/exit zone framing, never a vote reason."""
    return ws / "quant" / date / "intraday_timing.json"


def macro_regime_path(ws: Path, date: str) -> Path:
    """Shared macro regime artifact (scripts/compute_macro_regime.py)."""
    return ws.resolve().parent / "shared" / "market_context" / date / "indicators" / "macro_regime.json"


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

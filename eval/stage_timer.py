#!/usr/bin/env python3
"""Stage timer: record stage start/end timestamps during pipeline execution.

Usage:
    .venv/bin/python3 eval/stage_timer.py start <workspace> <stage_name>
    .venv/bin/python3 eval/stage_timer.py end <workspace> <stage_name> <success:true|false> [error_message]
"""

import sys
import json
import os
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_log(log_path: Path) -> list[dict]:
    """Load or create the stage log."""
    if log_path.exists():
        try:
            return json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_log(log_path: Path, entries: list[dict]) -> None:
    """Write the stage log back to disk."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def now_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def handle_start(workspace: str, stage_name: str) -> dict:
    ws = Path(workspace)
    log_path = ws / "eval_stage_log.json"

    entries = load_log(log_path)

    entry = {
        "name": stage_name,
        "started_at": now_iso(),
        "completed_at": None,
        "duration_s": None,
        "success": None,
    }
    entries.append(entry)

    save_log(log_path, entries)
    return entry


def handle_end(workspace: str, stage_name: str, success: bool, error_msg: str | None = None) -> dict:
    ws = Path(workspace)
    log_path = ws / "eval_stage_log.json"

    entries = load_log(log_path)

    # Find the most recent entry with this stage name that hasn't been completed
    target = None
    for entry in reversed(entries):
        if entry.get("name") == stage_name and entry.get("completed_at") is None:
            target = entry
            break

    if target is None:
        # No matching start entry found; create a partial record
        target = {
            "name": stage_name,
            "started_at": None,
            "completed_at": None,
            "duration_s": None,
            "success": None,
        }
        entries.append(target)

    completed_at = now_iso()
    target["completed_at"] = completed_at
    target["success"] = success

    # Compute duration if start time is available
    if target.get("started_at"):
        try:
            start_dt = datetime.fromisoformat(target["started_at"])
            end_dt = datetime.fromisoformat(completed_at)
            target["duration_s"] = round((end_dt - start_dt).total_seconds(), 2)
        except (ValueError, TypeError):
            target["duration_s"] = None

    if error_msg:
        target["error"] = error_msg

    save_log(log_path, entries)
    return target


def main():
    if len(sys.argv) < 4:
        print(
            "Usage:\n"
            f"  {sys.argv[0]} start <workspace> <stage_name>\n"
            f"  {sys.argv[0]} end <workspace> <stage_name> <true|false> [error_message]",
            file=sys.stderr,
        )
        sys.exit(1)

    action = sys.argv[1].lower()
    workspace = sys.argv[2]
    stage_name = sys.argv[3]

    if action == "start":
        entry = handle_start(workspace, stage_name)
        print(json.dumps(entry, indent=2, ensure_ascii=False))

    elif action == "end":
        if len(sys.argv) < 5:
            print(f"Usage: {sys.argv[0]} end <workspace> <stage_name> <true|false> [error_message]", file=sys.stderr)
            sys.exit(1)

        success_str = sys.argv[4].lower()
        success = success_str in ("true", "1", "yes")
        error_msg = sys.argv[5] if len(sys.argv) > 5 else None

        entry = handle_end(workspace, stage_name, success, error_msg)
        print(json.dumps(entry, indent=2, ensure_ascii=False))

    else:
        print(f"Unknown action: {action}. Use 'start' or 'end'.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Migrate legacy root-level run-context files into per-workspace subfolders.

Old layout (ticker root):
    {date}_shared_context.json
    {date}_memory_context_{role}.json

New layout:
    shared_context/{date}.json
    memory/{date}_{role}.json

Idempotent and role-generic (handles any {role}, e.g. analyst/writer/reviewer/
reflect). Dry-run by default; pass --apply to move files. Workspaces are
gitignored, so this is a plain filesystem move (no commit).

Usage:
    .venv/bin/python3 scripts/migrate_context_layout.py [--apply] [--root DIR]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATE = r"\d{4}-\d{2}-\d{2}"
SHARED_RE = re.compile(rf"^({DATE})_shared_context\.json$")
MEMORY_RE = re.compile(rf"^({DATE})_memory_context_(.+)\.json$")


def planned_moves(workspaces: Path):
    """Yield (src, dst) for every legacy context file under workspaces/*/."""
    if not workspaces.is_dir():
        return
    for ws in sorted(workspaces.iterdir()):
        if not ws.is_dir():
            continue
        for child in sorted(ws.iterdir()):
            if not child.is_file():
                continue
            m = SHARED_RE.match(child.name)
            if m:
                yield child, ws / "shared_context" / f"{m.group(1)}.json"
                continue
            m = MEMORY_RE.match(child.name)
            if m:
                date, role = m.group(1), m.group(2)
                yield child, ws / "memory" / f"{date}_{role}.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform the moves (default: dry-run)")
    ap.add_argument("--root", default=str(ROOT / "workspaces"), help="workspaces dir")
    args = ap.parse_args()

    workspaces = Path(args.root)
    moved = skipped = 0
    for src, dst in planned_moves(workspaces):
        rel_src = src.relative_to(workspaces.parent)
        rel_dst = dst.relative_to(workspaces.parent)
        if dst.exists():
            print(f"SKIP (target exists): {rel_src}")
            skipped += 1
            continue
        print(f"{'MOVE' if args.apply else 'PLAN'}: {rel_src} -> {rel_dst}")
        if args.apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            moved += 1

    verb = "moved" if args.apply else "would move"
    print(f"\n{verb}: {moved if args.apply else sum(1 for _ in planned_moves(workspaces)) - skipped}"
          f"  skipped(existing): {skipped}")
    if not args.apply:
        print("(dry-run — re-run with --apply to perform the moves)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

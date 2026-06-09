#!/usr/bin/env python3
"""Prune the long-term memory-mcp store down to a ticker keep-list.

Filters memory/{episodic,semantic,procedural}/index.jsonl so that only records
whose resolved ticker is in KEEP (or that are GLOBAL / process-learning memories)
survive. Records are keyed off their OWN primary ticker (tags.ticker, else parsed
from id) -- never related_entities. Idempotent.

Usage:
    python3 scripts/prune_memory_to_keeplist.py            # dry-run (default)
    python3 scripts/prune_memory_to_keeplist.py --apply    # back up + rewrite
    python3 scripts/prune_memory_to_keeplist.py --apply --backup-dir <path>

Never touches anything under workspaces/ or the personal Claude memory.
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = PROJECT_ROOT / "memory"
MEMORY_TYPES = ("episodic", "semantic", "procedural")

# Companies/tickers to retain, exactly as they appear in tags.ticker / id.
KEEP = {"0293", "0941", "1810", "PDD", "BTC", "HOOD", "BRK.B"}
GLOBAL_TICKER = "GLOBAL"  # cross-company process memories, always kept

# Exchange/market suffixes that denote the SAME company (e.g. 0293 == 0293.HK).
# NOTE: share-class dots like ".B" in BRK.B are NOT exchange codes and are kept.
EXCHANGE_SUFFIXES = (".HK", ".SS", ".SZ", ".KS", ".SH", ".T", ".L")


def _strip_exchange_suffix(t: str) -> str:
    for suf in EXCHANGE_SUFFIXES:
        if t.endswith(suf):
            return t[: -len(suf)]
    return t


def _in_keep(t: str) -> bool:
    """True if ticker t (or its exchange-stripped form) is in the keep-list."""
    return t in KEEP or _strip_exchange_suffix(t) in KEEP


def _read_jsonl(fp: Path) -> list[dict]:
    if not fp.exists():
        return []
    out = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _write_jsonl(fp: Path, records: list[dict]) -> None:
    fp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def resolve_ticker(rec: dict) -> str | None:
    """Return the record's own primary ticker, or None if unresolvable."""
    t = (rec.get("tags") or {}).get("ticker")
    if isinstance(t, str) and t.strip():
        return t.strip()
    # Fall back to the id: mem_{ticker}_{date}_{type}_{seq}
    rid = rec.get("id")
    if isinstance(rid, str) and rid.startswith("mem_"):
        parts = rid.split("_")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def keep_record(rec: dict) -> bool:
    t = resolve_ticker(rec)
    if t is None:
        return True  # unresolvable -> treat as global/process, keep
    if t == GLOBAL_TICKER:
        return True
    return _in_keep(t)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="back up and rewrite the files (default is dry-run)")
    ap.add_argument("--backup-dir", default=str(MEMORY_DIR / "_backup_pre_prune_2026-06-09"),
                    help="where to copy the original jsonl files before rewriting")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"== prune_memory_to_keeplist [{mode}] ==")
    print(f"keep tickers: {sorted(KEEP)} (+ GLOBAL/unresolvable)")
    print(f"memory dir:   {MEMORY_DIR}\n")

    backup_dir = Path(args.backup_dir)
    if args.apply:
        backup_dir.mkdir(parents=True, exist_ok=True)

    grand_kept = grand_removed = 0
    for mt in MEMORY_TYPES:
        fp = MEMORY_DIR / mt / "index.jsonl"
        records = _read_jsonl(fp)
        kept, removed = [], []
        for r in records:
            (kept if keep_record(r) else removed).append(r)

        removed_by_ticker = collections.Counter(
            resolve_ticker(r) or "<unresolved>" for r in removed
        )
        kept_tickers = sorted({resolve_ticker(r) or "<unresolved>" for r in kept})

        print(f"[{mt}] total={len(records)} kept={len(kept)} removed={len(removed)}")
        if removed_by_ticker:
            print(f"    removed-by-ticker: {dict(sorted(removed_by_ticker.items()))}")
        print(f"    kept tickers: {kept_tickers}")

        if args.apply and fp.exists():
            shutil.copy2(fp, backup_dir / f"{mt}.index.jsonl")
            _write_jsonl(fp, kept)

        grand_kept += len(kept)
        grand_removed += len(removed)

    print(f"\nTOTAL kept={grand_kept} removed={grand_removed}")
    if args.apply:
        print(f"backup written to: {backup_dir}")
    else:
        print("(dry-run -- no files changed; re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

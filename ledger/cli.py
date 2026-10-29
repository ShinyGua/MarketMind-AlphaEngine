#!/usr/bin/env python3
"""Ledger commands. Run from the repo root:

    .venv/bin/python3 ledger/cli.py init                    # seal the preregistration (record 1)
    .venv/bin/python3 ledger/cli.py seal  <workspace> <date> # after the pre-release gate passed
    .venv/bin/python3 ledger/cli.py block <workspace> <date> # record a gate-blocked release
    .venv/bin/python3 ledger/cli.py verify                  # recompute every hash and link
    .venv/bin/python3 ledger/cli.py stamps                  # submit/upgrade OpenTimestamps receipts

Data root: MM_DATA_ROOT (default ~/MarketMindData), never inside iCloud.
Exit: 0 ok, 1 refused or failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import seal, stamp, store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MarketMind decision ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    for name in ("seal", "block"):
        p = sub.add_parser(name)
        p.add_argument("workspace", type=Path)
        p.add_argument("date")
    sub.add_parser("verify")
    sub.add_parser("stamps")
    args = ap.parse_args(argv)
    try:
        root = store.check_root(store.data_root())
        if args.cmd == "init":
            rec = seal.init(root)
            print(f"ledger: protocol is record {rec['seq']} ({rec['hash'][:12]}) in {root}")
        elif args.cmd == "seal":
            receipt = seal.seal(args.workspace, args.date, root)
            print(f"ledger: sealed {receipt['decision_id']} as record {receipt['seq']} "
                  f"(stamp {receipt['stamp_status']})")
        elif args.cmd == "block":
            rec = seal.record_block(args.workspace, args.date, root)
            print("ledger: no protocol record; block not recorded" if rec is None
                  else f"ledger: recorded gate block as record {rec['seq']}")
        elif args.cmd == "verify":
            result = store.verify(root)
            result["stamps"] = stamp.stamp_summary(root)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        elif args.cmd == "stamps":
            changed = stamp.upgrade_all(root)
            print(f"ledger: {len(changed)} stamp record(s) appended; "
                  f"now {stamp.stamp_summary(root)}")
    except store.LedgerError as exc:
        print(f"ledger: REFUSED — {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Append-only, hash-chained decision ledger.

One file, ``{root}/ledger.jsonl``, one JSON record per line:

    {"seq": 7, "type": "decision", "prev_hash": "<hash of record 6>",
     "body": {...}, "hash": sha256(canonical({seq, type, prev_hash, body}))}

Record types: protocol (the preregistration — always record 1), decision,
gate_block, outcome, stamp, drill. Corrections are new records; nothing is
ever rewritten. ``verify`` recomputes every hash and link, so an edited,
dropped, reordered or truncated record is detected. A hash chain alone does not
stop someone rewriting the whole file — external OpenTimestamps receipts on
record hashes (ledger/stamp.py) are what fix records in time.

The root (``MM_DATA_ROOT``, default ``~/MarketMindData``) must live outside
iCloud-synced folders: iCloud evicts and re-syncs files, which breaks file
locking and can silently fork the chain.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

GENESIS = "0" * 64
RECORD_TYPES = {"protocol", "decision", "gate_block", "outcome", "stamp", "drill"}
_ICLOUD = ("Documents", "Desktop", "Library/Mobile Documents")


class LedgerError(RuntimeError):
    pass


def data_root() -> Path:
    return Path(os.environ.get("MM_DATA_ROOT", "~/MarketMindData")).expanduser()


def check_root(root: Path) -> Path:
    """Refuse iCloud-synced locations; return the resolved root."""
    resolved = root.expanduser().resolve()
    home = Path.home().resolve()
    for sub in _ICLOUD:
        synced = (home / sub).resolve()
        if resolved == synced or synced in resolved.parents:
            raise LedgerError(f"{resolved} is inside iCloud-synced {synced}; "
                              "set MM_DATA_ROOT outside ~/Documents and ~/Desktop")
    return resolved


def canonical(obj) -> bytes:
    """Deterministic bytes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def record_hash(seq: int, rtype: str, prev_hash: str, body: dict) -> str:
    return hashlib.sha256(canonical({"seq": seq, "type": rtype, "prev_hash": prev_hash,
                                     "body": body})).hexdigest()


def ledger_path(root: Path) -> Path:
    return root / "ledger.jsonl"


@contextmanager
def _locked(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "ledger.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _last_record(path: Path) -> dict | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        chunk = b""
        while pos > 0:
            step = min(4096, pos)
            pos -= step
            fh.seek(pos)
            chunk = fh.read(step) + chunk
            lines = chunk.rstrip(b"\n").split(b"\n")
            if len(lines) > 1 or pos == 0:
                last = lines[-1]
                break
    if not chunk.endswith(b"\n"):
        raise LedgerError("ledger ends in a partial record (interrupted write); run verify")
    try:
        return json.loads(last)
    except ValueError as exc:
        raise LedgerError(f"last ledger record is unreadable: {exc}") from exc


def append(root: Path, rtype: str, body: dict) -> dict:
    """Append one record under an exclusive lock; fsync before returning."""
    if rtype not in RECORD_TYPES:
        raise LedgerError(f"unknown record type {rtype!r}")
    root = check_root(root)
    path = ledger_path(root)
    with _locked(root):
        last = _last_record(path)
        if rtype != "protocol" and last is None:
            raise LedgerError("ledger has no protocol record; run `ledger init` first")
        seq = (last["seq"] + 1) if last else 1
        prev = last["hash"] if last else GENESIS
        rec = {"seq": seq, "type": rtype, "prev_hash": prev, "body": body,
               "hash": record_hash(seq, rtype, prev, body)}
        with open(path, "ab") as fh:
            fh.write(canonical(rec) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
    return rec


def read(root: Path) -> list[dict]:
    path = ledger_path(root)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def verify(root: Path) -> dict:
    """Recompute every hash and link. Never raises on bad content."""
    path = ledger_path(root)
    out = {"ok": True, "count": 0, "head": None, "errors": []}
    if not path.exists():
        return out
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        out["errors"].append("file ends in a partial record (truncated or interrupted write)")
    prev, expect_seq = GENESIS, 1
    for n, line in enumerate(raw.split(b"\n"), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            out["errors"].append(f"line {n}: unreadable JSON")
            continue
        seq, rtype, body = rec.get("seq"), rec.get("type"), rec.get("body")
        if seq != expect_seq:
            out["errors"].append(f"line {n}: seq {seq}, expected {expect_seq}")
        if rec.get("prev_hash") != prev:
            out["errors"].append(f"line {n}: prev_hash does not link to the previous record")
        if record_hash(seq, rtype, rec.get("prev_hash"), body) != rec.get("hash"):
            out["errors"].append(f"line {n}: hash mismatch (record content changed)")
        if seq == 1 and rtype != "protocol":
            out["errors"].append("record 1 is not the protocol record")
        prev, expect_seq = rec.get("hash"), (seq or expect_seq) + 1
        out["count"] += 1
        out["head"] = rec.get("hash")
    out["ok"] = not out["errors"]
    return out

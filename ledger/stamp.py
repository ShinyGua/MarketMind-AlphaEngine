"""OpenTimestamps receipts for ledger records.

Each sealed decision record's hash is written to ``{root}/stamps/{seq}.hash``
and submitted with ``ots stamp`` to the free public calendars. Only that hash
leaves the machine. The calendars return a *pending* receipt
(``{seq}.hash.ots``); hours later ``ots upgrade`` (which also needs only the
calendars) completes it with a Bitcoin block attestation.

Statuses, appended to the ledger as ``stamp`` records:
  not_submitted  the client is missing or no calendar answered — retried by upgrade_all
  pending        calendars acknowledged; no Bitcoin attestation yet
  attested       the receipt carries a Bitcoin block attestation
  failed         the receipt is missing or holds no attestation at all

A later upgrade appends a record only when the status changes (so a receipt
that is merely still waiting adds nothing).

``attested`` is not ``verified``: checking an attestation against the Bitcoin
chain needs a Bitcoin node or a trusted block explorer, and that deployment is
an open choice. A receipt proves the hash existed no later than its block's
time — never that the contents are true, nor when the decision was "really" made.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import store


def ots_binary() -> str | None:
    local = Path(sys.executable).parent / "ots"
    if local.exists():
        return str(local)
    return shutil.which("ots")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


STAMPED_TYPES = ("protocol", "decision")   # what init and seal stamp


def stamp_record(root: Path, record: dict, binary: str | None = "auto") -> dict:
    """Write the record hash to a file, submit it, append a stamp record."""
    binary = ots_binary() if binary == "auto" else binary
    stamps = root / "stamps"
    stamps.mkdir(parents=True, exist_ok=True)
    target = stamps / f"{record['seq']:06d}.hash"
    target.write_text(record["hash"] + "\n", encoding="utf-8")
    if binary is None:
        status, detail = "not_submitted", "OpenTimestamps client not installed"
    else:
        code, out = _run([binary, "stamp", str(target)])
        receipt = target.with_name(target.name + ".ots")
        status = "pending" if code == 0 and receipt.exists() else "not_submitted"
        detail = out[-300:]
    body = {"target_seq": record["seq"], "target_hash": record["hash"],
            "file": target.name, "status": status, "at": _now(), "detail": detail}
    return store.append(root, "stamp", body)


def _latest_status(records: list[dict]) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    for rec in records:
        if rec["type"] == "stamp":
            latest[rec["body"]["target_seq"]] = rec["body"]
    return latest


def _upgrade(binary: str, receipt: Path) -> tuple[str, str]:
    """Upgrade one receipt and read its state back from the receipt itself.

    ``ots upgrade`` exits 1 and prints "Failed! Timestamp not complete" whenever
    no Bitcoin block has confirmed the hash yet — the normal state for hours
    after stamping — so neither its exit code nor its wording separates
    "waiting" from "broken". ``ots info`` lists the attestations the receipt
    holds: a BitcoinBlockHeaderAttestation means attested (an attested receipt
    still lists other calendars as pending), a PendingAttestation alone means
    still waiting, and anything else (missing or unreadable receipt) is failed.
    """
    if not receipt.exists():
        return "failed", f"receipt missing: {receipt.name}"
    _code, out = _run([binary, "upgrade", str(receipt)])
    _icode, info = _run([binary, "info", str(receipt)])
    if "BitcoinBlockHeaderAttestation" in info:
        return "attested", out[-300:]
    if "PendingAttestation" in info:
        return "pending", out[-300:]
    return "failed", (out + "\n" + info)[-300:]


def upgrade_all(root: Path, binary: str | None = "auto") -> list[dict]:
    """Submit not-yet-submitted hashes and upgrade pending receipts."""
    binary = ots_binary() if binary == "auto" else binary
    if binary is None:
        return []
    records = store.read(root)
    by_seq = {r["seq"]: r for r in records}
    latest = _latest_status(records)
    changed = []
    # a seal or init that died between its append and its stamp left a record
    # with no stamp at all: submit it now, or it is never anchored
    for rec in records:
        if rec["type"] in STAMPED_TYPES and rec["seq"] not in latest:
            changed.append(stamp_record(root, rec, binary))
    for seq, last in sorted(latest.items()):
        if last["status"] in ("attested",):
            continue
        if last["status"] == "not_submitted":
            changed.append(stamp_record(root, by_seq[seq], binary))
            continue
        receipt = root / "stamps" / (last["file"] + ".ots")
        status, out = _upgrade(binary, receipt)
        if status == last["status"]:
            continue  # nothing new to record
        changed.append(store.append(root, "stamp", {
            "target_seq": seq, "target_hash": last["target_hash"], "file": last["file"],
            "status": status, "at": _now(), "detail": out}))
    return changed


def stamp_summary(root: Path) -> dict:
    counts: dict[str, int] = {}
    for body in _latest_status(store.read(root)).values():
        counts[body["status"]] = counts.get(body["status"], 0) + 1
    return counts

"""Off-site backup of the ledger data root with restic, and the restore drill.

3-2-1: the local data root, Time Machine (set up by the user), and an
encrypted restic repository off-site (Backblaze B2 or S3). This module only
drives restic; it never creates accounts or buckets.

Configuration (environment, as restic itself reads it):
  RESTIC_REPOSITORY        e.g. b2:bucket:marketmind or s3:s3.amazonaws.com/bucket
  RESTIC_PASSWORD_COMMAND  default: read the macOS Keychain item
                           "marketmind-restic" (security find-generic-password)
  provider credentials     B2_ACCOUNT_ID/B2_ACCOUNT_KEY or AWS_ACCESS_KEY_ID/...

The monthly drill restores the latest snapshot into a temporary directory and
checks, on the *restored* copy: the hash chain, every decision's manifest and
archive hash, and that every stamp record's receipt file is present. Success
and failure are both appended to the live ledger as ``drill`` records.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import store

KEYCHAIN_SERVICE = "marketmind-restic"
INCLUDE = ("ledger.jsonl", "manifests", "archive", "prices", "stamps", "reports")


def _env() -> dict:
    env = dict(os.environ)
    if not env.get("RESTIC_PASSWORD") and not env.get("RESTIC_PASSWORD_COMMAND"):
        env["RESTIC_PASSWORD_COMMAND"] = f"security find-generic-password -s {KEYCHAIN_SERVICE} -w"
    return env


def restic_binary() -> str | None:
    return shutil.which("restic")


def _restic(args: list[str], env: dict, timeout: int = 3600) -> tuple[int, str]:
    binary = restic_binary()
    if binary is None:
        return 127, "restic not installed (brew install restic)"
    try:
        p = subprocess.run([binary, *args], capture_output=True, text=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return p.returncode, (p.stdout + p.stderr).strip()


def configured(env: dict | None = None) -> bool:
    return bool((env or os.environ).get("RESTIC_REPOSITORY"))


def backup(root: Path, env: dict | None = None) -> dict:
    env = env or _env()
    if not configured(env):
        return {"ok": False, "reason": "RESTIC_REPOSITORY not set — off-site backup not configured"}
    paths = [str(root / p) for p in INCLUDE if (root / p).exists()]
    code, out = _restic(["backup", "--json", "--tag", "marketmind-ledger", *paths], env)
    snapshot = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("message_type") == "summary":
            snapshot = ev.get("snapshot_id")
    return {"ok": code == 0 and snapshot is not None, "snapshot": snapshot,
            "detail": out[-300:] if code else None}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_copy(copy: Path) -> dict:
    """Verify a restored data root: chain, manifests, archives, receipts."""
    errors = []
    chain = store.verify(copy)
    errors += [f"chain: {e}" for e in chain["errors"]]
    stamps_seen = {}
    for rec in store.read(copy):
        body = rec["body"]
        if rec["type"] == "decision":
            man = copy / "manifests" / f"{body['decision_id']}.json"
            arc = copy / "archive" / f"{body['decision_id']}.tar.gz"
            if not man.exists() or _sha(man) != body.get("manifest_sha256"):
                errors.append(f"manifest missing or changed for {body['decision_id']}")
            if not arc.exists() or _sha(arc) != body.get("archive_sha256"):
                errors.append(f"archive missing or changed for {body['decision_id']}")
        elif rec["type"] == "stamp":
            stamps_seen[body["target_seq"]] = body
    for seq, body in stamps_seen.items():
        if body["status"] in ("pending", "attested"):
            receipt = copy / "stamps" / (body["file"] + ".ots")
            if not receipt.exists():
                errors.append(f"receipt missing for record {seq}")
    return {"ok": not errors, "records": chain["count"], "errors": errors}


def drill(root: Path, env: dict | None = None) -> dict:
    """Restore the latest snapshot to a temp dir, check it, log the result."""
    env = env or _env()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not configured(env):
        result = {"ok": False, "at": at, "reason": "RESTIC_REPOSITORY not set — nothing to restore"}
    else:
        with tempfile.TemporaryDirectory(prefix="mm-drill-") as tmp:
            code, out = _restic(["restore", "latest", "--tag", "marketmind-ledger",
                                 "--target", tmp], env)
            if code != 0:
                result = {"ok": False, "at": at, "reason": f"restore failed: {out[-200:]}"}
            else:
                restored = [p for p in Path(tmp).rglob("ledger.jsonl")]
                if not restored:
                    result = {"ok": False, "at": at, "reason": "snapshot has no ledger.jsonl"}
                else:
                    check = check_copy(restored[0].parent)
                    result = {"at": at, **check}
    if store.read(root):
        store.append(root, "drill", result)
    return result

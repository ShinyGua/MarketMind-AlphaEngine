"""Seal a released decision into the ledger, or record a blocked one.

Sealing happens right after the pre-release gate passes and before export. It
refuses unless (a) the preregistration protocol is record 1 of the ledger,
(b) the gate passed this exact file (its recorded sha256 still matches) and the
decision still passes when re-evaluated at the seal's own issuance time — the
verdict file alone is writable by the LLM steps it judges — and (c) the ledger
holds no release outcome (decision or gate_block) for this ticker and run date
yet: a run date has one outcome, so a crash, a retry or `--from decide` cannot
add a second scored forecast. (c) is checked under the ledger lock.

A decision record carries everything needed to score it later without trusting
the workspace: the issuance time from this code's clock (never an LLM-written
field), the forecast, both actions, the final-round ballots with conviction-
and equal-weight tallies, and hashes of every dated input artifact (manifest),
the resolved config, the skills tree and the code version. The run's dated
artifacts plus a profile snapshot are archived as ``archive/{id}.tar.gz``.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import stamp, store

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_FILE = Path(__file__).resolve().parent / "protocol_v1.json"
DATED_DIRS = ("raw", "normalized", "quant", "valuation", "discussion", "drafts", "reviews",
              "decision")
LABELS = ("BUY", "HOLD", "SELL")


def _release_check(ws: Path, date: str, at: datetime | None = None) -> dict:
    if str(ROOT / "eval") not in sys.path:
        sys.path.insert(0, str(ROOT / "eval"))
    from pre_release_gate import release_check
    return release_check(ws, date, at=at, ledger=False)


def _same_run(ws: Path, date: str):
    """Records that settle this run date's release: its seal or its block."""
    def match(rec: dict) -> bool:
        body = rec.get("body") or {}
        return (rec.get("type") in ("decision", "gate_block")
                and body.get("ticker") == ws.name and body.get("run_date") == date)
    return match


def outcome(ws: Path, date: str, root: Path | None = None) -> dict | None:
    """The release outcome the ledger holds for this run date, or None when it holds
    none (no ledger file counts as none). Raises store.LedgerError when the ledger
    cannot be read completely: it may hold an outcome that must be respected."""
    root = store.check_root(root or store.data_root())
    rec = next((r for r in store.read_complete(root) if _same_run(ws, date)(r)), None)
    if rec is None:
        return None
    return {"type": rec["type"], "seq": rec["seq"], "issued_at": rec["body"].get("issued_at"),
            "decision_sha256": ((rec["body"].get("gate") or {}).get("decision_sha256"))}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def has_protocol(root: Path) -> bool:
    recs = store.read_complete(root)   # a torn or corrupt ledger refuses, never crashes
    return bool(recs) and recs[0]["type"] == "protocol"


def init(root: Path, stamp_binary: str | None = "auto") -> dict:
    """Seal the preregistration as record 1 (idempotent)."""
    root = store.check_root(root)
    if has_protocol(root):
        return store.read(root)[0]
    text = PROTOCOL_FILE.read_text(encoding="utf-8")
    body = {"protocol": json.loads(text),
            "protocol_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "sealed_at": _now().isoformat(timespec="seconds")}
    rec = store.append(root, "protocol", body)
    stamp.stamp_record(root, rec, stamp_binary)
    return rec


def _run_files(ws: Path, date: str) -> list[Path]:
    """Every dated artifact of the run plus the undated inputs."""
    paths = []
    for sub in DATED_DIRS:
        base = ws / sub / date
        if base.is_dir():
            paths += [p for p in sorted(base.rglob("*")) if p.is_file()]
    paths += [p for p in (ws / "shared_context" / f"{date}.json", ws / "resolved_config.json")
              if p.is_file()]
    if (ws / "profile").is_dir():
        paths += [p for p in sorted((ws / "profile").rglob("*")) if p.is_file()]
    return paths


def snapshot(ws: Path, date: str, dest: Path) -> tuple[str, dict]:
    """Archive the run to ``dest`` and hash each file from the bytes archived.

    One read per file, so the manifest describes exactly the archive even while
    something else writes to the workspace. Returns (archive sha256, manifest).
    """
    files = {}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for p in _run_files(ws, date):
            try:
                data = p.read_bytes()
            except OSError:   # removed since it was listed
                continue
            rel = str(p.relative_to(ws))
            info = tarfile.TarInfo(f"{ws.name}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            files[rel] = hashlib.sha256(data).hexdigest()
    return _sha(dest), files


def skills_sha() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / ".claude" / "skills").rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(ROOT)).encode() + b"\0" + _sha(p).encode() + b"\n")
    return h.hexdigest()


def git_state() -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
    head = run("rev-parse", "HEAD")
    diff = run("diff", "HEAD")
    return {"head": head.decode().strip() if head else None,
            "dirty_diff_sha256": hashlib.sha256(diff).hexdigest() if diff else None}


def final_ballots(ws: Path, date: str) -> list[dict]:
    rounds = sorted((ws / "decision" / date / "panel").glob("round_*"),
                    key=lambda p: int(p.name.split("_")[-1]) if p.name.split("_")[-1].isdigit() else 0)
    if not rounds:
        return []
    keep = ("role", "round", "vote", "conviction", "risk_overlay", "action_if_flat",
            "action_if_long", "p_outperform")
    out = []
    for path in sorted(rounds[-1].glob("*_ballot.json")):
        try:
            b = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({k: b.get(k) for k in keep})
    return out


def tallies(ballots: list[dict]) -> dict:
    """Head-count, conviction-weighted and equal-weight views of the same ballots."""
    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    head = {lab: sum(1 for b in ballots if b.get("vote") == lab) for lab in LABELS}
    conv = {lab: round(sum(num(b.get("conviction")) or 0.0 for b in ballots if b.get("vote") == lab), 4)
            for lab in LABELS}
    ps = [(num(b.get("p_outperform")), num(b.get("conviction")) or 0.0) for b in ballots]
    ps = [(p, c) for p, c in ps if p is not None]
    mean_p = round(sum(p for p, _ in ps) / len(ps), 4) if ps else None
    wsum = sum(c for _, c in ps)
    conv_p = round(sum(p * c for p, c in ps) / wsum, 4) if ps and wsum > 0 else None
    return {"n_ballots": len(ballots), "head_count": head, "conviction_weighted": conv,
            "p_outperform_equal_weight": mean_p, "p_outperform_conviction_weighted": conv_p}


def _profile(ws: Path) -> dict:
    try:
        return json.loads((ws / "profile" / "company_profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def seal(ws: Path, date: str, root: Path | None = None, now: datetime | None = None,
         stamp_binary: str | None = "auto") -> dict:
    """Seal a gate-passed decision. Raises store.LedgerError on refusal."""
    root = store.check_root(root or store.data_root())
    if not has_protocol(root):
        raise store.LedgerError("no protocol record — run `ledger init` before sealing")
    prior = outcome(ws, date, root)
    if prior:
        raise store.LedgerError(f"{ws.name} {date} already has a release outcome: record "
                                f"{prior['seq']} ({prior['type']}); a run date is sealed once")
    issued_at = now or _now()
    chk = _release_check(ws, date, issued_at)
    if chk["status"] != "passed":
        why = "; ".join(f"{e.get('check')}: {e.get('message')}" for e in chk["errors"][:3])
        raise store.LedgerError(f"pre-release gate status is {chk['status']!r}; only a passed, "
                                "unedited decision can be sealed" + (f" ({why})" if why else ""))
    # Seal exactly the bytes that were re-checked, not a later re-read.
    dec, gate = chk["decision"], chk["gate"]
    profile = _profile(ws)
    decision_id = f"{ws.name}_{date}_{issued_at.strftime('%Y%m%dT%H%M%SZ')}"

    # Files are named per attempt: a seal that loses the race under the ledger
    # lock never overwrites the winner's archive, and removes only its own.
    stem = f"{decision_id}_{uuid.uuid4().hex[:8]}"
    arch_path = root / "archive" / f"{stem}.tar.gz"
    man_path = root / "manifests" / f"{stem}.json"
    arch_sha, files = snapshot(ws, date, arch_path)
    if files.get(f"decision/{date}/final_decision.json") != chk["sha256"]:
        arch_path.unlink(missing_ok=True)
        raise store.LedgerError("final_decision.json changed while it was being sealed")
    man = {"decision_id": decision_id, "workspace": ws.name, "run_date": date, "files": files}
    man_path.parent.mkdir(parents=True, exist_ok=True)
    man_path.write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")
    ballots = final_ballots(ws, date)

    body = {
        "decision_id": decision_id,
        "ticker": ws.name,
        "symbol": profile.get("yf_ticker") or ws.name,
        "market": (profile.get("market_profile") or "US").upper(),
        "exchange": profile.get("exchange"),
        "run_date": date,
        "issued_at": issued_at.isoformat(timespec="seconds"),
        "headline": {"decision": dec.get("decision"), "risk_overlay": dec.get("risk_overlay"),
                     **(dec.get("derived_fields") or {})},
        "actions": dec.get("actions"),
        "forecast": dec.get("forecast"),
        "confidence": dec.get("confidence"),
        "ballots": ballots,
        "tallies": tallies(ballots),
        "gate": {"status": chk["status"], "decision_sha256": chk["sha256"],
                 "warnings": len(gate.get("warnings") or [])},
        "gate_revision": dec.get("gate_revision"),
        "manifest_file": man_path.name,
        "manifest_sha256": _sha(man_path),
        "archive_file": arch_path.name,
        "archive_sha256": arch_sha,
        "config_sha256": files.get("resolved_config.json"),
        "skills_sha256": skills_sha(),
        "git": git_state(),
    }
    try:
        rec = store.append(root, "decision", body, conflicts=_same_run(ws, date))
    except store.LedgerError:
        man_path.unlink(missing_ok=True)
        arch_path.unlink(missing_ok=True)
        raise
    stamp_rec = stamp.stamp_record(root, rec, stamp_binary)   # after the lock: network
    receipt = {"decision_id": decision_id, "seq": rec["seq"], "hash": rec["hash"],
               "issued_at": body["issued_at"], "stamp_status": stamp_rec["body"]["status"],
               "ledger": str(store.ledger_path(root))}
    (ws / "decision" / date / "ledger_receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def record_block(ws: Path, date: str, root: Path | None = None) -> dict | None:
    """Append a gate_block record for a release the gate stopped (no seal).
    Refuses when the decision passes, or when the run date already has an outcome."""
    root = store.check_root(root or store.data_root())
    if not has_protocol(root):
        return None
    chk = _release_check(ws, date)
    if chk["status"] == "passed":
        raise store.LedgerError("the pre-release gate passed this decision; there is no block to record")
    body = {"ticker": ws.name, "run_date": date, "at": _now().isoformat(timespec="seconds"),
            "errors": [f"{e.get('check')}: {e.get('message')}" for e in chk["errors"]]
                      or [f"gate result {chk['status']}"]}
    return store.append(root, "gate_block", body, conflicts=_same_run(ws, date))

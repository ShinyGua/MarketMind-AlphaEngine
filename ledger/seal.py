"""Seal a released decision into the ledger, or record a blocked one.

Sealing happens right after the pre-release gate passes and before export. It
refuses unless (a) the preregistration protocol is record 1 of the ledger and
(b) the gate passed this exact file (its recorded sha256 still matches).

A decision record carries everything needed to score it later without trusting
the workspace: the issuance time from this code's clock (never an LLM-written
field), the forecast, both actions, the final-round ballots with conviction-
and equal-weight tallies, and hashes of every dated input artifact (manifest),
the resolved config, the skills tree and the code version. The run's dated
artifacts plus a profile snapshot are archived as ``archive/{id}.tar.gz``.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import stamp, store

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_FILE = Path(__file__).resolve().parent / "protocol_v1.json"
DATED_DIRS = ("raw", "normalized", "quant", "valuation", "discussion", "drafts", "reviews",
              "decision")
LABELS = ("BUY", "HOLD", "SELL")


def _gate_status(ws: Path, date: str) -> str:
    if str(ROOT / "eval") not in sys.path:
        sys.path.insert(0, str(ROOT / "eval"))
    from pre_release_gate import gate_status
    return gate_status(ws, date)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def has_protocol(root: Path) -> bool:
    recs = store.read(root)
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


def manifest(ws: Path, date: str) -> dict:
    """sha256 of every dated artifact of the run plus the undated inputs."""
    files = {}
    for sub in DATED_DIRS:
        base = ws / sub / date
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    files[str(p.relative_to(ws))] = _sha(p)
    for p in [ws / "shared_context" / f"{date}.json", ws / "resolved_config.json",
              *sorted((ws / "profile").glob("*.json"))]:
        if p.is_file():
            files[str(p.relative_to(ws))] = _sha(p)
    return files


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


def archive(ws: Path, date: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for sub in DATED_DIRS:
            base = ws / sub / date
            if base.is_dir():
                tar.add(base, arcname=f"{ws.name}/{sub}/{date}")
        for p in [ws / "shared_context" / f"{date}.json", ws / "resolved_config.json"]:
            if p.is_file():
                tar.add(p, arcname=f"{ws.name}/{p.relative_to(ws)}")
        if (ws / "profile").is_dir():
            tar.add(ws / "profile", arcname=f"{ws.name}/profile")
    return _sha(dest)


def seal(ws: Path, date: str, root: Path | None = None, now: datetime | None = None,
         stamp_binary: str | None = "auto") -> dict:
    """Seal a gate-passed decision. Raises store.LedgerError on refusal."""
    root = store.check_root(root or store.data_root())
    if not has_protocol(root):
        raise store.LedgerError("no protocol record — run `ledger init` before sealing")
    status = _gate_status(ws, date)
    if status != "passed":
        raise store.LedgerError(f"pre-release gate status is {status!r}; only a passed, "
                                "unedited decision can be sealed")
    issued_at = now or _now()
    dec_path = ws / "decision" / date / "final_decision.json"
    dec = json.loads(dec_path.read_text(encoding="utf-8"))
    gate = json.loads((ws / "decision" / date / "pre_release_gate.json").read_text(encoding="utf-8"))
    profile = _profile(ws)
    decision_id = f"{ws.name}_{date}_{issued_at.strftime('%Y%m%dT%H%M%SZ')}"

    files = manifest(ws, date)
    man = {"decision_id": decision_id, "workspace": ws.name, "run_date": date, "files": files}
    man_path = root / "manifests" / f"{decision_id}.json"
    man_path.parent.mkdir(parents=True, exist_ok=True)
    man_path.write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")
    arch_sha = archive(ws, date, root / "archive" / f"{decision_id}.tar.gz")
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
        "gate": {"status": gate.get("status"), "decision_sha256": gate.get("decision_sha256"),
                 "warnings": len(gate.get("warnings") or [])},
        "gate_revision": dec.get("gate_revision"),
        "manifest_sha256": _sha(man_path),
        "archive_sha256": arch_sha,
        "config_sha256": files.get("resolved_config.json"),
        "skills_sha256": skills_sha(),
        "git": git_state(),
    }
    rec = store.append(root, "decision", body)
    stamp_rec = stamp.stamp_record(root, rec, stamp_binary)
    receipt = {"decision_id": decision_id, "seq": rec["seq"], "hash": rec["hash"],
               "issued_at": body["issued_at"], "stamp_status": stamp_rec["body"]["status"],
               "ledger": str(store.ledger_path(root))}
    (ws / "decision" / date / "ledger_receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def record_block(ws: Path, date: str, root: Path | None = None) -> dict | None:
    """Append a gate_block record for a release the gate stopped (no seal)."""
    root = store.check_root(root or store.data_root())
    if not has_protocol(root):
        return None
    try:
        gate = json.loads((ws / "decision" / date / "pre_release_gate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        gate = {}
    body = {"ticker": ws.name, "run_date": date, "at": _now().isoformat(timespec="seconds"),
            "errors": [f"{e.get('check')}: {e.get('message')}" for e in gate.get("errors", [])]}
    return store.append(root, "gate_block", body)

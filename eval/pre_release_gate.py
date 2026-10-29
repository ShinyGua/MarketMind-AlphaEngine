#!/usr/bin/env python3
"""Pre-release gate — the deterministic check a decision must pass before export.

Runs at the end of the decide stage, before anything is rendered or sealed.
Unlike the advisory graders in reflect, a failure here BLOCKS the release.

Checks, in order:
  schema            final_decision.json is schema v2: two actions, a forecast,
                    structured top_reasons with evidence_ids and direction
  derive            headline `decision` / legacy `risk_overlay` are re-derived
                    from `actions` and the investor's declared position
  unresolved_id     every cited evidence id exists as a card file
  future_card       no cited card was published after issuance
  stale_chip /      every cited chip card carries its observation date and is
  undated_chip      fresh for its block's cadence (any label, overlay or read)
  k1_uncorroborated a BUY or SELL whose same-direction reasons cite chip
                    evidence also cites independent same-direction evidence
                    (company / filing / catalyst / company-specific news)

Warnings (never block): headline mismatch, suspected chip cards without a date,
a card whose sentiment opposes the reason citing it, a BUY/SELL with no
same-direction reason.

Writes decision/{date}/pre_release_gate.json with the sha256 of the decision
file as released, so the ledger seal can refuse an edited decision.

Usage:
    .venv/bin/python3 eval/pre_release_gate.py <workspace> <date>
    .venv/bin/python3 eval/pre_release_gate.py <workspace> <date> --audit --out <file>

--audit reads a historical (v1) decision without modifying anything: string
reasons are read as supporting the label, with ids taken from their text.

Exit: 0 passed, 2 blocked, 1 usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared import contracts, evidence_meta, freshness  # noqa: E402

LABELS = {"BUY", "HOLD", "SELL"}
OVERLAYS = {"none", "hedge", "trim", "stop"}
DIRECTIONS = {"up", "down", "neutral"}
LABEL_DIRECTION = {"BUY": "up", "SELL": "down"}
_ID_RE = re.compile(r"ev_\d{4}-?\d{2}-?\d{2}_[A-Za-z]+_\d{3}")


def _err(check: str, message: str, **extra) -> dict:
    return {"check": check, "message": message, **extra}


def load_cards(ws: Path, date: str) -> dict[str, dict]:
    """Every card file for the run, keyed by its id (file stem as fallback)."""
    cards: dict[str, dict] = {}
    for path in sorted((ws / "normalized" / date / "evidence_cards").glob("*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(card, dict):
            cards.setdefault(str(card.get("id") or path.stem), card)
            cards.setdefault(path.stem, card)
    return cards


def position_state(ws: Path) -> str:
    try:
        prof = json.loads(contracts.investor_profile_path(ws).read_text(encoding="utf-8"))
        state = str(prof.get("position_state") or "none").lower()
    except (OSError, ValueError, AttributeError):
        state = "none"
    return state if state in ("none", "holding", "adding") else "none"


def check_schema(dec: dict) -> list[dict]:
    errs = []
    if dec.get("schema_version") != 2:
        errs.append(_err("schema", "schema_version must be 2"))
    actions = dec.get("actions")
    if not isinstance(actions, dict):
        errs.append(_err("schema", "actions {if_flat, if_long} missing"))
    else:
        for track in ("if_flat", "if_long"):
            a = actions.get(track)
            if not isinstance(a, dict) or a.get("label") not in LABELS:
                errs.append(_err("schema", f"actions.{track}.label must be BUY|HOLD|SELL"))
            elif a.get("overlay", "none") not in OVERLAYS:
                errs.append(_err("schema", f"actions.{track}.overlay must be none|hedge|trim|stop"))
    fc = dec.get("forecast")
    p = fc.get("p_outperform") if isinstance(fc, dict) else None
    if not isinstance(p, (int, float)) or isinstance(p, bool) or not 0 <= p <= 1:
        errs.append(_err("schema", "forecast.p_outperform must be a number in [0, 1]"))
    q = fc.get("excess_quantiles") if isinstance(fc, dict) else None
    if q is not None:
        vals = [q.get(k) if isinstance(q, dict) else None for k in ("p10", "p50", "p90")]
        if not all(isinstance(v, (int, float)) for v in vals) or not vals[0] <= vals[1] <= vals[2]:
            errs.append(_err("schema", "forecast.excess_quantiles must be ordered p10 <= p50 <= p90"))
    conf = dec.get("confidence")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0 <= conf <= 1:
        errs.append(_err("schema", "confidence must be a number in [0, 1]"))
    reasons = dec.get("top_reasons")
    if not isinstance(reasons, list) or not reasons:
        errs.append(_err("schema", "top_reasons must be a non-empty list"))
    else:
        for i, r in enumerate(reasons):
            if not (isinstance(r, dict) and isinstance(r.get("text"), str)
                    and isinstance(r.get("evidence_ids"), list)
                    and r.get("direction") in DIRECTIONS):
                errs.append(_err("schema", f"top_reasons[{i}] must be {{text, evidence_ids[], direction: up|down|neutral}}"))
    return errs


def derive_headline(dec: dict, state: str) -> dict | None:
    actions = dec.get("actions")
    track = "if_long" if state in ("holding", "adding") else "if_flat"
    a = actions.get(track) if isinstance(actions, dict) else None
    if not isinstance(a, dict) or a.get("label") not in LABELS:
        return None
    return {"decision": a["label"], "risk_overlay": a.get("overlay") or "none",
            "position_state": state, "track": track}


def audit_reasons(dec: dict) -> list[dict]:
    """v1 string reasons -> structured, assuming each supports the label."""
    direction = LABEL_DIRECTION.get(str(dec.get("decision", "")).upper(), "neutral")
    out = []
    for r in dec.get("top_reasons") or []:
        if isinstance(r, dict):
            out.append(r)
        else:
            text = str(r)
            out.append({"text": text, "evidence_ids": sorted(set(_ID_RE.findall(text))),
                        "direction": direction, "inferred": True})
    return out


def _labels(dec: dict) -> set[str]:
    labels = {str(dec.get("decision", "")).upper()}
    actions = dec.get("actions") if isinstance(dec.get("actions"), dict) else {}
    for track in ("if_flat", "if_long"):
        a = actions.get(track)
        if isinstance(a, dict):
            labels.add(str(a.get("label", "")).upper())
    return labels & LABELS


def evaluate(ws: Path, date: str, dec: dict, issued_at: datetime,
             audit: bool = False) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []
    schema_errors = check_schema(dec)
    reasons = audit_reasons(dec) if audit else [r for r in (dec.get("top_reasons") or [])
                                                if isinstance(r, dict)]
    if audit:
        warnings += [{**e, "check": "schema_v1"} for e in schema_errors]
    else:
        errors += schema_errors

    derived = None
    if not audit:
        derived = derive_headline(dec, position_state(ws))
        if derived and str(dec.get("decision", "")).upper() != derived["decision"]:
            warnings.append(_err("headline_mismatch",
                                 f"decision {dec.get('decision')} != {derived['track']} action "
                                 f"{derived['decision']}; using the action"))

    cards = load_cards(ws, date)
    ticker = ws.name
    cited = []
    for r in reasons:
        cited += [str(i) for i in (r.get("evidence_ids") or [])]
    cited += [str(i) for i in (dec.get("supporting_evidence_ids") or [])]
    classes: dict[str, dict] = {}
    for cid in dict.fromkeys(cited):
        card = cards.get(cid)
        if card is None:
            errors.append(_err("unresolved_id", f"{cid} is not an evidence card of this run",
                               evidence_id=cid))
            continue
        cls = evidence_meta.classify(card, ticker)
        classes[cid] = cls
        if evidence_meta.published_after(card, issued_at):
            errors.append(_err("future_card", f"{cid} published {card.get('published_at')}, "
                               f"after issuance {issued_at.isoformat()}", evidence_id=cid))
        if cls["chip_class"] == "chip":
            obs_date, basis = evidence_meta.observation_date(card)
            if obs_date is None:
                errors.append(_err("undated_chip", f"chip card {cid} has no observation date "
                                   "(meta.as_of; data cards need it explicitly)", evidence_id=cid))
            else:
                if basis == "published_at":
                    warnings.append(_err("as_of_from_published_at",
                                         f"news chip card {cid} dated by its publication date",
                                         evidence_id=cid))
                block = (((card.get("meta") or {}).get("lineage") or card.get("lineage") or {})
                         .get("block"))
                if block == "price_structure":
                    block = "turnover"  # same daily cadence
                elif block not in freshness.CADENCE:
                    block = "unknown"
                verdict = freshness.assess(block, obs_date, date, issued_at)
                if not verdict["fresh"]:
                    errors.append(_err("stale_chip", f"chip card {cid}: {verdict['reason']}",
                                       evidence_id=cid))
        elif cls["chip_class"] == "suspected_chip" and cls["as_of"] is None:
            warnings.append(_err("undated_suspected_chip",
                                 f"{cid} looks like chip/flow data but has no as_of", evidence_id=cid))

    for i, r in enumerate(reasons):
        d = r.get("direction")
        for cid in r.get("evidence_ids") or []:
            sent = str((cards.get(cid) or {}).get("sentiment", "")).lower()
            if (d == "up" and sent == "negative") or (d == "down" and sent == "positive"):
                warnings.append(_err("sentiment_conflict",
                                     f"top_reasons[{i}] points {d} but {cid} is {sent}",
                                     evidence_id=cid))

    # K1 — once per direction implied by any BUY/SELL among headline and actions.
    for label in sorted(_labels(dec) - {"HOLD"}):
        d = LABEL_DIRECTION[label]
        same = [r for r in reasons if r.get("direction") == d]
        if not same:
            warnings.append(_err("no_supporting_reason", f"{label} has no '{d}' reason"))
            continue
        ids = [str(i) for r in same for i in (r.get("evidence_ids") or []) if str(i) in classes]
        chip_ids = [i for i in ids if classes[i]["chip_class"] in ("chip", "suspected_chip")]
        if not chip_ids:
            continue
        independent = [i for i in ids if classes[i]["independent"]
                       and not evidence_meta.published_after(cards[i], issued_at)]
        if not independent:
            errors.append(_err("k1_uncorroborated",
                               f"{label}: '{d}' reasons rest on chip evidence ({', '.join(sorted(set(chip_ids)))}) "
                               "with no independent same-direction evidence "
                               "(company / filing / catalyst / company-specific news)"))

    counts = {
        "cited": len(dict.fromkeys(cited)),
        "chip": sum(1 for c in classes.values() if c["chip_class"] == "chip"),
        "suspected_chip": sum(1 for c in classes.values() if c["chip_class"] == "suspected_chip"),
        "independent": sum(1 for c in classes.values() if c["independent"]),
    }
    return {
        "gate": "pre_release",
        "version": 1,
        "mode": "audit" if audit else "release",
        "status": "blocked" if errors else "passed",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "issued_at": issued_at.isoformat(timespec="seconds"),
        "derived_fields": derived,
        "errors": errors,
        "warnings": warnings,
        "counts": counts,
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(ws: Path, date: str, now: datetime | None = None) -> dict:
    """Release mode: derive headline fields into the decision, check, record."""
    path = contracts.decision_path(ws, date)
    issued_at = now or datetime.now(timezone.utc)
    try:
        dec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result = {"gate": "pre_release", "version": 1, "mode": "release", "status": "blocked",
                  "checked_at": issued_at.isoformat(timespec="seconds"),
                  "issued_at": issued_at.isoformat(timespec="seconds"), "derived_fields": None,
                  "errors": [_err("schema", f"final_decision.json unreadable: {exc}")],
                  "warnings": [], "counts": {}}
    else:
        result = evaluate(ws, date, dec, issued_at)
        derived = result["derived_fields"]
        if derived:
            dec["decision"] = derived["decision"]
            dec["risk_overlay"] = derived["risk_overlay"]
            dec["derived_fields"] = {k: derived[k] for k in ("position_state", "track")}
            path.write_text(json.dumps(dec, indent=2, ensure_ascii=False), encoding="utf-8")
    result["decision_sha256"] = sha256_file(path) if path.exists() else None
    out = contracts.pre_release_gate_path(ws, date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def gate_status(ws: Path, date: str) -> str:
    """'passed' | 'blocked' | 'missing' | 'stale' (decision edited after the gate)."""
    try:
        gate = json.loads(contracts.pre_release_gate_path(ws, date).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "missing"
    if gate.get("status") != "passed":
        return "blocked"
    path = contracts.decision_path(ws, date)
    if not path.exists() or sha256_file(path) != gate.get("decision_sha256"):
        return "stale"
    return "passed"


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-release gate for final_decision.json")
    ap.add_argument("workspace", type=Path)
    ap.add_argument("date")
    ap.add_argument("--audit", action="store_true",
                    help="read-only check of a historical decision (no files in the workspace change)")
    ap.add_argument("--out", type=Path, help="audit mode: write the result here")
    args = ap.parse_args()
    if not args.workspace.is_dir():
        print(f"pre_release_gate: no workspace {args.workspace}", file=sys.stderr)
        return 1

    if args.audit:
        path = contracts.decision_path(args.workspace, args.date)
        try:
            dec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"pre_release_gate: {exc}", file=sys.stderr)
            return 1
        issued = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        result = evaluate(args.workspace, args.date, dec, issued, audit=True)
        text = json.dumps(result, indent=2, ensure_ascii=False)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0

    result = run(args.workspace, args.date)
    print(f"pre_release_gate: {result['status']} "
          f"({len(result['errors'])} error(s), {len(result['warnings'])} warning(s))")
    for e in result["errors"]:
        print(f"  BLOCK {e['check']}: {e['message']}")
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())

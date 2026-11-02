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
  ambiguous_id      ... and exactly one: two different card files claiming the
                    same id would let either one be judged
  future_card       no cited card was published after issuance
  stale_chip /      every cited chip card carries its observation date and is
  undated_chip      fresh for its block's cadence (any label, overlay or read),
                    on the workspace market's calendar
  no_supporting_    a BUY or SELL has at least one reason pointing its way
  reason
  k1_uncorroborated a BUY or SELL that leans on chip evidence — cited by a
                    same-direction or neutral reason, or a chip card whose own
                    sentiment points the label's way under any tag — also cites
                    independent, dated same-direction evidence
                    (company / filing / catalyst / company-specific news)

Warnings (never block): headline mismatch, suspected chip cards without a date,
cited cards without a parseable publication date, a card whose sentiment
opposes the reason citing it.

Writes decision/{date}/pre_release_gate.json with the sha256 of the decision
file as released. The gate never raises on LLM-written input: anything it
cannot evaluate is blocked. It removes any earlier verdict before it reads the
decision, so a crash can never leave an old "passed" in place.

Consumers (ledger seal, export, PDF renderer) call `release_check()`, which
re-evaluates the decision instead of trusting the verdict file: the file is
writable by the same LLM steps the gate guards against.

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
DIRECTION_SENTIMENT = {"up": "positive", "down": "negative"}
_ID_RE = re.compile(r"ev_\d{4}-?\d{2}-?\d{2}_[A-Za-z]+_\d{3}")

# Chip blocks a card's own fields imply: source type and dataset name, beside
# any block it names. The strictest wins, so a missing, mislabelled or
# capitalised block cannot buy a longer freshness window.
_SOURCE_TYPE_BLOCK = {"margin_data": "margin", "fund_flow": "main_force",
                      "northbound_holding": "northbound", "lhb": "lhb", "dragon_tiger": "lhb",
                      "holder_count": "holder_count", "turnover": "turnover"}
_DATASET_BLOCK = (("stock_margin", "margin"), ("margin_detail", "margin"),
                  ("stock_hsgt", "northbound"), ("fund_flow", "main_force"),
                  ("stock_lhb", "lhb"), ("stock_zh_a_gdhs", "holder_count"),
                  ("turnover_rate", "turnover"), ("stock_restricted_release", "restricted_release"))
# Daily bars, and an unlock schedule (a snapshot read on its as_of), are judged
# like a daily block. Strictest first; a card implying none of these is "unknown".
_JUDGED_AS = {"price_structure": "turnover", "restricted_release": "turnover"}
_STRICTEST_FIRST = ("turnover", "main_force", "northbound", "margin", "lhb", "holder_count")


def _err(check: str, message: str, **extra) -> dict:
    return {"check": check, "message": message, **extra}


def _list(value) -> list:
    """An LLM-written list field; anything else reads as empty (check_schema says so)."""
    return value if isinstance(value, list) else []


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def load_cards_checked(ws: Path, date: str) -> tuple[dict[str, dict], set[str]]:
    """Every card file for the run keyed by its id (file stem as fallback), plus
    the ids that two different card files claim."""
    cards: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for path in sorted((ws / "normalized" / date / "evidence_cards").glob("*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(card, dict):
            for key in dict.fromkeys((str(card.get("id") or path.stem), path.stem)):
                if key in cards and cards[key] != card:
                    ambiguous.add(key)
                cards.setdefault(key, card)
    return cards, ambiguous


def load_cards(ws: Path, date: str) -> dict[str, dict]:
    return load_cards_checked(ws, date)[0]


def chip_block(card: dict) -> str:
    """The cadence a chip card is judged by: the strictest its fields imply."""
    meta = _dict(card.get("meta"))
    lineages = [_dict(meta.get("lineage")), _dict(card.get("lineage"))]
    names = [str(lin.get("block")).strip().lower() for lin in lineages if lin.get("block")]
    names.append(_SOURCE_TYPE_BLOCK.get(str(card.get("source_type", "")).strip().lower()))
    sources = " ".join(str(x) for x in [card.get("source_name")]
                       + [lin.get("dataset") for lin in lineages] if x).lower()
    names += [block for fragment, block in _DATASET_BLOCK if fragment in sources]
    judged = {_JUDGED_AS.get(n, n) for n in names if n}
    return next((b for b in _STRICTEST_FIRST if b in judged), "unknown")


def market_profile(ws: Path) -> str:
    """The workspace's market (US|HK|CN|...), whose calendar judges chip freshness."""
    for path, keys in ((ws / "profile" / "company_profile.json", ("market_profile",)),
                       (ws / "resolved_config.json", ("company", "market_profile"))):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            for k in keys:
                value = value.get(k)
        except (OSError, ValueError, AttributeError):
            continue
        if value:
            return str(value).upper()
    return "CN"


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
            elif not isinstance(a.get("overlay", "none"), str) or a.get("overlay", "none") not in OVERLAYS:
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
    if not isinstance(dec.get("supporting_evidence_ids", []), list):
        errs.append(_err("schema", "supporting_evidence_ids must be a list"))
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
    for r in _list(dec.get("top_reasons")):
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
    reasons = audit_reasons(dec) if audit else [r for r in _list(dec.get("top_reasons"))
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

    cards, ambiguous = load_cards_checked(ws, date)
    ticker = ws.name
    market = market_profile(ws)

    def ids(r: dict) -> list[str]:
        return [str(i) for i in _list(r.get("evidence_ids"))]

    cited = [i for r in reasons for i in ids(r)]
    cited += [str(i) for i in _list(dec.get("supporting_evidence_ids"))]
    classes: dict[str, dict] = {}
    for cid in dict.fromkeys(cited):
        card = cards.get(cid)
        if card is None:
            errors.append(_err("unresolved_id", f"{cid} is not an evidence card of this run",
                               evidence_id=cid))
            continue
        if cid in ambiguous:
            errors.append(_err("ambiguous_id", f"{cid} is claimed by more than one card file",
                               evidence_id=cid))
            continue
        cls = evidence_meta.classify(card, ticker)
        classes[cid] = cls
        if evidence_meta.published_after(card, issued_at):
            errors.append(_err("future_card", f"{cid} published {card.get('published_at')}, "
                               f"after issuance {issued_at.isoformat()}", evidence_id=cid))
        if evidence_meta.parse_when(card.get("published_at"))[0] is None:
            warnings.append(_err("undated_card", f"{cid} has no parseable published_at, so "
                                 "look-ahead cannot be ruled out", evidence_id=cid))
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
                verdict = freshness.assess(chip_block(card), obs_date, date, issued_at,
                                           market=market)
                horizon = freshness.calendar_end(market)
                if horizon and date > horizon:
                    warnings.append(_err("calendar_horizon", f"{cid}: sessions after {horizon} are "
                                         "weekdays (holidays unknown); upgrade exchange_calendars",
                                         evidence_id=cid))
                if not verdict["fresh"]:
                    errors.append(_err("stale_chip", f"chip card {cid}: {verdict['reason']}",
                                       evidence_id=cid))
        elif cls["chip_class"] == "suspected_chip" and cls["as_of"] is None:
            warnings.append(_err("undated_suspected_chip",
                                 f"{cid} looks like chip/flow data but has no as_of", evidence_id=cid))

    for i, r in enumerate(reasons):
        d = r.get("direction")
        for cid in ids(r):
            sent = str(_dict(cards.get(cid)).get("sentiment", "")).lower()
            if (d == "up" and sent == "negative") or (d == "down" and sent == "positive"):
                warnings.append(_err("sentiment_conflict",
                                     f"top_reasons[{i}] points {d} but {cid} is {sent}",
                                     evidence_id=cid))

    # K1 — once per direction implied by any BUY/SELL among headline and actions.
    # The direction tags are the decision author's own, so a tag alone cannot
    # take chip evidence out of K1: a "neutral" reason citing chips still leans
    # on them, so does any reason citing a chip card whose own sentiment points
    # the label's way (the generator sets it), and a BUY/SELL must have a reason
    # its way.
    for label in sorted(_labels(dec) - {"HOLD"}):
        d = LABEL_DIRECTION[label]
        same = [r for r in reasons if r.get("direction") == d]
        if not same:
            errors.append(_err("no_supporting_reason", f"{label} has no '{d}' reason"))
            continue
        chip_ids = [i for r in reasons for i in ids(r) if i in classes
                    and classes[i]["chip_class"] in ("chip", "suspected_chip")
                    and (r.get("direction") in (d, "neutral")
                         or str(cards[i].get("sentiment", "")).lower() == DIRECTION_SENTIMENT[d])]
        if not chip_ids:
            continue
        # Corroboration needs a publication date: an undated or month/year card
        # could post-date the decision.
        independent = [i for r in same for i in ids(r) if i in classes and classes[i]["independent"]
                       and evidence_meta.parse_when(cards[i].get("published_at"))[1]
                       in ("date", "datetime")
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


def _not_evaluable(issued_at: datetime, message: str) -> dict:
    return {"gate": "pre_release", "version": 1, "mode": "release", "status": "blocked",
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "issued_at": issued_at.isoformat(timespec="seconds"), "derived_fields": None,
            "errors": [_err("schema", message)], "warnings": [], "counts": {}}


def evaluate_safely(ws: Path, date: str, dec, issued_at: datetime) -> dict:
    """evaluate() for release: LLM-written input it cannot evaluate blocks, never raises."""
    if not isinstance(dec, dict):
        return _not_evaluable(issued_at, "final_decision.json is not a JSON object")
    try:
        return evaluate(ws, date, dec, issued_at)
    except Exception as exc:  # noqa: BLE001 - malformed input must block, not crash the gate
        return _not_evaluable(issued_at, f"decision not evaluable: {type(exc).__name__}: {exc}")


def run(ws: Path, date: str, now: datetime | None = None) -> dict:
    """Release mode: derive headline fields into the decision, check, record."""
    path = contracts.decision_path(ws, date)
    out = contracts.pre_release_gate_path(ws, date)
    out.unlink(missing_ok=True)   # no verdict from an earlier attempt survives this run
    issued_at = now or datetime.now(timezone.utc)
    raw = None
    try:
        raw = path.read_bytes()
        dec = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        result = _not_evaluable(issued_at, f"final_decision.json unreadable: {exc}")
    else:
        result = evaluate_safely(ws, date, dec, issued_at)
        derived = result["derived_fields"]
        if derived:
            dec["decision"] = derived["decision"]
            dec["risk_overlay"] = derived["risk_overlay"]
            dec["derived_fields"] = {k: derived[k] for k in ("position_state", "track")}
            text = json.dumps(dec, indent=2, ensure_ascii=False)
            contracts.write_atomic(path, text)
            raw = text.encode("utf-8")
    # the hash of the bytes this run judged, not of a re-read the file could change under
    result["decision_sha256"] = hashlib.sha256(raw).hexdigest() if raw is not None else None
    contracts.write_atomic(out, json.dumps(result, indent=2, ensure_ascii=False))
    return result


def _ledger_outcome(ws: Path, date: str) -> dict | None:
    """The ledger's release outcome for this run date; raises if the ledger is unreadable."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from ledger import seal as ledger_seal
    return ledger_seal.outcome(ws, date)


def release_check(ws: Path, date: str, at: datetime | None = None,
                  ledger: bool = True) -> dict:
    """Re-derive the verdict for the decision file as it is now.

    The verdict file only says which decision bytes the gate saw; whether they
    pass is recomputed here, at ``at``. By default that is a clock nothing in the
    workspace can set: the issuance time the ledger sealed for this decision, or
    else now — never the verdict file's own `issued_at`, which would let a
    backdated file make old chip data fresh. Once the ledger holds this run
    date's outcome, only the sealed decision may be published; a ledger that
    exists but cannot be read blocks, since it may hold that outcome. Returns
    {status, errors, decision, sha256, gate}; status is 'passed' | 'blocked' |
    'missing' | 'stale' (edited after the gate) | 'mismatch' (the recorded
    verdict says passed, but re-evaluation does not, or the ledger sealed
    something else or cannot be read).
    """
    out = {"status": "missing", "errors": [], "decision": None, "sha256": None, "gate": None,
           "sealed": False}
    try:
        gate = json.loads(contracts.pre_release_gate_path(ws, date).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    out["gate"] = gate = _dict(gate)
    if gate.get("status") != "passed":
        out["status"] = "blocked"
        out["errors"] = [e for e in _list(gate.get("errors")) if isinstance(e, dict)]
        return out
    try:
        raw = contracts.decision_path(ws, date).read_bytes()
    except OSError:
        out["status"] = "stale"
        return out
    out["sha256"] = hashlib.sha256(raw).hexdigest()
    if out["sha256"] != gate.get("decision_sha256"):
        out["status"] = "stale"
        return out
    sealed = None
    if ledger:
        try:
            sealed = _ledger_outcome(ws, date)
        except Exception as exc:  # noqa: BLE001 - unknown outcome: never publish past it
            out["status"] = "mismatch"
            out["errors"] = [_err("ledger", f"the ledger cannot be read, so this run date's "
                                  f"outcome is unknown: {exc}")]
            return out
        if sealed and (sealed["type"] != "decision" or sealed.get("decision_sha256") != out["sha256"]):
            out["status"] = "mismatch"
            out["errors"] = [_err("ledger", f"the ledger already holds record {sealed['seq']} "
                                  f"({sealed['type']}) for {ws.name} {date}; only that outcome "
                                  "may be published")]
            return out
    if at is None:
        when, precision = evidence_meta.parse_when((sealed or {}).get("issued_at"))
        at = when if when is not None and precision == "datetime" else datetime.now(timezone.utc)
    try:
        dec = json.loads(raw.decode("utf-8"))
    except ValueError:
        dec = None
    result = evaluate_safely(ws, date, dec, at)
    if result["status"] != "passed":
        out["status"] = "mismatch"
        out["errors"] = result["errors"]
        return out
    out["decision"] = dec
    out["sealed"] = bool(sealed and sealed["type"] == "decision")
    out["status"] = "passed"
    return out


def gate_status(ws: Path, date: str) -> str:
    """'passed' | 'blocked' | 'missing' | 'stale' | 'mismatch' — see release_check."""
    return release_check(ws, date)["status"]


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

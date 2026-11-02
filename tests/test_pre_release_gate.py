"""Tests for eval/pre_release_gate.py — the blocking check before export."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pre_release_gate", ROOT / "eval" / "pre_release_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

DATE = "2026-10-22"
ISSUED = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)   # 17:00 Beijing, after the close

CARDS = {
    "ev_2026-10-22_company_001": {"desk": "company", "source_type": "news", "ticker": "300308",
                                  "title": "Q3 800G shipments doubled", "published_at": "2026-10-21",
                                  "sentiment": "positive"},
    "ev_2026-10-22_company_002": {"desk": "company", "source_type": "news", "ticker": "300308",
                                  "title": "Gross margin compressed to 38%", "published_at": "2026-10-21",
                                  "sentiment": "negative"},
    "ev_2026-10-22_chip_001": {"desk": "chips", "source_type": "chip_structure", "sentiment": "negative",
                               "title": "Main-force outflow 5 days",
                               "meta": {"as_of": "2026-10-22", "family": "chip",
                                        "generator": "chip_evidence_cards",
                                        "lineage": {"dataset": "akshare:x", "block": "main_force"}}},
    "ev_2026-10-22_chip_002": {"desk": "chips", "source_type": "chip_structure", "sentiment": "negative",
                               "title": "Northbound trimmed",
                               "meta": {"as_of": "2024-08-16", "family": "chip",
                                        "lineage": {"dataset": "akshare:x", "block": "northbound"}}},
    "ev_2026-10-22_company_003": {"desk": "company", "source_type": "data", "ticker": "300308",
                                  "title": "融资余额连降至256.58亿元", "published_at": "2026-10-22",
                                  "topic_tags": ["margin", "chips"], "sentiment": "negative"},
    "ev_2026-10-22_web_001": {"desk": "web", "source_type": "news", "ticker": "300308",
                              "title": "中际旭创龙虎榜：机构净卖出", "published_at": "2026-10-22",
                              "sentiment": "negative"},
    "ev_2026-10-22_web_002": {"desk": "web", "source_type": "news", "ticker": "300308",
                              "title": "Follow-up coverage", "published_at": "2026-10-25"},
    "ev_2026-10-22_sector_001": {"desk": "sector", "source_type": "news", "title": "Optics peers weak",
                                 "published_at": "2026-10-21", "sentiment": "negative"},
}


def _ws(tmp_path, position="none"):
    ws = tmp_path / "300308"
    cards = ws / "normalized" / DATE / "evidence_cards"
    cards.mkdir(parents=True)
    for cid, body in CARDS.items():
        (cards / f"{cid}.json").write_text(json.dumps({"id": cid, **body}, ensure_ascii=False))
    (ws / "profile").mkdir()
    (ws / "profile" / "investor_profile.json").write_text(json.dumps({"position_state": position}))
    (ws / "decision" / DATE).mkdir(parents=True)
    return ws


def _decision(label="HOLD", reasons=None, flat=None, long=None, **over):
    dec = {
        "schema_version": 2,
        "decision": label,
        "risk_overlay": "none",
        "actions": {"if_flat": {"label": flat or label, "overlay": "none"},
                    "if_long": {"label": long or label, "overlay": "trim"}},
        "forecast": {"p_outperform": 0.45, "benchmark": "CSI300", "horizon_sessions": 20},
        "confidence": 0.5,
        "top_reasons": reasons if reasons is not None else [
            {"text": "Shipments doubled", "evidence_ids": ["ev_2026-10-22_company_001"], "direction": "up"},
            {"text": "Margin pressure", "evidence_ids": ["ev_2026-10-22_company_002"], "direction": "down"},
        ],
        "supporting_evidence_ids": [],
    }
    dec.update(over)
    return dec


def _run(ws, dec):
    (ws / "decision" / DATE / "final_decision.json").write_text(json.dumps(dec, ensure_ascii=False))
    return gate.run(ws, DATE, now=ISSUED)


def _checks(result):
    return sorted(e["check"] for e in result["errors"])


def test_clean_v2_decision_passes_and_records_sha(tmp_path):
    ws = _ws(tmp_path)
    r = _run(ws, _decision())
    assert r["status"] == "passed", r["errors"]
    saved = json.loads((ws / "decision" / DATE / "pre_release_gate.json").read_text())
    assert saved["decision_sha256"] == gate.sha256_file(ws / "decision" / DATE / "final_decision.json")
    assert gate.gate_status(ws, DATE) == "passed"


def test_v1_decision_is_blocked_on_schema(tmp_path):
    ws = _ws(tmp_path)
    v1 = {"decision": "HOLD", "risk_overlay": "none", "confidence": 0.5,
          "top_reasons": ["reason (ev_2026-10-22_company_001)"]}
    r = _run(ws, v1)
    assert r["status"] == "blocked" and set(_checks(r)) == {"schema"}


def test_headline_is_derived_from_the_declared_position(tmp_path):
    ws = _ws(tmp_path, position="holding")
    r = _run(ws, _decision(label="HOLD", flat="HOLD", long="HOLD"))
    dec = json.loads((ws / "decision" / DATE / "final_decision.json").read_text())
    assert r["derived_fields"]["track"] == "if_long"
    assert dec["risk_overlay"] == "trim"           # the long track's overlay
    assert dec["derived_fields"]["position_state"] == "holding"


def test_headline_mismatch_is_corrected_with_a_warning(tmp_path):
    ws = _ws(tmp_path)
    r = _run(ws, _decision(label="BUY", flat="HOLD", long="HOLD"))
    dec = json.loads((ws / "decision" / DATE / "final_decision.json").read_text())
    assert dec["decision"] == "HOLD"
    assert any(w["check"] == "headline_mismatch" for w in r["warnings"])


def test_unresolved_id_blocks_but_prose_ids_are_ignored(tmp_path):
    ws = _ws(tmp_path)
    reasons = [{"text": "see ev_2026-10-22_chip_*", "evidence_ids": ["ev_2026-10-22_company_999"],
                "direction": "neutral"}]
    r = _run(ws, _decision(reasons=reasons))
    assert _checks(r) == ["unresolved_id"]


def test_stale_and_undated_chip_cards_block_any_label(tmp_path):
    ws = _ws(tmp_path)
    reasons = [{"text": "Northbound trimmed", "evidence_ids": ["ev_2026-10-22_chip_002"], "direction": "down"},
               {"text": "Margin falling", "evidence_ids": ["ev_2026-10-22_company_003"], "direction": "down"}]
    r = _run(ws, _decision(label="HOLD", reasons=reasons))
    assert _checks(r) == ["stale_chip", "undated_chip"]


def test_future_card_blocks(tmp_path):
    ws = _ws(tmp_path)
    reasons = [{"text": "Later coverage", "evidence_ids": ["ev_2026-10-22_web_002"], "direction": "neutral"}]
    assert _checks(_run(ws, _decision(reasons=reasons))) == ["future_card"]


def test_chip_only_sell_is_blocked_by_k1(tmp_path):
    ws = _ws(tmp_path)
    reasons = [{"text": "Main-force outflow", "evidence_ids": ["ev_2026-10-22_chip_001"], "direction": "down"},
               {"text": "Peers weak", "evidence_ids": ["ev_2026-10-22_sector_001"], "direction": "down"}]
    r = _run(ws, _decision(label="SELL", reasons=reasons))
    assert _checks(r) == ["k1_uncorroborated"]   # sector context cannot corroborate


def test_chip_plus_independent_evidence_passes_k1(tmp_path):
    ws = _ws(tmp_path)
    reasons = [{"text": "Main-force outflow", "evidence_ids": ["ev_2026-10-22_chip_001"], "direction": "down"},
               {"text": "Margins compressing", "evidence_ids": ["ev_2026-10-22_company_002"], "direction": "down"}]
    r = _run(ws, _decision(label="SELL", reasons=reasons))
    assert r["status"] == "passed", r["errors"]


def test_independent_evidence_must_point_the_same_way(tmp_path):
    ws = _ws(tmp_path)
    reasons = [{"text": "Main-force outflow", "evidence_ids": ["ev_2026-10-22_chip_001"], "direction": "down"},
               {"text": "Shipments doubled", "evidence_ids": ["ev_2026-10-22_company_001"], "direction": "up"}]
    assert _checks(_run(ws, _decision(label="SELL", reasons=reasons))) == ["k1_uncorroborated"]


def test_suspected_chip_triggers_k1_and_is_not_independent(tmp_path):
    ws = _ws(tmp_path)
    reasons = [{"text": "Institutions sold on the LHB", "evidence_ids": ["ev_2026-10-22_web_001"],
                "direction": "down"}]
    r = _run(ws, _decision(label="SELL", reasons=reasons))
    assert _checks(r) == ["k1_uncorroborated"]


def test_hold_skips_k1(tmp_path):
    ws = _ws(tmp_path)
    reasons = [{"text": "Main-force outflow", "evidence_ids": ["ev_2026-10-22_chip_001"], "direction": "down"}]
    assert _run(ws, _decision(label="HOLD", reasons=reasons))["status"] == "passed"


def test_k1_applies_to_a_non_headline_action(tmp_path):
    # Headline HOLD for a flat investor, but SELL for a holder: still a SELL call.
    ws = _ws(tmp_path)
    reasons = [{"text": "Main-force outflow", "evidence_ids": ["ev_2026-10-22_chip_001"], "direction": "down"}]
    r = _run(ws, _decision(label="HOLD", flat="HOLD", long="SELL", reasons=reasons))
    assert _checks(r) == ["k1_uncorroborated"]


def test_editing_after_the_gate_is_detected(tmp_path):
    ws = _ws(tmp_path)
    _run(ws, _decision())
    path = ws / "decision" / DATE / "final_decision.json"
    dec = json.loads(path.read_text())
    dec["confidence"] = 0.9
    path.write_text(json.dumps(dec))
    assert gate.gate_status(ws, DATE) == "stale"


def test_missing_gate_file_is_reported(tmp_path):
    assert gate.gate_status(_ws(tmp_path), DATE) == "missing"


def test_sentiment_conflict_is_only_a_warning(tmp_path):
    ws = _ws(tmp_path)
    reasons = [{"text": "Bullish read", "evidence_ids": ["ev_2026-10-22_company_002"], "direction": "up"},
               {"text": "Shipments", "evidence_ids": ["ev_2026-10-22_company_001"], "direction": "up"}]
    r = _run(ws, _decision(label="BUY", reasons=reasons))
    assert r["status"] == "passed"
    assert any(w["check"] == "sentiment_conflict" for w in r["warnings"])


def test_audit_mode_reads_v1_without_writing(tmp_path):
    ws = _ws(tmp_path)
    path = ws / "decision" / DATE / "final_decision.json"
    v1 = {"decision": "SELL", "confidence": 0.6,
          "top_reasons": ["Outflow (ev_2026-10-22_chip_001)", "Peers (ev_2026-10-22_sector_001)"]}
    path.write_text(json.dumps(v1))
    r = gate.evaluate(ws, DATE, v1, ISSUED, audit=True)
    assert r["mode"] == "audit" and _checks(r) == ["k1_uncorroborated"]
    assert any(w["check"] == "schema_v1" for w in r["warnings"])
    assert json.loads(path.read_text()) == v1
    assert not (ws / "decision" / DATE / "pre_release_gate.json").exists()


# ---------- the gate never crashes, and its verdict is re-checked ----------

def _add_card(ws, name, body):
    (ws / "normalized" / DATE / "evidence_cards" / f"{name}.json").write_text(
        json.dumps(body, ensure_ascii=False))


@pytest.mark.parametrize("over", [{"top_reasons": 1}, {"supporting_evidence_ids": 1},
                                  {"actions": {"if_flat": {"label": "HOLD", "overlay": ["trim"]},
                                               "if_long": {"label": "HOLD"}}}])
def test_malformed_llm_fields_block_instead_of_crashing(tmp_path, over):
    r = _run(_ws(tmp_path), _decision(**over))
    assert r["status"] == "blocked" and "schema" in _checks(r)


def test_a_crash_inside_evaluation_replaces_an_earlier_passed_verdict(tmp_path):
    ws = _ws(tmp_path)
    _add_card(ws, "ev_2026-10-22_company_009", {"id": "ev_2026-10-22_company_009", "desk": "company",
                                                "topic_tags": 5, "meta": "x"})
    path = ws / "decision" / DATE / "final_decision.json"
    dec = _decision(reasons=[{"text": "x", "evidence_ids": ["ev_2026-10-22_company_009"],
                              "direction": "up"}])
    path.write_text(json.dumps(dec))
    (ws / "decision" / DATE / "pre_release_gate.json").write_text(json.dumps(   # planted
        {"status": "passed", "decision_sha256": gate.sha256_file(path),
         "issued_at": ISSUED.isoformat()}))
    r = gate.run(ws, DATE, now=ISSUED)
    assert r["status"] == "blocked" and "not evaluable" in r["errors"][0]["message"]
    saved = json.loads((ws / "decision" / DATE / "pre_release_gate.json").read_text())
    assert saved["status"] == "blocked" and gate.gate_status(ws, DATE) == "blocked"


def test_a_planted_passed_verdict_fails_the_recheck(tmp_path):
    ws = _ws(tmp_path)
    path = ws / "decision" / DATE / "final_decision.json"
    chip_only_buy = _decision(label="BUY", reasons=[
        {"text": "Outflow reversed", "evidence_ids": ["ev_2026-10-22_chip_001"], "direction": "up"}])
    path.write_text(json.dumps(chip_only_buy))
    (ws / "decision" / DATE / "pre_release_gate.json").write_text(json.dumps(
        {"status": "passed", "decision_sha256": gate.sha256_file(path),
         "issued_at": ISSUED.isoformat()}))
    chk = gate.release_check(ws, DATE)
    assert chk["status"] == "mismatch" and [e["check"] for e in chk["errors"]] == ["k1_uncorroborated"]


# ---------- K1 cannot be dodged by the direction tag ----------

CHIP_FRESH = "ev_2026-10-22_chip_001"


def test_a_neutral_reason_citing_chips_still_needs_corroboration(tmp_path):
    reasons = [{"text": "Peers", "evidence_ids": ["ev_2026-10-22_sector_001"], "direction": "up"},
               {"text": "Flows", "evidence_ids": [CHIP_FRESH], "direction": "neutral"}]
    assert _checks(_run(_ws(tmp_path), _decision(label="BUY", reasons=reasons))) == ["k1_uncorroborated"]


def test_a_buy_needs_a_reason_pointing_up(tmp_path):
    for direction in ("neutral", "down"):
        reasons = [{"text": "Flows", "evidence_ids": [CHIP_FRESH], "direction": direction}]
        r = _run(_ws(tmp_path / direction), _decision(label="BUY", reasons=reasons))
        assert _checks(r) == ["no_supporting_reason"]


def test_neutral_chips_with_independent_corroboration_pass(tmp_path):
    reasons = [{"text": "Shipments", "evidence_ids": ["ev_2026-10-22_company_001"], "direction": "up"},
               {"text": "Flows", "evidence_ids": [CHIP_FRESH], "direction": "neutral"}]
    assert _run(_ws(tmp_path), _decision(label="BUY", reasons=reasons))["status"] == "passed"


def test_an_undated_card_cannot_corroborate(tmp_path):
    ws = _ws(tmp_path)
    _add_card(ws, "ev_2026-10-22_company_009", {"id": "ev_2026-10-22_company_009", "desk": "company",
                                                "source_type": "news", "title": "Order win"})
    reasons = [{"text": "Flows + order", "evidence_ids": [CHIP_FRESH, "ev_2026-10-22_company_009"],
                "direction": "up"}]
    r = _run(ws, _decision(label="BUY", reasons=reasons))
    assert _checks(r) == ["k1_uncorroborated"]
    assert any(w["check"] == "undated_card" for w in r["warnings"])


# ---------- one card per id ----------

def test_a_second_file_claiming_an_id_is_ambiguous(tmp_path):
    """A fresh copy filed under a stale chip card's id must not be judged instead of it."""
    ws = _ws(tmp_path)
    stale_id = "ev_2026-10-22_chip_002"
    _add_card(ws, "ev_2026-10-22_aaa", {**CARDS[stale_id], "id": stale_id,
                                         "meta": {"as_of": "2026-10-22", "family": "chip",
                                                  "lineage": {"block": "northbound"}}})
    reasons = [{"text": "Flows", "evidence_ids": [stale_id], "direction": "neutral"}]
    assert _checks(_run(ws, _decision(reasons=reasons))) == ["ambiguous_id"]


# ---------- the freshness window comes from the strictest block a card implies ----------

STALE_AS_OF = "2026-10-08"   # 10 sessions before the run


@pytest.mark.parametrize("card", [
    {"desk": "company", "source_type": "northbound_holding", "meta": {"as_of": STALE_AS_OF}},
    {"desk": "chips", "meta": {"as_of": STALE_AS_OF, "lineage": {"block": "Northbound"}}},
    {"desk": "chips", "meta": {"as_of": STALE_AS_OF, "lineage": {"dataset": "akshare:stock_hsgt_x"}},
     "lineage": {"block": "lhb"}},
    {"desk": "chips", "meta": {"as_of": "2019-01-02", "lineage": {"block": "restricted_release"}}},
    {"desk": "chips", "meta": {"as_of": "2027-06-01", "lineage": {"block": "restricted_release"}}},
    {"desk": "chips", "meta": {"as_of": " 2020-01-02"}},
])
def test_self_described_chip_cards_cannot_buy_a_longer_window(tmp_path, card):
    ws = _ws(tmp_path)
    _add_card(ws, "ev_2026-10-22_chip_009", {"id": "ev_2026-10-22_chip_009", "title": "Flows",
                                             "sentiment": "positive", **card})
    reasons = [{"text": "Flows", "evidence_ids": ["ev_2026-10-22_chip_009"], "direction": "neutral"}]
    assert _checks(_run(ws, _decision(reasons=reasons))) == ["stale_chip"]


def test_generator_holder_count_card_keeps_its_quarterly_window(tmp_path):
    ws = _ws(tmp_path)
    _add_card(ws, "ev_2026-10-22_chip_009", {
        "id": "ev_2026-10-22_chip_009", "desk": "chips", "source_type": "chip_structure",
        "title": "Holder count fell", "meta": {
            "as_of": "2026-09-30", "generator": "chip_evidence_cards", "family": "chip",
            "lineage": {"dataset": "akshare:stock_zh_a_gdhs_detail_em", "block": "holder_count"}}})
    reasons = [{"text": "Holders", "evidence_ids": ["ev_2026-10-22_chip_009"], "direction": "neutral"}]
    assert _run(ws, _decision(reasons=reasons))["status"] == "passed"


# ---------- a US name's chip card is judged on the NYSE calendar ----------

def _us_price_card_run(tmp_path, run_date, issued, as_of):
    ws = tmp_path / "MU"
    cards = ws / "normalized" / run_date / "evidence_cards"
    cards.mkdir(parents=True)
    cid = f"ev_{run_date}_chip_001"
    (cards / f"{cid}.json").write_text(json.dumps({
        "id": cid, "desk": "chips", "source_type": "chip_structure", "title": "Breakout on volume",
        "published_at": as_of, "meta": {"as_of": as_of, "family": "chip",
                                         "generator": "chip_evidence_cards",
                                         "lineage": {"dataset": "nasdaq", "block": "price_structure"}}}))
    (ws / "profile").mkdir()
    (ws / "profile" / "company_profile.json").write_text(json.dumps({"market_profile": "US"}))
    dec = _decision(reasons=[{"text": "Tape", "evidence_ids": [cid], "direction": "neutral"}])
    return gate.evaluate(ws, run_date, dec, issued)


def test_us_card_is_stale_across_a_shanghai_holiday(tmp_path):
    # NYSE traded through Golden Week: six sessions after 09-28, not two
    r = _us_price_card_run(tmp_path, "2026-10-07", datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc),
                           "2026-09-28")
    assert _checks(r) == ["stale_chip"]


def test_us_card_is_fresh_across_thanksgiving(tmp_path):
    # one NYSE session after 11-24 by 09:30 ET on 11-27; Shanghai would count three
    r = _us_price_card_run(tmp_path, "2026-11-27", datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc),
                           "2026-11-24")
    assert r["status"] == "passed", r["errors"]


def test_an_opposite_tag_on_a_bullish_chip_card_still_counts(tmp_path):
    """A token 'up' reason plus the bullish chip card tagged 'down' does not dodge K1."""
    ws = _ws(tmp_path)
    _add_card(ws, "ev_2026-10-22_chip_009", {
        "id": "ev_2026-10-22_chip_009", "desk": "chips", "source_type": "chip_structure",
        "title": "Main-force inflow 5 days", "sentiment": "positive",
        "meta": {"as_of": "2026-10-22", "family": "chip", "generator": "chip_evidence_cards",
                 "lineage": {"block": "main_force"}}})
    reasons = [{"text": "Peers firm", "evidence_ids": ["ev_2026-10-22_sector_001"], "direction": "up"},
               {"text": "Inflow", "evidence_ids": ["ev_2026-10-22_chip_009"], "direction": "down"}]
    assert _checks(_run(ws, _decision(label="BUY", reasons=reasons))) == ["k1_uncorroborated"]


def test_a_bearish_chip_card_cited_against_a_buy_needs_no_corroboration(tmp_path):
    reasons = [{"text": "Peers firm", "evidence_ids": ["ev_2026-10-22_sector_001"], "direction": "up"},
               {"text": "Outflow", "evidence_ids": [CHIP_FRESH], "direction": "down"}]
    assert _run(_ws(tmp_path), _decision(label="BUY", reasons=reasons))["status"] == "passed"


# ---------- Codex review 2026-11-02: the re-check clock and the ledger ----------

def test_a_backdated_verdict_does_not_make_stale_chips_fresh(tmp_path):
    """R1: release_check's default clock is never the verdict file's own issued_at."""
    ws = _ws(tmp_path)
    reasons = [{"text": "Outflow", "evidence_ids": ["ev_2026-10-22_chip_002"], "direction": "neutral"}]
    path = ws / "decision" / DATE / "final_decision.json"
    path.write_text(json.dumps(_decision(reasons=reasons)))      # northbound, as_of 2024-08-16
    (ws / "decision" / DATE / "pre_release_gate.json").write_text(json.dumps(
        {"status": "passed", "decision_sha256": gate.sha256_file(path),
         "issued_at": "2024-08-16T09:00:00+00:00"}))
    chk = gate.release_check(ws, DATE)
    assert chk["status"] == "mismatch" and [e["check"] for e in chk["errors"]] == ["stale_chip"]


def _seal_root(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT))
    from ledger import seal as ledger_seal, store
    root = tmp_path / "data"
    ledger_seal.init(root, stamp_binary=None)
    return ledger_seal, store, root


def test_the_recheck_clock_is_the_sealed_time_or_now(tmp_path, monkeypatch):
    """A sealed decision is judged at the ledger's issuance (a later clock would excuse
    cards published since); an unsealed one at the current clock."""
    ledger_seal, store, root = _seal_root(tmp_path)
    monkeypatch.setenv("MM_DATA_ROOT", str(root))
    ws = _ws(tmp_path)
    assert _run(ws, _decision())["status"] == "passed"
    seen = []
    real = gate.evaluate_safely
    monkeypatch.setattr(gate, "evaluate_safely", lambda w, d, dec, at: seen.append(at) or real(w, d, dec, at))
    assert gate.release_check(ws, DATE)["status"] == "passed"
    ledger_seal.seal(ws, DATE, root, now=ISSUED.replace(minute=7), stamp_binary=None)
    assert gate.release_check(ws, DATE)["status"] == "passed"
    assert seen[0] > datetime(2026, 11, 1, tzinfo=timezone.utc)            # unsealed: now
    assert seen[-1] == ISSUED.replace(minute=7)                            # sealed: its time


def test_an_unreadable_ledger_is_not_an_empty_one(tmp_path, monkeypatch):
    """R3: a torn tail after an existing block must not let a passing decision through."""
    ledger_seal, store, root = _seal_root(tmp_path)
    monkeypatch.setenv("MM_DATA_ROOT", str(root))
    ws = _ws(tmp_path)
    store.append(root, "gate_block", {"ticker": ws.name, "run_date": DATE, "errors": []})
    _run(ws, _decision())
    assert gate.release_check(ws, DATE)["status"] == "mismatch"            # the block holds
    with open(store.ledger_path(root), "ab") as fh:
        fh.write(b"{")
    chk = gate.release_check(ws, DATE)
    assert chk["status"] == "mismatch" and "cannot be read" in chk["errors"][0]["message"]



def test_a_ledger_with_a_broken_hash_is_not_trusted(tmp_path, monkeypatch):
    """Codex round 2: a block edited into another ticker (valid JSON, stale hash) must not
    read as "no outcome"."""
    ledger_seal, store, root = _seal_root(tmp_path)
    monkeypatch.setenv("MM_DATA_ROOT", str(root))
    ws = _ws(tmp_path)
    store.append(root, "gate_block", {"ticker": ws.name, "run_date": DATE, "errors": []})
    rows = store.read(root)
    rows[-1]["body"]["ticker"] = "OTHER"
    store.ledger_path(root).write_bytes(b"".join(store.canonical(r) + b"\n" for r in rows))
    _run(ws, _decision())
    chk = gate.release_check(ws, DATE)
    assert chk["status"] == "mismatch" and "does not verify" in chk["errors"][0]["message"]


def test_past_the_calendar_horizon_a_passing_card_still_warns(tmp_path):
    ws = tmp_path / "300308"
    date = "2027-01-05"
    cards = ws / "normalized" / date / "evidence_cards"
    cards.mkdir(parents=True)
    cid = f"ev_{date}_chip_001"
    (cards / f"{cid}.json").write_text(json.dumps({
        "id": cid, "desk": "chips", "source_type": "chip_structure", "title": "Inflow",
        "published_at": date, "meta": {"as_of": date, "family": "chip",
                                       "lineage": {"block": "main_force"}}}))
    dec = _decision(reasons=[{"text": "Flows", "evidence_ids": [cid], "direction": "neutral"}])
    r = gate.evaluate(ws, date, dec, datetime(2027, 1, 5, 9, 0, tzinfo=timezone.utc))
    assert r["status"] == "passed"
    assert any(w["check"] == "calendar_horizon" for w in r["warnings"])

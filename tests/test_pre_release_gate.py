"""Tests for eval/pre_release_gate.py — the blocking check before export."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

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

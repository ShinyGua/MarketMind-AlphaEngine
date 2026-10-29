"""Tests for the decision ledger: store, seal, stamps."""
import hashlib
import json
import multiprocessing
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ledger import seal, stamp, store  # noqa: E402

DATE = "2026-10-22"


@pytest.fixture
def root(tmp_path, monkeypatch):
    # No test may reach the public OpenTimestamps calendars.
    monkeypatch.setattr(stamp, "ots_binary", lambda: None)
    return tmp_path / "MarketMindData"


def _init(root):
    rec = store.append(root, "protocol", {"protocol": {"version": 1}})
    return rec


# ---------- store ----------

def test_canonical_bytes_are_stable():
    a = store.canonical({"b": 1, "a": "中", "c": [1, {"y": 2, "x": 1}]})
    b = store.canonical({"c": [1, {"x": 1, "y": 2}], "a": "中", "b": 1})
    assert a == b == '{"a":"中","b":1,"c":[1,{"x":1,"y":2}]}'.encode()


def test_protocol_must_come_first(root):
    with pytest.raises(store.LedgerError, match="protocol"):
        store.append(root, "decision", {"x": 1})


def test_chain_links_and_verifies(root):
    _init(root)
    r2 = store.append(root, "decision", {"id": "a"})
    r3 = store.append(root, "outcome", {"id": "a", "excess": 0.01})
    assert r2["prev_hash"] == store.read(root)[0]["hash"] and r3["prev_hash"] == r2["hash"]
    v = store.verify(root)
    assert v["ok"] and v["count"] == 3 and v["head"] == r3["hash"]


def _lines(root):
    return store.ledger_path(root).read_text().splitlines()


def test_edited_record_is_detected(root):
    _init(root)
    store.append(root, "decision", {"label": "HOLD"})
    lines = _lines(root)
    lines[1] = lines[1].replace("HOLD", "BUY")
    store.ledger_path(root).write_text("\n".join(lines) + "\n")
    assert any("hash mismatch" in e for e in store.verify(root)["errors"])


def test_dropped_and_reordered_records_are_detected(root):
    _init(root)
    for i in range(3):
        store.append(root, "decision", {"i": i})
    lines = _lines(root)
    store.ledger_path(root).write_text("\n".join([lines[0], lines[2], lines[1], lines[3]]) + "\n")
    errs = store.verify(root)["errors"]
    assert any("seq" in e for e in errs) and any("prev_hash" in e for e in errs)
    store.ledger_path(root).write_text("\n".join([lines[0], lines[1], lines[3]]) + "\n")
    assert not store.verify(root)["ok"]


def test_truncated_tail_is_detected_and_blocks_appends(root):
    _init(root)
    store.append(root, "decision", {"i": 1})
    raw = store.ledger_path(root).read_bytes()
    store.ledger_path(root).write_bytes(raw[:-20])
    assert any("partial record" in e for e in store.verify(root)["errors"])
    with pytest.raises(store.LedgerError, match="partial"):
        store.append(root, "decision", {"i": 2})


def test_icloud_roots_are_refused():
    with pytest.raises(store.LedgerError, match="iCloud"):
        store.check_root(Path.home() / "Documents" / "MarketMindData")


def _worker(args):
    root, n = args
    for i in range(n):
        store.append(Path(root), "decision", {"pid": multiprocessing.current_process().name, "i": i})


def test_concurrent_appends_keep_one_chain(root):
    _init(root)
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(4) as pool:
        pool.map(_worker, [(str(root), 10)] * 4)
    v = store.verify(root)
    assert v["ok"], v["errors"]
    assert v["count"] == 41
    assert [r["seq"] for r in store.read(root)] == list(range(1, 42))


# ---------- seal ----------

def _workspace(tmp_path, gate_status="passed", edit_after=False):
    ws = tmp_path / "300308"
    d = ws / "decision" / DATE
    (d / "panel" / "round_1").mkdir(parents=True)
    (d / "panel" / "round_2").mkdir(parents=True)
    for role, vote, conv, p in (("company_analyst", "HOLD", 0.6, 0.45),
                                ("chips_analyst", "SELL", 0.8, 0.35),
                                ("risk_analyst", "SELL", 0.4, 0.40)):
        (d / "panel" / "round_2" / f"{role}_ballot.json").write_text(json.dumps(
            {"role": role, "round": 2, "vote": vote, "conviction": conv, "risk_overlay": "none",
             "action_if_flat": "HOLD", "action_if_long": vote, "p_outperform": p}))
    (d / "panel" / "round_1" / "company_analyst_ballot.json").write_text('{"vote": "BUY"}')
    dec = {"schema_version": 2, "decision": "HOLD", "risk_overlay": "none",
           "actions": {"if_flat": {"label": "HOLD", "overlay": "none"},
                       "if_long": {"label": "SELL", "overlay": "none"}},
           "forecast": {"p_outperform": 0.4, "benchmark": "CSI300", "horizon_sessions": 20},
           "confidence": 0.5, "derived_fields": {"position_state": "none", "track": "if_flat"}}
    (d / "final_decision.json").write_text(json.dumps(dec))
    sha = hashlib.sha256((d / "final_decision.json").read_bytes()).hexdigest()
    (d / "pre_release_gate.json").write_text(json.dumps(
        {"status": gate_status, "decision_sha256": sha, "warnings": []}))
    if edit_after:
        (d / "final_decision.json").write_text(json.dumps({**dec, "confidence": 0.9}))
    (ws / "raw" / DATE / "prices").mkdir(parents=True)
    (ws / "raw" / DATE / "prices" / "300308_3mo.csv").write_text("Date,Close\n2026-10-21,646.77\n")
    (ws / "profile").mkdir()
    (ws / "profile" / "company_profile.json").write_text(json.dumps(
        {"yf_ticker": "300308.SZ", "market_profile": "CN", "exchange": "SZSE"}))
    return ws


NOW = datetime(2026, 10, 22, 9, 0, 0, tzinfo=timezone.utc)


def test_seal_refuses_without_protocol(tmp_path, root):
    with pytest.raises(store.LedgerError, match="protocol"):
        seal.seal(_workspace(tmp_path), DATE, root, now=NOW, stamp_binary=None)


@pytest.mark.parametrize("status,edit", [("blocked", False), ("passed", True)])
def test_seal_refuses_blocked_or_edited(tmp_path, root, status, edit):
    seal.init(root)
    with pytest.raises(store.LedgerError, match="gate status"):
        seal.seal(_workspace(tmp_path, status, edit), DATE, root, now=NOW, stamp_binary=None)


def test_seal_records_everything_needed_to_score(tmp_path, root):
    seal.init(root)
    ws = _workspace(tmp_path)
    receipt = seal.seal(ws, DATE, root, now=NOW, stamp_binary=None)
    rec = store.read(root)[receipt["seq"] - 1]
    body = rec["body"]
    assert rec["type"] == "decision" and body["issued_at"] == "2026-10-22T09:00:00+00:00"
    assert body["decision_id"] == "300308_2026-10-22_20261022T090000Z"
    assert body["symbol"] == "300308.SZ" and body["market"] == "CN"
    assert body["actions"]["if_long"]["label"] == "SELL"
    assert body["forecast"]["p_outperform"] == 0.4
    # final round only, both tallies from the same ballots
    assert body["tallies"]["n_ballots"] == 3
    assert body["tallies"]["head_count"] == {"BUY": 0, "HOLD": 1, "SELL": 2}
    assert body["tallies"]["conviction_weighted"]["SELL"] == 1.2
    assert body["tallies"]["p_outperform_equal_weight"] == 0.4
    assert body["tallies"]["p_outperform_conviction_weighted"] == round((0.27 + 0.28 + 0.16) / 1.8, 4)
    man = json.loads((root / "manifests" / f"{body['decision_id']}.json").read_text())
    assert "raw/2026-10-22/prices/300308_3mo.csv" in man["files"]
    assert "decision/2026-10-22/final_decision.json" in man["files"]
    assert "profile/company_profile.json" in man["files"]
    assert (root / "archive" / f"{body['decision_id']}.tar.gz").exists()
    assert json.loads((ws / "decision" / DATE / "ledger_receipt.json").read_text())["seq"] == rec["seq"]
    assert store.verify(root)["ok"]


def test_blocked_release_is_recorded_not_sealed(tmp_path, root):
    seal.init(root)
    ws = _workspace(tmp_path, "blocked")
    rec = seal.record_block(ws, DATE, root)
    assert rec["type"] == "gate_block" and rec["body"]["ticker"] == "300308"


# ---------- stamps ----------

def test_init_seals_the_committed_protocol_once(root):
    first = seal.init(root)
    again = seal.init(root)
    assert first["seq"] == again["seq"] == 1
    body = store.read(root)[0]["body"]
    text = (ROOT / "ledger" / "protocol_v1.json").read_text(encoding="utf-8")
    assert body["protocol_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert body["protocol"]["forecast"]["horizons_sessions"]["primary"] == 20


def test_missing_client_is_not_submitted_never_raises(tmp_path, root):
    store.append(root, "protocol", {"v": 1})
    rec = store.append(root, "decision", {"id": "x"})
    s = stamp.stamp_record(root, rec, binary=None)
    assert s["type"] == "stamp" and s["body"]["status"] == "not_submitted"
    assert (root / "stamps" / f"{rec['seq']:06d}.hash").read_text().strip() == rec["hash"]
    assert stamp.stamp_summary(root) == {"not_submitted": 1}
    assert stamp.upgrade_all(root, binary=None) == []

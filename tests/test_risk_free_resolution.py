"""Tests for run_valuation._resolve_risk_free — live 10Y discovery from the
shared macro layer with config fallback. Synthetic fixtures only.

Contract: the macro layer is ADVISORY — a fallback is recorded in the returned
"note" (→ macro_inputs.risk_free_note), never in the engine's inputs_missing,
so it can never cap the valuation confidence heuristic.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "valuation"))
import run_valuation as rv  # noqa: E402

DATE = "2026-01-15"
CFG = {"use_live_risk_free": True, "risk_free_rate": 0.042}


def _make_ws(tmp_path, regime=None, dgs10_csv=None, sources=None):
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    shared = tmp_path / "workspaces" / "shared" / "market_context" / DATE
    if regime is not None:
        p = shared / "indicators" / "macro_regime.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(regime), encoding="utf-8")
    if dgs10_csv is not None:
        p = shared / "raw" / "macro" / "DGS10.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dgs10_csv, encoding="utf-8")
    if sources is not None:
        p = shared / "raw" / "macro" / "macro_sources.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sources), encoding="utf-8")
    return ws


def _regime(value, source="fred", quality="fred"):
    return {"rates": {"us10y": {"value": value, "source": source,
                                "as_of": "2026-01-14", "data_quality": quality}}}


def test_live_regime_value_used(tmp_path):
    ws = _make_ws(tmp_path, regime=_regime(0.0454))
    rf = rv._resolve_risk_free(str(ws), DATE, CFG)
    assert rf == {"rate": 0.0454, "source": "DGS10", "as_of": "2026-01-14", "note": None}


def test_proxy_regime_labels_ticker(tmp_path):
    ws = _make_ws(tmp_path, regime=_regime(0.0451, source="yfinance:^TNX", quality="proxy"))
    rf = rv._resolve_risk_free(str(ws), DATE, CFG)
    assert rf["source"] == "^TNX"
    assert rf["rate"] == 0.0451


def test_missing_regime_falls_back_to_csv(tmp_path):
    ws = _make_ws(tmp_path, dgs10_csv="date,value\n2026-01-13,4.31\n2026-01-14,4.4\n",
                  sources={"series": {"DGS10": {"source": "fred"}}})
    rf = rv._resolve_risk_free(str(ws), DATE, CFG)
    assert rf["rate"] == 0.044  # percent CSV -> decimal
    assert rf["source"] == "DGS10"
    assert rf["as_of"] == "2026-01-14"


def test_csv_without_provenance_not_labeled_fred(tmp_path):
    # DGS10.csv present but macro_sources.json absent: the data may be ^TNX
    # proxy values, so the source must NOT claim FRED ("DGS10") provenance.
    ws = _make_ws(tmp_path, dgs10_csv="date,value\n2026-01-14,4.4\n")
    rf = rv._resolve_risk_free(str(ws), DATE, CFG)
    assert rf["rate"] == 0.044
    assert rf["source"] == "shared_csv"


def test_out_of_band_live_rejected_via_note_only(tmp_path):
    ws = _make_ws(tmp_path, regime=_regime(0.45))  # 45% — corrupt read
    rf = rv._resolve_risk_free(str(ws), DATE, CFG)
    assert rf["source"] == "config_fallback"
    assert rf["rate"] == CFG["risk_free_rate"]
    assert "out_of_band" in rf["note"]


def test_shared_dir_absent_falls_back_via_note_only(tmp_path):
    ws = _make_ws(tmp_path)
    rf = rv._resolve_risk_free(str(ws), DATE, CFG)
    assert rf["source"] == "config_fallback"
    assert "unavailable" in rf["note"]


def test_regime_missing_quality_skipped(tmp_path):
    ws = _make_ws(tmp_path, regime=_regime(None, source="missing", quality="missing"))
    rf = rv._resolve_risk_free(str(ws), DATE, CFG)
    assert rf["source"] == "config_fallback"


def test_disabled_flag_uses_config(tmp_path):
    ws = _make_ws(tmp_path, regime=_regime(0.0454))
    rf = rv._resolve_risk_free(str(ws), DATE,
                               {"use_live_risk_free": False, "risk_free_rate": 0.042})
    assert rf == {"rate": 0.042, "source": "config_fallback", "as_of": None, "note": None}

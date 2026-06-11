"""Tests for scripts/macro_evidence_cards.py — rule-based projection of the
shared macro regime into per-ticker evidence cards. Synthetic regimes only.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "macro_evidence_cards", ROOT / "scripts" / "macro_evidence_cards.py")
mec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mec)

DATE = "2026-01-15"

BENIGN = {
    "rates": {"us10y": {"value": 0.042, "delta_1m": 0.0005, "direction": "stable",
                        "data_quality": "fred"}},
    "curve": {"slope_2s10s": 0.005, "label": "normal"},
    "volatility": {"vix": 15.0, "percentile_1y": 0.40, "label": "normal"},
    "credit": {"hy_spread": 0.034, "regime": "normal"},
    "policy": {"fedfunds": 0.043, "delta_3m": 0.0, "stance": "on_hold"},
    "usd": {"chg_1m_pct": 0.5},
}

STRESSED = {
    "rates": {"us10y": {"value": 0.048, "delta_1m": 0.004, "direction": "rising",
                        "data_quality": "fred"}},
    "curve": {"slope_2s10s": -0.0012, "label": "inverted", "data_quality": "fred"},
    "volatility": {"vix": 32.0, "percentile_1y": 0.95, "label": "stressed",
                   "data_quality": "proxy"},
    "credit": {"hy_spread": 0.062, "regime": "stressed", "data_quality": "fred"},
    "policy": {"fedfunds": 0.05, "delta_3m": 0.005, "stance": "tightening",
               "data_quality": "fred"},
    "usd": {"chg_1m_pct": 3.1, "data_quality": "proxy"},
}


def test_benign_regime_produces_zero_cards():
    assert mec.build_cards(BENIGN, DATE, "en") == []


def test_stressed_regime_produces_all_cards_with_schema():
    cards = mec.build_cards(STRESSED, DATE, "en")
    assert len(cards) == 6
    for card in cards:
        for field in ("id", "desk", "source_type", "ticker", "title", "summary",
                      "why_it_matters", "materiality_score", "sentiment", "topic_tags"):
            assert field in card, field
        assert card["desk"] == "macro"
        assert card["ticker"] == "MARKET"
        assert "macro" in card["topic_tags"]
        assert 0.5 <= card["materiality_score"] <= 0.8
    assert cards[0]["id"] == f"ev_{DATE}_macro_001"


def test_stressed_credit_outranks_wide():
    wide = dict(STRESSED, credit={"hy_spread": 0.05, "regime": "wide"})
    wide_card = next(c for c in mec.build_cards(wide, DATE, "en") if "credit" in c["topic_tags"])
    stressed_card = next(c for c in mec.build_cards(STRESSED, DATE, "en")
                         if "credit" in c["topic_tags"])
    assert stressed_card["materiality_score"] > wide_card["materiality_score"]


def test_rate_move_sentiment_follows_sign():
    rising = mec.build_cards(STRESSED, DATE, "en")
    rate_card = next(c for c in rising if "rates" in c["topic_tags"])
    assert rate_card["sentiment"] == "negative"
    falling = dict(STRESSED)
    falling["rates"] = {"us10y": {"value": 0.040, "delta_1m": -0.004, "data_quality": "fred"}}
    rate_card = next(c for c in mec.build_cards(falling, DATE, "en")
                     if "rates" in c["topic_tags"])
    assert rate_card["sentiment"] == "positive"


def test_chinese_text_english_keys():
    cards = mec.build_cards(STRESSED, DATE, "ch")
    curve_card = next(c for c in cards if "curve" in c["topic_tags"])
    assert "倒挂" in curve_card["title"]
    assert set(curve_card.keys()) == set(mec.build_cards(STRESSED, DATE, "en")[0].keys())


def test_proxy_curve_inversion_emits_no_recession_card():
    # Keyless short leg is ^IRX (3m bill): 3m10s inverts far earlier than 2s10s,
    # so a proxy-quality inversion must not become a recession-signal card.
    proxied = dict(STRESSED, curve={"slope_2s10s": -0.0012, "label": "inverted",
                                    "data_quality": "proxy"})
    assert not any("curve" in c["topic_tags"] for c in mec.build_cards(proxied, DATE, "en"))
    # FRED-quality inversion still fires (control)
    assert any("curve" in c["topic_tags"] for c in mec.build_cards(STRESSED, DATE, "en"))


def test_source_name_is_per_signal_block():
    cards = mec.build_cards(STRESSED, DATE, "en")
    vix_card = next(c for c in cards if "volatility" in c["topic_tags"])
    credit_card = next(c for c in cards if "credit" in c["topic_tags"])
    assert vix_card["source_name"] == "yfinance"   # volatility block is proxy
    assert credit_card["source_name"] == "FRED"    # credit block is fred


def test_rerun_removes_stale_macro_cards(tmp_path):
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    regime_path = (tmp_path / "workspaces" / "shared" / "market_context" / DATE
                   / "indicators" / "macro_regime.json")
    regime_path.parent.mkdir(parents=True)
    (ws / "resolved_config.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")
    out_dir = ws / "normalized" / DATE / "evidence_cards"

    regime_path.write_text(json.dumps(STRESSED), encoding="utf-8")
    assert len(mec.run(ws, DATE)) > 1

    regime_path.write_text(json.dumps(BENIGN), encoding="utf-8")
    assert mec.run(ws, DATE) == []
    assert list(out_dir.glob(f"ev_{DATE}_macro_*.json")) == []  # stale cards purged


def test_run_writes_files_and_skips_without_regime(tmp_path):
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    assert mec.run(ws, DATE) == []  # no regime: skip, no raise

    regime_path = (tmp_path / "workspaces" / "shared" / "market_context" / DATE
                   / "indicators" / "macro_regime.json")
    regime_path.parent.mkdir(parents=True)
    regime_path.write_text(json.dumps(STRESSED), encoding="utf-8")
    (ws / "resolved_config.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")

    cards = mec.run(ws, DATE)
    out_dir = ws / "normalized" / DATE / "evidence_cards"
    assert len(list(out_dir.glob("ev_*_macro_*.json"))) == len(cards) > 0

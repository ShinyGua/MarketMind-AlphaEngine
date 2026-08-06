"""Tests for scripts/collect_macro_series.py — FRED-first collection with a
per-series *keyless FRED CSV* fallback.

Yahoo/yfinance is no longer used anywhere in this pipeline, so the former
yfinance-proxy fallback (and its treasury x10-vs-percent rescaling) is gone:
the keyless path now fetches the same FRED series over the public fredgraph
CSV endpoint, in FRED units. That also recovers CPI / Fed funds / HY spread,
which had no proxy at all and were always reported missing on keyless runs.

requests is monkeypatched; no real network, no live-artifact numbers.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "collect_macro_series", ROOT / "scripts" / "collect_macro_series.py")
cms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cms)

DATE = "2026-01-15"


class _FakeResp:
    """Serves both FRED shapes: .json() for the keyed API, .text for the CSV."""

    def __init__(self, status_code, observations=None, text=None):
        self.status_code = status_code
        self._obs = observations or []
        self.text = text if text is not None else ""

    def json(self):
        return {"observations": self._obs}


CSV_BODY = "observation_date,VALUE\n2026-01-02,4.54\n2026-01-03,.\n2026-01-06,4.61\n"


def _csv_get(url, params=None, timeout=None):
    """Every series resolves over the keyless CSV endpoint."""
    assert "fredgraph.csv" in url
    return _FakeResp(200, text=CSV_BODY)


@pytest.fixture
def no_dotenv(monkeypatch):
    monkeypatch.setattr(cms, "load_dotenv", lambda *a, **k: {})


def test_keyless_uses_fred_csv_and_recovers_every_series(tmp_path, monkeypatch, no_dotenv):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    import requests
    monkeypatch.setattr(requests, "get", _csv_get)
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)

    result = cms.collect(ws, DATE)

    assert result["series"]["DGS10"]["source"] == "fred_csv"
    # Values arrive in FRED units already — no rescaling, and the '.' gap row is
    # dropped rather than written as a bogus observation.
    csv = (cms.shared_macro_dir(ws, DATE) / "DGS10.csv").read_text(encoding="utf-8")
    assert "4.54" in csv and "4.61" in csv
    assert result["series"]["DGS10"]["rows"] == 2

    # These four have no Yahoo proxy and were permanently missing on keyless
    # runs; the CSV endpoint serves them like any other series.
    for sid in ("CPIAUCSL", "CPILFESL", "FEDFUNDS", "BAMLH0A0HYM2"):
        assert sid not in result["inputs_missing"]
        assert result["series"][sid]["source"] == "fred_csv"

    # fred_csv is real FRED data in FRED units, so it carries full fred quality.
    assert result["mode"] == "fred"


def test_per_series_fred_failure_falls_back_to_csv(tmp_path, monkeypatch, no_dotenv):
    monkeypatch.setenv("FRED_API_KEY", "k")
    import requests

    def mixed_get(url, params=None, timeout=None):
        if "fredgraph.csv" in url:
            return _FakeResp(200, text=CSV_BODY)
        if (params or {}).get("series_id") == "DGS10":
            return _FakeResp(429)  # rate-limited on this series only
        return _FakeResp(200, [{"date": "2026-01-02", "value": "3.1"},
                               {"date": "2026-01-03", "value": "."}])  # '.' gap skipped

    monkeypatch.setattr(requests, "get", mixed_get)
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)

    result = cms.collect(ws, DATE)

    assert result["series"]["DGS10"]["source"] == "fred_csv"
    assert "rate_limited" in result["series"]["DGS10"]["reason"]
    assert result["series"]["DGS2"]["source"] == "fred"
    assert result["series"]["DGS2"]["rows"] == 1  # '.' observation dropped
    assert "DGS10" not in result["inputs_missing"]
    # Keyed API + keyless CSV are the same source family, so the run is not
    # downgraded to "mixed" quality just because one series took the CSV route.
    assert result["mode"] == "fred"


def test_unavailable_cache_is_refetched(tmp_path, monkeypatch, no_dotenv):
    # A transient total failure recorded by an earlier same-day run must not be
    # honored as a cache — the next ticker retries.
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    import requests
    monkeypatch.setattr(requests, "get", _csv_get)
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    out = cms.shared_macro_dir(ws, DATE)
    out.mkdir(parents=True)
    (out / "macro_sources.json").write_text(
        json.dumps({"mode": "unavailable", "series": {}, "inputs_missing": []}),
        encoding="utf-8")

    result = cms.collect(ws, DATE)  # no --force needed
    assert result["mode"] == "fred"
    assert result["series"]["DGS10"]["source"] == "fred_csv"


def test_existing_sources_json_skips_unless_force(tmp_path, monkeypatch, no_dotenv):
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    out = cms.shared_macro_dir(ws, DATE)
    out.mkdir(parents=True)
    sentinel = {"series": {}, "inputs_missing": [], "mode": "fred"}
    (out / "macro_sources.json").write_text(json.dumps(sentinel), encoding="utf-8")

    # no network mocks installed: would raise if it tried to fetch
    assert cms.collect(ws, DATE)["mode"] == "fred"


def test_collect_never_raises_on_total_failure(tmp_path, monkeypatch, no_dotenv):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    import requests

    def dead(url, params=None, timeout=None):
        raise requests.exceptions.ConnectionError("no network")

    monkeypatch.setattr(requests, "get", dead)
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)

    result = cms.collect(ws, DATE)
    assert result["mode"] == "unavailable"
    assert set(result["inputs_missing"]) == set(result["series"].keys())


def test_no_yfinance_fallback_remains(tmp_path, monkeypatch, no_dotenv):
    """Yahoo must not be reachable even if yfinance is importable."""
    assert not hasattr(cms, "fetch_yf_proxy")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    import requests

    def dead(url, params=None, timeout=None):
        raise requests.exceptions.ConnectionError("no network")

    monkeypatch.setattr(requests, "get", dead)

    def _boom(*a, **k):  # any yfinance import attempt fails the test loudly
        raise AssertionError("pipeline attempted a yfinance import")

    monkeypatch.setitem(sys.modules, "yfinance", property(_boom))
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    assert cms.collect(ws, DATE)["mode"] == "unavailable"

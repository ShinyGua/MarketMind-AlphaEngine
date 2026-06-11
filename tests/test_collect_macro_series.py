"""Tests for scripts/collect_macro_series.py — FRED-first collection with
per-series yfinance proxy fallback. requests/yfinance are monkeypatched;
no real network, no live-artifact numbers.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "collect_macro_series", ROOT / "scripts" / "collect_macro_series.py")
cms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cms)

DATE = "2026-01-15"


class _FakeResp:
    def __init__(self, status_code, observations=None):
        self.status_code = status_code
        self._obs = observations or []

    def json(self):
        return {"observations": self._obs}


class _FakeSeries(dict):
    """Mimics a pandas Close series: .items() yields (ts, value)."""


class _FakeTs:
    def __init__(self, s):
        self._s = s

    def strftime(self, fmt):
        return self._s


class _FakeHist:
    def __init__(self, closes):
        self._closes = closes

    @property
    def empty(self):
        return not self._closes

    def __contains__(self, key):
        return key == "Close"

    def __getitem__(self, key):
        s = _FakeSeries()
        for i, v in enumerate(self._closes):
            s[_FakeTs(f"2026-01-{i + 1:02d}")] = v
        return s


def _fake_yfinance(closes_by_symbol):
    mod = types.ModuleType("yfinance")

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, period=None, interval=None):
            return _FakeHist(closes_by_symbol.get(self.symbol, []))

    mod.Ticker = Ticker
    return mod


@pytest.fixture
def no_dotenv(monkeypatch):
    monkeypatch.setattr(cms, "load_dotenv", lambda *a, **k: {})


def test_keyless_uses_proxies_and_marks_missing(tmp_path, monkeypatch, no_dotenv):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "yfinance",
                        _fake_yfinance({"^TNX": [45.4, 45.0], "^VIX": [18.0, 19.0]}))
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)

    result = cms.collect(ws, DATE)

    assert result["series"]["DGS10"]["source"] == "yfinance:^TNX"
    # ^TNX quotes yield x 10 -> divided to FRED percent units
    csv = (cms.shared_macro_dir(ws, DATE) / "DGS10.csv").read_text(encoding="utf-8")
    assert "4.54" in csv and "45.4" not in csv
    # no proxy exists for CPI / FedFunds / HY spread
    for sid in ("CPIAUCSL", "CPILFESL", "FEDFUNDS", "BAMLH0A0HYM2"):
        assert sid in result["inputs_missing"]
        assert result["series"][sid]["source"] == "missing"
        assert result["series"][sid]["reason"] == "no_key"
    assert result["mode"] == "yfinance_proxy"


def test_percent_form_treasury_proxy_not_rescaled(tmp_path, monkeypatch, no_dotenv):
    # current yfinance returns ^TNX already in percent (4.54) — must NOT divide again
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance({"^TNX": [4.53, 4.54]}))
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    cms.collect(ws, DATE)
    csv = (cms.shared_macro_dir(ws, DATE) / "DGS10.csv").read_text(encoding="utf-8")
    assert "4.54" in csv and "0.454" not in csv


def test_per_series_fred_failure_falls_back_to_proxy(tmp_path, monkeypatch, no_dotenv):
    monkeypatch.setenv("FRED_API_KEY", "k")
    import requests

    def fred_get(url, params=None, timeout=None):
        if params["series_id"] == "DGS10":
            return _FakeResp(429)  # rate-limited on this series only
        return _FakeResp(200, [{"date": "2026-01-02", "value": "3.1"},
                               {"date": "2026-01-03", "value": "."}])  # '.' gap skipped

    monkeypatch.setattr(requests, "get", fred_get)
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance({"^TNX": [45.4]}))
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)

    result = cms.collect(ws, DATE)

    assert result["series"]["DGS10"]["source"] == "yfinance:^TNX"
    assert "rate_limited" in result["series"]["DGS10"]["reason"]
    assert result["series"]["DGS2"]["source"] == "fred"
    assert result["series"]["DGS2"]["rows"] == 1  # '.' observation dropped
    assert result["mode"] == "mixed"
    assert "DGS10" not in result["inputs_missing"]


def test_mixed_convention_treasury_window_scaled_per_row(tmp_path, monkeypatch, no_dotenv):
    # Yahoo has flipped ^TNX between x10 (45.4) and percent (4.54) forms; each
    # row must be scaled independently, not by one window-wide factor.
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "yfinance",
                        _fake_yfinance({"^TNX": [45.4, 45.2, 4.51, 4.54]}))
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    cms.collect(ws, DATE)
    csv = (cms.shared_macro_dir(ws, DATE) / "DGS10.csv").read_text(encoding="utf-8")
    values = [float(line.split(",")[1]) for line in csv.strip().splitlines()[1:]]
    assert values == [4.54, 4.52, 4.51, 4.54]


def test_unavailable_cache_is_refetched(tmp_path, monkeypatch, no_dotenv):
    # A transient total failure recorded by an earlier same-day run must not be
    # honored as a cache — the next ticker retries.
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance({"^TNX": [4.5]}))
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)
    out = cms.shared_macro_dir(ws, DATE)
    out.mkdir(parents=True)
    (out / "macro_sources.json").write_text(
        json.dumps({"mode": "unavailable", "series": {}, "inputs_missing": []}),
        encoding="utf-8")

    result = cms.collect(ws, DATE)  # no --force needed
    assert result["mode"] == "yfinance_proxy"
    assert result["series"]["DGS10"]["source"] == "yfinance:^TNX"


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
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance({}))  # every proxy empty
    ws = tmp_path / "workspaces" / "TEST"
    ws.mkdir(parents=True)

    result = cms.collect(ws, DATE)
    assert result["mode"] == "unavailable"
    assert set(result["inputs_missing"]) == set(result["series"].keys())

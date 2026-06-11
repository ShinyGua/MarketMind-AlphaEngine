"""Tests for the preflight diagnostics script (scripts/check_data_sources.py):
DNS + probe classification and — critically — that it reports key presence/length
but never the secret value, and writes sanitized JSON under raw/{date}/diagnostics.
socket + requests are monkeypatched; no real network.
"""
import importlib.util
import json
import socket
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_data_sources", ROOT / "scripts" / "check_data_sources.py")
cds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cds)

SECRET = "supersecret_newsapi_value_123"
DATE = "2026-06-03"


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def keyed_env(monkeypatch):
    monkeypatch.setenv("NEWSAPI_KEY", SECRET)
    monkeypatch.setenv("FRED_API_KEY", "fredkey0000")
    # Don't let a real .env override the test environ (loader is no-override anyway).
    monkeypatch.setattr(cds, "load_dotenv", lambda *a, **k: {})
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "127.0.0.1")  # all DNS ok


def test_all_ok_classifies_auth_ok(keyed_env, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200))
    r = cds.run(show_lengths=True)
    assert r["sources"]["newsapi"] == "auth_ok"
    assert r["sources"]["fred"] == "auth_ok"
    assert all(v == "ok" for v in r["dns"].values())


def test_reports_presence_and_length_not_value(keyed_env, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200))
    r = cds.run(show_lengths=True)
    assert r["keys"]["NEWSAPI_KEY"] == {"present": True, "length": len(SECRET)}
    # The secret value must never appear anywhere in the serialized result.
    assert SECRET not in json.dumps(r)


def test_no_values_omits_length(keyed_env, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200))
    r = cds.run(show_lengths=False)
    assert r["keys"]["NEWSAPI_KEY"] == {"present": True}


def test_dns_failure_short_circuits_probe(keyed_env, monkeypatch):
    monkeypatch.setattr(socket, "gethostbyname",
                        lambda h: (_ for _ in ()).throw(socket.gaierror("no dns")))
    r = cds.run(show_lengths=True)
    assert r["dns"]["newsapi.org"] == "failed"
    assert r["sources"]["newsapi"] == "dns_failed"


def test_auth_failure_classified(keyed_env, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(403))
    assert cds.run(show_lengths=True)["sources"]["newsapi"] == "auth_failed"


def test_missing_key_is_no_key(monkeypatch):
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(cds, "load_dotenv", lambda *a, **k: {})
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "127.0.0.1")
    r = cds.run(show_lengths=True)
    assert r["sources"]["newsapi"] == "no_key"
    assert r["keys"]["NEWSAPI_KEY"]["present"] is False


def test_macro_plan_keyed_routes_all_via_fred(keyed_env, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200))
    plan = cds.run(show_lengths=True)["macro"]
    assert plan["mode"] == "fred"
    assert "CPIAUCSL" in plan["series_via_fred"]
    assert plan["series_via_proxy"] == [] and plan["series_unavailable"] == []


def test_macro_plan_keyless_splits_proxy_vs_unavailable(monkeypatch):
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(cds, "load_dotenv", lambda *a, **k: {})
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "127.0.0.1")
    plan = cds.run(show_lengths=True)["macro"]
    assert plan["mode"] == "yfinance_proxy"
    assert "DGS10" in plan["series_via_proxy"]
    for sid in ("CPIAUCSL", "CPILFESL", "FEDFUNDS", "BAMLH0A0HYM2"):
        assert sid in plan["series_unavailable"]


def test_writes_sanitized_json(tmp_path, keyed_env, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200))
    ws = tmp_path / "ORCL"
    monkeypatch.setattr("sys.argv", ["check_data_sources.py", str(ws), DATE])
    cds.main()
    out = ws / "raw" / DATE / "diagnostics" / "data_sources.json"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert SECRET not in text
    data = json.loads(text)
    assert data["sources"]["newsapi"] == "auth_ok"
    assert data["keys"]["NEWSAPI_KEY"]["present"] is True

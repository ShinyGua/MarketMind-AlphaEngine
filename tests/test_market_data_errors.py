"""Tests for structured data-source failure classification in the market-data MCP
server: dns / network / auth / rate-limit / http reasons (vs the No-KEY cases),
and that _get_news returns those reasons in the fallback envelope instead of
raising. No real network — requests.get is monkeypatched.
"""
import asyncio
import importlib.util
import json
import socket
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "market_data_server", ROOT / "mcp" / "market_data_server.py")
mds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mds)


# ---- _classify_request (pure) -------------------------------------------------

def test_classify_dns_from_connectionerror():
    exc = requests.exceptions.ConnectionError("Failed to resolve 'newsapi.org'")
    assert mds._classify_request(exc=exc) == "dns_failed"


def test_classify_dns_from_gaierror():
    assert mds._classify_request(exc=socket.gaierror(-2, "Name or service not known")) == "dns_failed"


def test_classify_timeout_is_network():
    assert mds._classify_request(exc=requests.exceptions.Timeout("timed out")) == "network_failed"


def test_classify_generic_connectionerror_is_network():
    exc = requests.exceptions.ConnectionError("Connection refused")
    assert mds._classify_request(exc=exc) == "network_failed"


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.parametrize("code,reason", [
    (401, "auth_failed"), (403, "auth_failed"),
    (429, "rate_limited"), (500, "http_failed"), (404, "http_failed"),
])
def test_classify_http_statuses(code, reason):
    assert mds._classify_request(resp=_Resp(code)) == reason


# ---- _get_news returns classified reasons ------------------------------------

class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _run_news(monkeypatch, getter):
    monkeypatch.setenv("NEWSAPI_KEY", "dummy_key_value")
    monkeypatch.setattr(requests, "get", getter)
    out = asyncio.run(mds._get_news({"query": "Oracle", "max_results": 1}))
    return json.loads(out[0].text)


def test_get_news_dns_failure(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("Failed to resolve 'newsapi.org'")
    r = _run_news(monkeypatch, boom)
    assert r["fallback_needed"] is True
    assert r["reason"] == "dns_failed"
    assert r["provider"] == "newsapi"


def test_get_news_auth_failure(monkeypatch):
    r = _run_news(monkeypatch, lambda *a, **k: _FakeResp(401))
    assert r["fallback_needed"] is True and r["reason"] == "auth_failed"


def test_get_news_rate_limited(monkeypatch):
    r = _run_news(monkeypatch, lambda *a, **k: _FakeResp(429))
    assert r["reason"] == "rate_limited"


def test_get_news_success_passes_through(monkeypatch):
    payload = {"articles": [{"title": "t", "source": {"name": "X"}, "url": "u",
                             "publishedAt": "2026-06-03", "description": "d"}]}
    r = _run_news(monkeypatch, lambda *a, **k: _FakeResp(200, payload))
    assert "fallback_needed" not in r
    assert r["metadata"]["source"] == "newsapi"
    assert r["articles"][0]["title"] == "t"


def test_get_news_no_key(monkeypatch):
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    out = asyncio.run(mds._get_news({"query": "Oracle"}))
    assert json.loads(out[0].text)["reason"] == "No NEWSAPI_KEY"

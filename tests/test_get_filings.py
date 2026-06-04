"""Tests for _get_filings rebuilt on the EDGAR submissions API: ticker->CIK
resolution, form/lookback filtering, limit, URL construction, and structured
fallback envelopes. requests.get is monkeypatched (URL-dispatched) — no network.
"""
import asyncio
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "market_data_server", ROOT / "mcp" / "market_data_server.py")
mds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mds)

TODAY = datetime.now(timezone.utc)
RECENT = TODAY.strftime("%Y-%m-%d")
OLD = (TODAY - timedelta(days=400)).strftime("%Y-%m-%d")

TICKERS_JSON = {"0": {"cik_str": 1341439, "ticker": "ORCL", "title": "ORACLE CORP"}}
SUBMISSIONS = {
    "name": "ORACLE CORP",
    "filings": {"recent": {
        "form": ["8-K", "10-Q", "4", "10-K"],
        "filingDate": [RECENT, RECENT, RECENT, OLD],
        "accessionNumber": ["0001-24-001", "0001-24-002", "0001-24-003", "0001-24-004"],
        "primaryDocument": ["a8k.htm", "a10q.htm", "form4.xml", "a10k.htm"],
        "primaryDocDescription": ["8-K", "10-Q", "OWNERSHIP", "10-K"],
    }},
}


@pytest.fixture(autouse=True)
def _clear_cache():
    mds._CIK_CACHE.clear()
    yield
    mds._CIK_CACHE.clear()


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _dispatch(tickers=TICKERS_JSON, submissions=SUBMISSIONS, sub_status=200):
    def fake_get(url, **kw):
        if "company_tickers.json" in url:
            return _Resp(tickers)
        if "submissions/CIK" in url:
            return _Resp(submissions, status=sub_status)
        raise AssertionError(f"unexpected url {url}")
    return fake_get


def _call(monkeypatch, getter, args):
    monkeypatch.setattr(requests, "get", getter)
    out = asyncio.run(mds._get_filings(args))
    return json.loads(out[0].text)


def test_resolves_cik_and_returns_filings(monkeypatch):
    r = _call(monkeypatch, _dispatch(), {"ticker": "ORCL", "lookback_days": 30})
    forms = [f["form_type"] for f in r["filings"]]
    assert forms == ["8-K", "10-Q", "4"]          # the 10-K is 400d old -> filtered
    assert r["metadata"]["source"] == "sec_edgar"
    f0 = r["filings"][0]
    assert f0["company_name"] == "ORACLE CORP"
    # CIK is zero-stripped to int in the path; accession dashes removed
    assert f0["url"] == "https://www.sec.gov/Archives/edgar/data/1341439/000124001/a8k.htm"


def test_form_type_filter(monkeypatch):
    r = _call(monkeypatch, _dispatch(),
              {"ticker": "ORCL", "filing_types": ["10-Q"], "lookback_days": 30})
    assert [f["form_type"] for f in r["filings"]] == ["10-Q"]


def test_limit_caps_results(monkeypatch):
    r = _call(monkeypatch, _dispatch(), {"ticker": "ORCL", "limit": 1, "lookback_days": 30})
    assert len(r["filings"]) == 1


def test_lookback_window(monkeypatch):
    # widen the window so the 400-day-old 10-K is included
    r = _call(monkeypatch, _dispatch(), {"ticker": "ORCL", "lookback_days": 500})
    assert "10-K" in [f["form_type"] for f in r["filings"]]


def test_unknown_ticker_is_cik_not_found(monkeypatch):
    r = _call(monkeypatch, _dispatch(), {"ticker": "ZZZZ"})
    assert r["fallback_needed"] is True and r["reason"] == "cik_not_found"


def test_dns_failure_classified(monkeypatch):
    def boom(url, **kw):
        raise requests.exceptions.ConnectionError("Failed to resolve 'sec.gov'")
    r = _call(monkeypatch, boom, {"ticker": "ORCL"})
    assert r["fallback_needed"] is True and r["reason"] == "dns_failed"


def test_submissions_http_error(monkeypatch):
    r = _call(monkeypatch, _dispatch(sub_status=403), {"ticker": "ORCL"})
    assert r["fallback_needed"] is True and r["reason"] == "auth_failed"

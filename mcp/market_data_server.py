"""MarketMind MCP server — wraps all external data source calls.

Exposes seven tools over stdio:
  get_price_history   — daily OHLCV via scripts/price_router.py (NASDAQ, akshare Sina, FRED)
  get_news            — headlines via NewsAPI
  get_filings         — SEC EDGAR full-text search
  get_macro_series    — FRED economic series
  get_company_info    — explicit "unavailable" (no approved profile source)
  get_fundamentals    — US: SEC EDGAR XBRL; CN/HK: explicit "unavailable"
  get_earnings_calendar — US: NASDAQ estimated earnings date; CN/HK: "unavailable"

Yahoo/yfinance is not used anywhere — not as a source and not as a fallback.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import mcp.types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# ── shared rate limiter ─────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared.rate_limiter import LIMITERS  # noqa: E402

# Load API keys from the project .env so NEWSAPI_KEY / FRED_API_KEY resolve even
# when Claude Code didn't expand them into the launch environment. File values
# are a fallback — anything already in os.environ wins.
from shared.dotenv import load_dotenv  # noqa: E402
load_dotenv()

server = Server("market-data")

# ── helpers ─────────────────────────────────────────────────────────────

_ISO_NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


def _round_floats(val, decimals: int = 2):
    """Recursively round floats inside nested dicts/lists."""
    if isinstance(val, float):
        return round(val, decimals)
    if isinstance(val, dict):
        return {k: _round_floats(v, decimals) for k, v in val.items()}
    if isinstance(val, list):
        return [_round_floats(v, decimals) for v in val]
    return val


def _ok(payload: dict) -> list[mcp.types.TextContent]:
    return [mcp.types.TextContent(type="text", text=json.dumps(payload, default=str))]


def _err(tool: str, error: Exception) -> list[mcp.types.TextContent]:
    return [mcp.types.TextContent(
        type="text",
        text=json.dumps({"error": str(error), "tool": tool}),
    )]


# DNS-failure signatures surfaced (often wrapped in a ConnectionError) when the
# execution sandbox blocks outbound name resolution.
_DNS_MARKERS = (
    "name or service not known", "nodename nor servname",
    "failed to resolve", "getaddrinfo", "temporary failure in name resolution",
    "name resolution",
)


def _classify_request(exc: Exception | None = None, resp=None) -> str:
    """Map a network exception or HTTP response to a structured failure reason so
    desks can tell apart dns_failed / network_failed / auth_failed / rate_limited /
    http_failed (vs the No-KEY cases handled separately)."""
    import socket

    import requests

    if exc is not None:
        if isinstance(exc, socket.gaierror):
            return "dns_failed"
        if isinstance(exc, requests.exceptions.Timeout):
            return "network_failed"
        if isinstance(exc, (requests.exceptions.ConnectionError, OSError)):
            if any(m in str(exc).lower() for m in _DNS_MARKERS):
                return "dns_failed"
            return "network_failed"
        return "network_failed"
    if resp is not None:
        if resp.status_code in (401, 403):
            return "auth_failed"
        if resp.status_code == 429:
            return "rate_limited"
        if resp.status_code >= 400:
            return "http_failed"
    return "http_failed"


# ── tool schemas ────────────────────────────────────────────────────────

TOOLS = [
    mcp.types.Tool(
        name="get_price_history",
        description=(
            "Fetch daily OHLCV history from approved sources (US: api.nasdaq.com; "
            "CN/HK stocks and indices: akshare Sina; ^VIX, BTC-USD, FX: FRED, "
            "close-only). Prices are as traded (unadjusted). Intraday intervals "
            "are unavailable; 1wk/1mo are resampled from daily bars."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticker symbols, e.g. ['NVDA','SPY']",
                },
                "period": {
                    "type": "string",
                    "enum": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y"],
                    "description": "Lookback period",
                },
                "interval": {
                    "type": "string",
                    "enum": ["1m", "5m", "15m", "1h", "1d", "1wk", "1mo"],
                    "description": "Bar interval",
                },
            },
            "required": ["tickers", "period", "interval"],
        },
    ),
    mcp.types.Tool(
        name="get_news",
        description=(
            "Search news articles via NewsAPI. Returns headlines with titles, "
            "sources, URLs, and publication dates."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "endpoint": {
                    "type": "string",
                    "enum": ["everything", "top-headlines"],
                    "default": "everything",
                },
                "category": {
                    "type": "string",
                    "enum": ["business", "technology", "general", "science", "health"],
                    "description": "Category (top-headlines only)",
                },
                "language": {"type": "string", "default": "en"},
                "max_results": {"type": "integer", "default": 15, "minimum": 1, "maximum": 100},
                "lookback_hours": {"type": "integer", "default": 36, "minimum": 1, "maximum": 168},
                "sort_by": {
                    "type": "string",
                    "default": "publishedAt",
                    "enum": ["publishedAt", "relevancy", "popularity"],
                },
            },
            "required": ["query"],
        },
    ),
    mcp.types.Tool(
        name="get_filings",
        description=(
            "Search SEC EDGAR for recent filings (10-K, 10-Q, 8-K, Form 4, etc.) "
            "by ticker or company name."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "company_name": {"type": "string", "description": "Optional company name for broader search"},
                "filing_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["10-K", "10-Q", "8-K", "4"],
                },
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 40},
                "lookback_days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365},
            },
            "required": ["ticker"],
        },
    ),
    mcp.types.Tool(
        name="get_macro_series",
        description=(
            "Fetch economic time series from FRED (e.g. DGS10, VIXCLS, DTWEXBGS). "
            "Returns date-value arrays for each series."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "series_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "FRED series IDs, e.g. ['DGS10','VIXCLS']",
                },
                "lookback_months": {"type": "integer", "default": 3, "minimum": 1, "maximum": 24},
            },
            "required": ["series_ids"],
        },
    ),
    mcp.types.Tool(
        name="get_company_info",
        description=("Company profile. No approved source exists, so this returns an "
                     "explicit 'unavailable' with where to look instead."),
        inputSchema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    ),
    mcp.types.Tool(
        name="get_fundamentals",
        description=(
            "Fetch valuation ratios and recent annual statements (revenue, EBIT, "
            "D&A, capex, tax) for a US ticker from SEC EDGAR XBRL. CN/HK names "
            "return an explicit 'unavailable'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    ),
    mcp.types.Tool(
        name="get_earnings_calendar",
        description=("Next earnings date for a US ticker from NASDAQ (labelled "
                     "'estimated' until the company announces it). CN/HK: 'unavailable'."),
        inputSchema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    ),
]


# ── list_tools ──────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[mcp.types.Tool]:
    return TOOLS


# ── tool implementations ────────────────────────────────────────────────

_EMPTY_OHLCV = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}

# Calendar months fetched per requested period (a little slack for holidays).
_PERIOD_MONTHS = {"1d": 0.3, "5d": 0.5, "1mo": 1.2, "3mo": 3.2, "6mo": 6.2, "1y": 12.2, "2y": 24.2}
_PRICE_CALL_TIMEOUT_S = 60
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _scripts_on_path() -> None:
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)


def _ohlcv_from_rows(rows: list[dict]) -> dict:
    """price_router rows -> the tool's column arrays. Close-only series keep None
    for open/high/low rather than inventing values."""
    def r2(v):
        return round(float(v), 2) if v is not None else None

    return {
        "dates": [r["Date"] for r in rows],
        "open": [r2(r.get("Open")) for r in rows],
        "high": [r2(r.get("High")) for r in rows],
        "low": [r2(r.get("Low")) for r in rows],
        "close": [r2(r.get("Close")) for r in rows],
        "volume": [int(r["Volume"]) if r.get("Volume") is not None else None for r in rows],
    }


def _pick(fn, a, b):
    """max/min over the non-None values; None when both are missing."""
    vals = [v for v in (a, b) if v is not None]
    return fn(vals) if vals else None


def _resample(rows: list[dict], interval: str) -> list[dict]:
    """Aggregate daily rows into weekly (ISO week) or monthly bars, dated by the
    bucket's last trading day."""
    from datetime import date as _d

    def key(ds: str):
        d = _d.fromisoformat(ds)
        return d.isocalendar()[:2] if interval == "1wk" else (d.year, d.month)

    out: list[dict] = []
    for r in rows:
        if out and key(out[-1]["_last"]) == key(r["Date"]):
            b = out[-1]
            b["High"] = _pick(max, b["High"], r.get("High"))
            b["Low"] = _pick(min, b["Low"], r.get("Low"))
            b["Close"] = r["Close"]
            b["Volume"] = (b["Volume"] or 0) + (r.get("Volume") or 0)
            b["_last"] = b["Date"] = r["Date"]
        else:
            out.append({**r, "_last": r["Date"]})
    for b in out:
        b.pop("_last", None)
    return out


async def _get_price_history(arguments: dict) -> list[mcp.types.TextContent]:
    """Daily bars from approved sources via scripts/price_router.py (no Yahoo).

    Each symbol is fetched independently: one failure yields an empty series plus
    an entry in ``errors`` and never aborts the others. Only daily bars exist
    upstream; 1wk/1mo are resampled and intraday intervals are reported
    unavailable rather than approximated.
    """
    import asyncio

    _scripts_on_path()
    from price_router import fetch_any

    tickers = arguments["tickers"]
    if isinstance(tickers, str):
        tickers = [tickers]
    period = arguments.get("period", "1y")
    interval = arguments.get("interval", "1d")

    if interval not in ("1d", "1wk", "1mo"):
        reason = f"interval {interval} unavailable: approved sources provide daily bars only"
        return _ok({
            "data": {t: dict(_EMPTY_OHLCV) for t in tickers},
            "errors": {t: reason for t in tickers},
            "metadata": {"fetched_at": _ISO_NOW(), "count": 0, "unavailable": True},
        })

    months = _PERIOD_MONTHS.get(period, 12.2)
    loop = asyncio.get_running_loop()
    data: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for ticker in tickers:
        await LIMITERS["prices"].acquire()
        try:
            rows, prov = await asyncio.wait_for(
                loop.run_in_executor(None, lambda t=ticker: fetch_any(t, months=months)),
                timeout=_PRICE_CALL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - isolate per symbol
            data[ticker] = dict(_EMPTY_OHLCV)
            errors[ticker] = (str(exc) or type(exc).__name__)[:200]
            continue
        if period in ("1d", "5d"):
            rows = rows[-(1 if period == "1d" else 5):]
        if interval != "1d":
            rows = _resample(rows, interval)
        data[ticker] = _ohlcv_from_rows(rows)
        sources[ticker] = prov

    total = sum(len(v["dates"]) for v in data.values())
    return _ok({
        "data": data,
        "errors": errors,
        "metadata": {"fetched_at": _ISO_NOW(), "count": total, "sources": sources,
                     "adjusted": False},
    })


async def _get_news(arguments: dict) -> list[mcp.types.TextContent]:
    import requests
    from datetime import timedelta

    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        return _ok({"fallback_needed": True, "reason": "No NEWSAPI_KEY"})

    endpoint = arguments.get("endpoint", "everything")
    query = arguments["query"]
    language = arguments.get("language", "en")
    max_results = arguments.get("max_results", 15)
    lookback_hours = arguments.get("lookback_hours", 36)
    sort_by = arguments.get("sort_by", "publishedAt")
    category = arguments.get("category")

    await LIMITERS["newsapi"].acquire()

    from_date = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%S")

    url = f"https://newsapi.org/v2/{endpoint}"
    params: dict = {
        "apiKey": api_key,
        "language": language,
        "pageSize": max_results,
        "sortBy": sort_by,
    }

    if endpoint == "everything":
        params["q"] = query
        params["from"] = from_date
    else:
        # top-headlines
        params["q"] = query
        if category:
            params["category"] = category

    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code >= 400:
            return _ok({"fallback_needed": True, "reason": _classify_request(resp=resp),
                        "provider": "newsapi", "status_code": resp.status_code})
        body = resp.json()
    except (requests.exceptions.RequestException, OSError) as exc:
        return _ok({"fallback_needed": True, "reason": _classify_request(exc=exc),
                    "provider": "newsapi"})

    articles = []
    for art in body.get("articles", [])[:max_results]:
        articles.append({
            "title": art.get("title"),
            "source": art.get("source", {}).get("name"),
            "url": art.get("url"),
            "published_at": art.get("publishedAt"),
            "description": art.get("description"),
        })

    return _ok({
        "articles": articles,
        "metadata": {"source": "newsapi", "count": len(articles), "fetched_at": _ISO_NOW()},
    })


# SEC requires a descriptive User-Agent with contact info on every request.
_SEC_UA = "MarketMind/1.0 (research@marketmind.dev)"
_CIK_CACHE: dict[str, str] = {}  # TICKER -> zero-padded 10-digit CIK


def _resolve_cik(ticker: str, requests) -> str | None:
    """Map a ticker to its zero-padded 10-digit CIK via SEC's company_tickers map
    (fetched once, cached). Returns None if the ticker isn't found."""
    t = ticker.upper()
    if not _CIK_CACHE:
        resp = requests.get("https://www.sec.gov/files/company_tickers.json",
                            headers={"User-Agent": _SEC_UA}, timeout=15)
        if resp.status_code >= 400:
            return None
        for row in resp.json().values():
            _CIK_CACHE[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    return _CIK_CACHE.get(t)


async def _get_filings(arguments: dict) -> list[mcp.types.TextContent]:
    import requests
    from datetime import timedelta

    ticker = arguments["ticker"]
    filing_types = set(arguments.get("filing_types", ["10-K", "10-Q", "8-K", "4"]))
    limit = arguments.get("limit", 5)
    lookback_days = arguments.get("lookback_days", 30)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    await LIMITERS["edgar"].acquire()

    # Canonical, deterministic path: the EDGAR submissions API keyed by CIK
    # (data.sec.gov/submissions/CIK{cik}.json). Replaces the brittle full-text
    # search call (quoted forms + bogus dateRange) that errored in practice.
    try:
        cik = _resolve_cik(ticker, requests)
        if not cik:
            return _ok({"fallback_needed": True, "reason": "cik_not_found",
                        "provider": "sec_edgar", "ticker": ticker})
        resp = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                            headers={"User-Agent": _SEC_UA}, timeout=15)
        if resp.status_code >= 400:
            return _ok({"fallback_needed": True, "reason": _classify_request(resp=resp),
                        "provider": "sec_edgar", "status_code": resp.status_code})
        sub = resp.json()
    except (requests.exceptions.RequestException, OSError) as exc:
        return _ok({"fallback_needed": True, "reason": _classify_request(exc=exc),
                    "provider": "sec_edgar"})

    company_name = sub.get("name")
    recent = (sub.get("filings") or {}).get("recent") or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    descs = recent.get("primaryDocDescription", [])
    cik_int = int(cik)

    filings = []
    for i, form in enumerate(forms):  # arrays are newest-first
        if filing_types and form not in filing_types:
            continue
        fdate = dates[i] if i < len(dates) else None
        if fdate and fdate < cutoff:
            continue
        accn = accns[i] if i < len(accns) else ""
        doc = docs[i] if i < len(docs) else ""
        accn_nodash = accn.replace("-", "")
        if doc:
            url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/{doc}"
        elif accn:
            url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/{accn}-index.htm"
        else:
            url = None
        filings.append({
            "form_type": form,
            "filed_date": fdate,
            "company_name": company_name,
            "description": descs[i] if i < len(descs) else "",
            "url": url,
        })
        if len(filings) >= limit:
            break

    return _ok({
        "filings": filings,
        "metadata": {"source": "sec_edgar", "count": len(filings), "fetched_at": _ISO_NOW()},
    })


async def _get_macro_series(arguments: dict) -> list[mcp.types.TextContent]:
    import requests
    from datetime import timedelta

    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return _ok({"fallback_needed": True, "reason": "No FRED_API_KEY"})

    series_ids = arguments["series_ids"]
    lookback_months = arguments.get("lookback_months", 3)

    start_date = (datetime.now(timezone.utc) - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")

    series: dict[str, dict] = {}
    for sid in series_ids:
        await LIMITERS["fred"].acquire()

        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": sid,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start_date,
        }

        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code >= 400:
                return _ok({"fallback_needed": True, "reason": _classify_request(resp=resp),
                            "provider": "fred", "status_code": resp.status_code})
            body = resp.json()
        except (requests.exceptions.RequestException, OSError) as exc:
            return _ok({"fallback_needed": True, "reason": _classify_request(exc=exc),
                        "provider": "fred"})

        dates = []
        values = []
        for obs in body.get("observations", []):
            dates.append(obs["date"])
            raw = obs["value"]
            if raw == ".":
                values.append(None)
            else:
                try:
                    values.append(round(float(raw), 4))
                except (ValueError, TypeError):
                    values.append(None)

        series[sid] = {"dates": dates, "values": values}

    return _ok({
        "series": series,
        "metadata": {"source": "fred", "count": len(series), "fetched_at": _ISO_NOW()},
    })


def _unavailable(tool: str, ticker: str, reason: str, use_instead: str) -> list[mcp.types.TextContent]:
    """Explicit 'no approved source' answer — never a silent Yahoo fallback."""
    return _ok({
        "ticker": ticker,
        "unavailable": True,
        "reason": reason,
        "use_instead": use_instead,
        "metadata": {"tool": tool, "fetched_at": _ISO_NOW()},
    })


def _is_us_listing(ticker: str) -> bool:
    _scripts_on_path()
    from fetch_prices_nasdaq import FetchError
    from price_router import classify
    try:
        return classify(ticker)[0] == "nasdaq"
    except FetchError:
        return False


async def _get_company_info(arguments: dict) -> list[mcp.types.TextContent]:
    """No approved source carries a full company profile (sector, market cap,
    description) for every market, so this tool answers explicitly."""
    return _unavailable(
        "get_company_info", arguments["ticker"],
        "no approved company-profile source (Yahoo is not used)",
        "US: scripts/nasdaq_fetch.py (quote + exchange) and get_filings (EDGAR); "
        "CN/HK: exchange website or web search with provenance")


async def _get_earnings_calendar(arguments: dict) -> list[mcp.types.TextContent]:
    """US: NASDAQ's analyst earnings-date endpoint. The date is an *estimate*
    (Zacks, derived from past reporting dates) until the company announces it,
    and is labelled so. CN/HK: explicit unavailable."""
    import asyncio
    import re
    import urllib.request

    ticker = arguments["ticker"]
    if not _is_us_listing(ticker):
        return _unavailable(
            "get_earnings_calendar", ticker,
            "no approved earnings-calendar source for this market",
            "CN: akshare stock_report_disclosure (scheduled disclosure dates); "
            "HK: exchange announcements / web search")

    _scripts_on_path()
    from fetch_prices_nasdaq import HEADERS

    url = f"https://api.nasdaq.com/api/analyst/{ticker.strip().upper()}/earnings-date"

    def fetch() -> dict:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    await LIMITERS["prices"].acquire()
    payload = await asyncio.get_running_loop().run_in_executor(None, fetch)
    data = payload.get("data") or {}
    text = " ".join(str(data.get(k) or "") for k in ("announcement", "reportText"))
    next_date = None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", data.get("reportText") or "")
    if m:
        next_date = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    status = "estimated" if "estimated" in text.lower() else "announced"
    return _ok({
        "next_earnings_date": next_date,
        "status": status if next_date else "unknown",
        "source_text": (data.get("announcement") or "")[:200],
        "earnings_dates": [],
        "metadata": {"source": "api.nasdaq.com/analyst/earnings-date", "ticker": ticker,
                     "fetched_at": _ISO_NOW()},
    })


async def _get_fundamentals(arguments: dict) -> list[mcp.types.TextContent]:
    """US: SEC EDGAR XBRL via scripts/fetch_fundamentals_edgar.build() — the same
    metrics / income_statement / cash_flow shape this tool always returned.
    CN/HK: explicit unavailable."""
    import asyncio

    ticker = arguments["ticker"]
    if not _is_us_listing(ticker):
        return _unavailable(
            "get_fundamentals", ticker,
            "no approved fundamentals source wired for this market",
            "CN: akshare stock_financial_abstract / stock_financial_report_sina; "
            "HK: company filings via web search")

    _scripts_on_path()
    from fetch_fundamentals_edgar import build

    await LIMITERS["edgar"].acquire()
    result = await asyncio.wait_for(
        asyncio.get_running_loop().run_in_executor(None, lambda: build(ticker)),
        timeout=120)
    return _ok(_round_floats(result, 4))


# ── dispatcher ──────────────────────────────────────────────────────────

_DISPATCH = {
    "get_price_history": _get_price_history,
    "get_news": _get_news,
    "get_filings": _get_filings,
    "get_macro_series": _get_macro_series,
    "get_company_info": _get_company_info,
    "get_fundamentals": _get_fundamentals,
    "get_earnings_calendar": _get_earnings_calendar,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[mcp.types.TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    try:
        return await handler(arguments)
    except Exception as exc:
        return _err(name, exc)


# ── entry point ─────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

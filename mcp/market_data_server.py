"""MarketMind MCP server — wraps all external data source calls.

Exposes seven tools over stdio:
  get_price_history   — stock/index prices via yfinance
  get_news            — headlines via NewsAPI
  get_filings         — SEC EDGAR full-text search
  get_macro_series    — FRED economic series
  get_company_info    — company profile via yfinance
  get_fundamentals    — valuation ratios + financial statements via yfinance
  get_earnings_calendar — earnings dates via yfinance
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
            "Fetch OHLCV price history for one or more tickers via yfinance. "
            "Returns date-indexed arrays of open/high/low/close/volume."
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
        description="Fetch company profile (sector, industry, market cap, etc.) via yfinance.",
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
            "Fetch valuation ratios (PE, EV/EBITDA, EV/Rev, margins, growth, ROE, "
            "beta) and recent annual financial statements (revenue, EBIT, D&A, "
            "capex, tax) for a ticker via yfinance. Used to build DCF and comps."
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
        description="Fetch upcoming and recent earnings dates for a ticker via yfinance.",
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


def _ohlcv_from_frame(frame, pd) -> dict:
    """Extract an OHLCV dict from a single-level (Open/High/Low/Close/Volume) frame.
    Missing columns degrade to all-None rather than raising."""
    n = len(frame.index)

    def col(name):
        return frame[name] if name in frame.columns else [None] * n

    return {
        "dates": [d.isoformat() for d in frame.index],
        "open": [round(float(v), 2) if pd.notna(v) else None for v in col("Open")],
        "high": [round(float(v), 2) if pd.notna(v) else None for v in col("High")],
        "low": [round(float(v), 2) if pd.notna(v) else None for v in col("Low")],
        "close": [round(float(v), 2) if pd.notna(v) else None for v in col("Close")],
        "volume": [int(v) if pd.notna(v) else None for v in col("Volume")],
    }


async def _get_price_history(arguments: dict) -> list[mcp.types.TextContent]:
    import yfinance as yf
    import pandas as pd

    tickers = arguments["tickers"]
    if isinstance(tickers, str):
        tickers = [tickers]
    period = arguments["period"]
    interval = arguments["interval"]

    await LIMITERS["yfinance"].acquire()

    df = yf.download(
        tickers,
        period=period,
        interval=interval,
        group_by="ticker",
        progress=False,
        threads=True,
    )

    data: dict[str, dict] = {}

    # yfinance with group_by="ticker" returns MultiIndex columns (ticker, field)
    # even for ONE ticker, so always select the ticker's sub-frame when MultiIndex
    # before reading Open/High/…; a flat frame (older yfinance / single string) is
    # used as-is. This unifies single- and multi-ticker and avoids KeyError: 'Open'.
    multi = isinstance(df.columns, pd.MultiIndex)
    for ticker in tickers:
        if df is None or df.empty:
            data[ticker] = dict(_EMPTY_OHLCV)
            continue
        try:
            sub = df[ticker] if multi else df
        except KeyError:
            data[ticker] = dict(_EMPTY_OHLCV)
            continue
        data[ticker] = dict(_EMPTY_OHLCV) if sub.empty else _ohlcv_from_frame(sub, pd)

    total = sum(len(v["dates"]) for v in data.values())
    return _ok({
        "data": data,
        "metadata": {"fetched_at": _ISO_NOW(), "count": total},
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


async def _get_company_info(arguments: dict) -> list[mcp.types.TextContent]:
    import yfinance as yf

    ticker = arguments["ticker"]

    await LIMITERS["yfinance"].acquire()

    info = yf.Ticker(ticker).info

    _FIELDS = [
        "shortName", "longName", "exchange", "sector", "industry",
        "marketCap", "currency", "country", "website",
        "longBusinessSummary", "fullTimeEmployees",
    ]
    _KEY_MAP = {
        "shortName": "name",
        "longName": "long_name",
        "exchange": "exchange",
        "sector": "sector",
        "industry": "industry",
        "marketCap": "market_cap",
        "currency": "currency",
        "country": "country",
        "website": "website",
        "longBusinessSummary": "description",
        "fullTimeEmployees": "full_time_employees",
    }

    result = {}
    for yf_key in _FIELDS:
        out_key = _KEY_MAP[yf_key]
        val = info.get(yf_key)
        result[out_key] = val

    result["ticker"] = ticker
    result["metadata"] = {"source": "yfinance", "fetched_at": _ISO_NOW()}

    return _ok(result)


async def _get_earnings_calendar(arguments: dict) -> list[mcp.types.TextContent]:
    import yfinance as yf

    ticker = arguments["ticker"]

    await LIMITERS["yfinance"].acquire()

    t = yf.Ticker(ticker)

    # .calendar may be a dict or DataFrame depending on yfinance version
    next_earnings_date = None
    try:
        cal = t.calendar
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed:
                next_earnings_date = ed[0].isoformat() if hasattr(ed[0], "isoformat") else str(ed[0])
        elif hasattr(cal, "iloc"):
            # DataFrame
            if "Earnings Date" in cal.index:
                val = cal.loc["Earnings Date"].iloc[0]
                next_earnings_date = val.isoformat() if hasattr(val, "isoformat") else str(val)
    except Exception:
        pass

    # .earnings_dates — a DataFrame of historical/upcoming dates
    earnings_dates: list[str] = []
    try:
        ed_df = t.earnings_dates
        if ed_df is not None and not ed_df.empty:
            earnings_dates = [d.isoformat() for d in ed_df.index[:20]]
    except Exception:
        pass

    return _ok({
        "next_earnings_date": next_earnings_date,
        "earnings_dates": earnings_dates,
        "metadata": {"source": "yfinance", "ticker": ticker, "fetched_at": _ISO_NOW()},
    })


# ── fundamentals helpers ──────────────────────────────────────────────────

# yfinance statement row labels vary across versions/tickers; try each candidate.
_STATEMENT_ROWS = {
    "revenue": ["Total Revenue", "TotalRevenue", "Operating Revenue"],
    "ebit": ["EBIT", "Operating Income", "OperatingIncome", "Total Operating Income As Reported"],
    "pretax_income": ["Pretax Income", "PretaxIncome", "Income Before Tax"],
    "tax_provision": ["Tax Provision", "TaxProvision", "Income Tax Expense"],
    "net_income": ["Net Income", "NetIncome", "Net Income Common Stockholders"],
}
_CASHFLOW_ROWS = {
    "operating_cash_flow": ["Operating Cash Flow", "Total Cash From Operating Activities",
                             "Cash Flow From Continuing Operating Activities"],
    "capex": ["Capital Expenditure", "Capital Expenditures", "Purchase Of PPE"],
    "dep_amort": ["Depreciation And Amortization", "Depreciation Amortization Depletion",
                  "Depreciation", "Reconciled Depreciation"],
}


def _extract_rows(df, row_map: dict) -> list[dict]:
    """Pull the most recent annual columns from a yfinance statement DataFrame.

    Returns a list of per-year dicts, newest first, with the keys in row_map.
    Missing rows/values come back as None rather than raising.
    """
    if df is None or not hasattr(df, "columns") or df.empty:
        return []

    def find_row(candidates):
        for name in candidates:
            if name in df.index:
                return df.loc[name]
        return None

    series = {key: find_row(cands) for key, cands in row_map.items()}

    out = []
    for col in df.columns[:4]:  # most recent up to 4 fiscal years
        year = col.year if hasattr(col, "year") else str(col)
        entry = {"period": str(year)}
        for key, s in series.items():
            val = None
            if s is not None:
                try:
                    raw = s.get(col)
                    if raw is not None and not (isinstance(raw, float) and raw != raw):
                        val = float(raw)
                except Exception:
                    val = None
            entry[key] = val
        out.append(entry)
    return out


async def _get_fundamentals(arguments: dict) -> list[mcp.types.TextContent]:
    import yfinance as yf

    ticker = arguments["ticker"]

    await LIMITERS["yfinance"].acquire()

    t = yf.Ticker(ticker)
    info = t.info or {}

    # Valuation ratios + key metrics straight from .info
    _INFO_FIELDS = {
        "quoteType": "quote_type",
        "currency": "currency",
        "marketCap": "market_cap",
        "enterpriseValue": "enterprise_value",
        "trailingPE": "trailing_pe",
        "forwardPE": "forward_pe",
        "priceToBook": "price_to_book",
        "enterpriseToRevenue": "ev_to_revenue",
        "enterpriseToEbitda": "ev_to_ebitda",
        "profitMargins": "profit_margins",
        "operatingMargins": "operating_margins",
        "grossMargins": "gross_margins",
        "revenueGrowth": "revenue_growth",
        "earningsGrowth": "earnings_growth",
        "returnOnEquity": "return_on_equity",
        "freeCashflow": "free_cashflow",
        "operatingCashflow": "operating_cashflow",
        "totalCash": "total_cash",
        "totalDebt": "total_debt",
        "totalRevenue": "total_revenue",
        "ebitda": "ebitda",
        "sharesOutstanding": "shares_outstanding",
        "beta": "beta",
        "currentPrice": "current_price",
    }
    metrics = {}
    for yf_key, out_key in _INFO_FIELDS.items():
        metrics[out_key] = info.get(yf_key)

    # Annual financial statements (best-effort; may be empty for funds/ETFs).
    income_statement = []
    cash_flow = []
    try:
        income_statement = _extract_rows(t.financials, _STATEMENT_ROWS)
    except Exception:
        income_statement = []
    try:
        cash_flow = _extract_rows(t.cashflow, _CASHFLOW_ROWS)
    except Exception:
        cash_flow = []

    result = {
        "ticker": ticker,
        "metrics": metrics,
        "income_statement": income_statement,  # newest first
        "cash_flow": cash_flow,                # newest first
        "metadata": {"source": "yfinance", "fetched_at": _ISO_NOW()},
    }
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

"""MarketMind MCP server — wraps all external data source calls.

Exposes six tools over stdio:
  get_price_history   — stock/index prices via yfinance
  get_news            — headlines via NewsAPI
  get_filings         — SEC EDGAR full-text search
  get_macro_series    — FRED economic series
  get_company_info    — company profile via yfinance
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

async def _get_price_history(arguments: dict) -> list[mcp.types.TextContent]:
    import yfinance as yf
    import pandas as pd

    tickers = arguments["tickers"]
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

    if len(tickers) == 1:
        # yf.download returns flat columns for a single ticker
        ticker = tickers[0]
        if df.empty:
            data[ticker] = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
        else:
            dates = [d.isoformat() for d in df.index]
            data[ticker] = {
                "dates": dates,
                "open": [round(float(v), 2) if pd.notna(v) else None for v in df["Open"]],
                "high": [round(float(v), 2) if pd.notna(v) else None for v in df["High"]],
                "low": [round(float(v), 2) if pd.notna(v) else None for v in df["Low"]],
                "close": [round(float(v), 2) if pd.notna(v) else None for v in df["Close"]],
                "volume": [int(v) if pd.notna(v) else None for v in df["Volume"]],
            }
    else:
        for ticker in tickers:
            try:
                sub = df[ticker]
                if sub.empty:
                    data[ticker] = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
                    continue
                dates = [d.isoformat() for d in sub.index]
                data[ticker] = {
                    "dates": dates,
                    "open": [round(float(v), 2) if pd.notna(v) else None for v in sub["Open"]],
                    "high": [round(float(v), 2) if pd.notna(v) else None for v in sub["High"]],
                    "low": [round(float(v), 2) if pd.notna(v) else None for v in sub["Low"]],
                    "close": [round(float(v), 2) if pd.notna(v) else None for v in sub["Close"]],
                    "volume": [int(v) if pd.notna(v) else None for v in sub["Volume"]],
                }
            except KeyError:
                data[ticker] = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}

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

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    body = resp.json()

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


async def _get_filings(arguments: dict) -> list[mcp.types.TextContent]:
    import requests
    from datetime import timedelta

    ticker = arguments["ticker"]
    company_name = arguments.get("company_name")
    filing_types = arguments.get("filing_types", ["10-K", "10-Q", "8-K", "4"])
    limit = arguments.get("limit", 5)
    lookback_days = arguments.get("lookback_days", 30)

    await LIMITERS["edgar"].acquire()

    query = company_name if company_name else ticker
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    forms = ",".join(f'"{ft}"' for ft in filing_types)

    url = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q": query,
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
        "forms": forms,
    }
    headers = {"User-Agent": "MarketMind/1.0 (research@marketmind.dev)"}

    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    body = resp.json()

    filings = []
    hits = body.get("hits", body.get("filings", []))
    if isinstance(hits, dict):
        hits = hits.get("hits", [])

    for hit in hits[:limit]:
        source = hit.get("_source", hit)
        filings.append({
            "form_type": source.get("form_type") or source.get("forms") or source.get("type"),
            "filed_date": source.get("file_date") or source.get("filed_date") or source.get("date_filed"),
            "company_name": source.get("entity_name") or source.get("company_name") or source.get("display_names", [None])[0],
            "description": source.get("display_description") or source.get("description", ""),
            "url": _build_filing_url(source),
        })

    return _ok({
        "filings": filings,
        "metadata": {"source": "sec_edgar", "count": len(filings), "fetched_at": _ISO_NOW()},
    })


def _build_filing_url(source: dict) -> str | None:
    """Construct an EDGAR filing URL from hit metadata."""
    file_num = source.get("file_num")
    accession = source.get("accession_no") or source.get("accession_number")
    if accession:
        clean = accession.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{file_num}/{clean}/{accession}-index.htm" if file_num else \
               f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&accession={accession}"
    return source.get("file_url") or source.get("url")


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

        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        body = resp.json()

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


# ── dispatcher ──────────────────────────────────────────────────────────

_DISPATCH = {
    "get_price_history": _get_price_history,
    "get_news": _get_news,
    "get_filings": _get_filings,
    "get_macro_series": _get_macro_series,
    "get_company_info": _get_company_info,
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

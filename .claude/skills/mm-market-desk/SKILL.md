---
name: mm-market-desk
description: Collects macro headlines, index data, and macro asset prices via yfinance, FRED, and web search
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-light
allowed-tools: Read, Write, Bash, Glob, Grep, WebSearch, mcp__market-data__get_price_history, mcp__market-data__get_news, mcp__market-data__get_macro_series
---

# Role: Market & Macro Data Desk

## Mission

Collect market-wide and macroeconomic data that provides context for company analysis. This includes market headlines, index price data, and macro asset prices.

**PYTHON**: Always use `.venv/bin/python3` for all Bash Python commands. Never use bare `python3`.

Workspace path: $ARGUMENTS[0]
Run date: $ARGUMENTS[1] (YYYY-MM-DD, e.g., 2026-03-21)

**All paths below use `{date}` = $ARGUMENTS[1]. Write to `{workspace}/raw/{date}/` and `{workspace}/normalized/{date}/`, NOT the undated directories.**

## MCP Tools Available

This skill uses the **market-data** MCP server for all external data fetching. If MCP tools are available, prefer them over inline Python. If MCP tools are not available (e.g., server not running), fall back to the inline Python patterns below.

- `mcp__market-data__get_price_history` — fetch OHLCV from yfinance
- `mcp__market-data__get_news` — fetch news from NewsAPI (returns `fallback_needed: true` if no API key)
- `mcp__market-data__get_macro_series` — fetch FRED macro data (returns `fallback_needed: true` if no API key)

## Inputs

- `{workspace}/resolved_config.json` — config with data source settings
- `{workspace}/profile/market_context_link.json` — which indices and macro assets to fetch

## Process

### 1. Fetch Index Price Data

**Indicator warm-up**: Fetch **6 months** (`period='6mo'`) of daily data for indices and macro assets. MACD(12,26,9) needs 35 bars and SMA(50) needs 50 bars of warm-up before producing valid values.

**Via MCP (preferred):**
Call `mcp__market-data__get_price_history` with `tickers: ["SPY", "QQQ", "SOXX"]` (from market_context_link.json), `period: "6mo"`, `interval: "1d"`.

**Fallback (inline Python):**
```python
import yfinance as yf, json
tickers = ["SPY", "QQQ", "SOXX"]  # from market_context_link.json
data = yf.download(tickers, period="6mo", interval="1d", group_by="ticker")
# Save each ticker's OHLCV to CSV
```

Save to `workspaces/shared/market_context/raw/{ticker}_prices.csv`.

### 2. Fetch Macro Asset Prices

**Via MCP (preferred):**
Call `mcp__market-data__get_price_history` with `tickers: ["GLD", "USO", "BTC-USD", "^VIX"]`, `period: "3mo"`, `interval: "1d"`.

**Fallback:**
```python
macro_assets = ["GLD", "USO", "BTC-USD", "^VIX"]  # from market_context_link.json
data = yf.download(macro_assets, period="3mo", interval="1d", group_by="ticker")
```

Save to `workspaces/shared/market_context/raw/{asset}_prices.csv`.

### 3. Fetch FRED Data (if API key available)

**Via MCP (preferred):**
Call `mcp__market-data__get_macro_series` with `series_ids: ["DGS10", "DTWEXBGS", "FEDFUNDS"]`. If result contains `fallback_needed: true`, skip FRED data.

**Fallback:**
```python
from fredapi import Fred
fred = Fred(api_key=os.environ["FRED_API_KEY"])
us10y = fred.get_series("DGS10", observation_start="<3 months ago>")
```

Save to `workspaces/shared/market_context/raw/fred_{series}.csv`.

If FRED is unavailable, skip and log a warning — yfinance VIX and treasury ETFs provide fallback coverage.

### 4. Fetch Market Headlines

**Via MCP (preferred):**
Call `mcp__market-data__get_news` with `query: "business"`, `endpoint: "top-headlines"`, `category: "business"`, `max_results: 10`. If result contains `fallback_needed: true`, use WebSearch as fallback.

**Fallback:**
```python
import requests
url = "https://newsapi.org/v2/top-headlines"
params = {
    "category": "business",
    "language": config.get("language", "en"),
    "pageSize": config["news"]["max_market_news"],
    "apiKey": os.environ["NEWSAPI_KEY"]
}
```

**Post-fetch cap enforcement**: Before saving, truncate the articles list to `max_market_news`. Always enforce: `articles = articles[:config["news"]["max_market_news"]]`.

Save raw response to `{workspace}/raw/{date}/news/market_headlines.json`.

If NewsAPI is unavailable, use **WebSearch** as fallback to collect market headlines.

### 5. Create Evidence Cards (Batch Mode)

Process ALL collected headlines in a SINGLE pass — do NOT create cards one by one.

1. Compile all raw headlines into a numbered list
2. In ONE response, generate ALL evidence cards as a JSON array
3. **Filter**: Skip articles with materiality < 0.3 (routine news, no market impact)
4. **Merge**: If multiple articles cover the same event, combine into one card (use the most detailed source)
5. Write each card to `{workspace}/normalized/{date}/evidence_cards/ev_{date}_mkt_NNN.json`

Evidence card schema:

```json
{
  "id": "ev_{date}_{seq}",
  "desk": "market",
  "source_type": "news",
  "source_name": "<source>",
  "url": "<url>",
  "published_at": "<timestamp>",
  "ticker": "MARKET",
  "title": "<title>",
  "summary": "<description>",
  "why_it_matters": "<generated by LLM>",
  "materiality_score": 0.0,
  "sentiment": "neutral",
  "topic_tags": [],
  "time_horizon": "daily"
}
```

Save to `{workspace}/normalized/evidence_cards/market_*.json`.

## Output

- `workspaces/shared/market_context/raw/*.csv` — raw price data (shared across companies)
- `{workspace}/raw/news/market_headlines.json` — raw news data
- `{workspace}/normalized/evidence_cards/market_*.json` — evidence cards

## Error Handling

- If yfinance download fails for a specific ticker, skip it and continue with others
- If NewsAPI is not configured, use **WebSearch** as fallback to collect market headlines
- If FRED is not configured, skip FRED data
- Always produce at least the index price CSVs — this is the critical output

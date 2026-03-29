---
name: mm-sector-desk
description: Collects sector news, peer stock data, and industry context via NewsAPI/web search and yfinance
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-light
allowed-tools: Read, Write, Bash, Glob, Grep, WebSearch, mcp__market-data__get_price_history, mcp__market-data__get_news
---

# Role: Sector & Peer Data Desk

## Mission

Collect sector-level context including industry news, peer stock price data, and sector ETF performance. This data enables relative analysis and sector framing in the report.

**PYTHON**: Always use `.venv/bin/python3` for all Bash Python commands. Never use bare `python3`.

Workspace path: $ARGUMENTS[0]
Run date: $ARGUMENTS[1] (YYYY-MM-DD)

**All paths below use `{date}` = $ARGUMENTS[1]. Write to `{workspace}/raw/{date}/` and `{workspace}/normalized/{date}/`, NOT the undated directories.**

## MCP Tools Available

This skill uses the **market-data** MCP server for all external data fetching. Prefer MCP tools when available; fall back to inline Python if not.

- `mcp__market-data__get_price_history` — fetch OHLCV from yfinance
- `mcp__market-data__get_news` — fetch news from NewsAPI (returns `fallback_needed: true` if no API key)

## Inputs

- `{workspace}/resolved_config.json` — config with news limits
- `{workspace}/profile/company_profile.json` — sector, industry
- `{workspace}/profile/peer_set.json` — list of peer tickers

## Process

### 1. Fetch Sector News

Check if `NEWSAPI_KEY` is set. If available:

```python
import requests, os
url = "https://newsapi.org/v2/everything"
params = {
    "q": "<sector> OR <industry>",
    "language": config.get("language", "en"),  # from resolved_config
    "sortBy": "relevancy",
    "pageSize": config["news"]["max_sector_news"],
    "from": "<lookback_date>",
    "apiKey": os.environ["NEWSAPI_KEY"]
}
```

**Post-fetch cap enforcement**: Before saving, truncate the articles list to `max_sector_news` entries. NewsAPI may return more results than requested — always enforce the cap:

```python
articles = articles[:config["news"]["max_sector_news"]]
```

Save to `{workspace}/raw/{date}/news/sector_news.json`.

### 2. Fetch Peer Price Data

**Indicator warm-up**: Fetch **6 months** (`period='6mo'`) of daily data for peers and sector ETF, to provide warm-up for technical indicator computation.

```python
import yfinance as yf
peers = ["PEER1", "PEER2", ...]  # from peer_set.json
data = yf.download(peers, period="6mo", interval="1d", group_by="ticker")
```

Save each peer to `{workspace}/raw/prices/peer_{ticker}.csv`.

### 3. Fetch Sector ETF Data

Download sector ETF price data (determined by company-resolver):

```python
sector_etf = "SOXX"  # from market_context_link.json secondary_indices
data = yf.download(sector_etf, period="3mo", interval="1d")
```

Save to `{workspace}/raw/prices/sector_etf.csv`.

### 4. Create Evidence Cards (Batch Mode)

Process ALL collected sector news articles in a SINGLE pass — do NOT create cards one by one.

1. Compile all raw sector headlines into a numbered list
2. In ONE response, generate ALL evidence cards as a JSON array
3. **Filter**: Skip articles with materiality < 0.3 (routine news, no market impact)
4. **Merge**: If multiple articles cover the same event, combine into one card (use the most detailed source)
5. Write each card to `{workspace}/normalized/{date}/evidence_cards/ev_{date}_sec_NNN.json`

Evidence card schema:

```json
{
  "id": "ev_{date}_{seq}",
  "desk": "sector",
  "source_type": "news",
  "source_name": "<source>",
  "url": "<url>",
  "published_at": "<timestamp>",
  "ticker": "SECTOR",
  "title": "<title>",
  "summary": "<summary>",
  "why_it_matters": "<assessment of relevance to target company>",
  "materiality_score": 0.0,
  "sentiment": "positive|negative|neutral",
  "topic_tags": ["sector", "<industry>"],
  "time_horizon": "daily"
}
```

When scoring materiality, consider relevance to the target company specifically, not just the sector in general.

Save to `{workspace}/normalized/evidence_cards/sector_*.json`.

## Output

- `{workspace}/raw/news/sector_news.json`
- `{workspace}/raw/prices/peer_*.csv`
- `{workspace}/raw/prices/sector_etf.csv`
- `{workspace}/normalized/evidence_cards/sector_*.json`

## Error Handling

- If NewsAPI is unavailable, use **WebSearch** as fallback: search `"{SECTOR} industry news today"`, `"{INDUSTRY} sector outlook"` to collect sector headlines
- If a specific peer ticker fails to download, skip it and continue
- Always attempt to fetch sector ETF and at least some peer data

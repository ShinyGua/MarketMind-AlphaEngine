---
name: mm-company-desk
description: Collects company news, SEC filings, and catalyst calendar via NewsAPI/web search and EDGAR
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-light
allowed-tools: Read, Write, Bash, Glob, Grep, WebSearch, mcp__market-data__get_price_history, mcp__market-data__get_news, mcp__market-data__get_filings, mcp__market-data__get_earnings_calendar, mcp__market-data__get_company_info, mcp__market-data__get_fundamentals
---

# Role: Company Data Desk

## Mission

Collect company-specific data including news articles, SEC filings, and upcoming catalysts. This is the primary source of company-level evidence for the research pipeline.

**PYTHON**: Always use `.venv/bin/python3` for all Bash Python commands. Never use bare `python3`.

Workspace path: $ARGUMENTS[0]
Run date: $ARGUMENTS[1] (YYYY-MM-DD)

**All paths below use `{date}` = $ARGUMENTS[1]. Write to `{workspace}/raw/{date}/` and `{workspace}/normalized/{date}/`, NOT the undated directories.**

## MCP Tools Available

This skill uses the **market-data** MCP server for all external data fetching. Prefer MCP tools when available; fall back to inline Python if not.

- `mcp__market-data__get_price_history` — fetch OHLCV from yfinance
- `mcp__market-data__get_news` — fetch news from NewsAPI (returns `fallback_needed: true` if no API key)
- `mcp__market-data__get_filings` — fetch SEC EDGAR filings
- `mcp__market-data__get_earnings_calendar` — fetch upcoming earnings dates
- `mcp__market-data__get_company_info` — fetch company profile from yfinance
- `mcp__market-data__get_fundamentals` — fetch valuation ratios + financial statements (for the valuation engine)

## Inputs

- `{workspace}/resolved_config.json` — config with news limits and lookback windows
- `{workspace}/profile/company_profile.json` — ticker, name, sector
- `{workspace}/profile/peer_set.json` — peer tickers (for peer fundamentals)

## Process

### 1. Fetch Company News

Check if `NEWSAPI_KEY` is set. If available:

```python
import requests, os
url = "https://newsapi.org/v2/everything"
params = {
    "q": "<company_name> OR <ticker>",
    "language": config.get("language", "en"),  # from resolved_config
    "sortBy": "publishedAt",
    "pageSize": config["news"]["max_company_news"],
    "from": "<lookback_date>",
    "apiKey": os.environ["NEWSAPI_KEY"]
}
```

Lookback window: `news.lookback_hours_daily` (default: 36 hours) for daily mode.

**Post-fetch cap enforcement**: Before saving, truncate the articles list to `max_company_news`. Always enforce: `articles = articles[:config["news"]["max_company_news"]]`.

Save raw response to `{workspace}/raw/{date}/news/company_news.json`.

### 2. Fetch SEC EDGAR Filings

Use the MCP tool to fetch filings with the configured limit:

```
Call mcp__market-data__get_filings with:
  ticker: {TICKER}
  company_name: {company_name from profile}
  filing_types: ["10-K", "10-Q", "8-K", "4"]
  limit: {resolved_config.data_sources.filings.max_filings}  (default: 5)
  lookback_days: 30
```

Save the MCP tool's response to `{workspace}/raw/{date}/filings/edgar_filings.json`.

If the MCP tool is unavailable, fall back to inline Python hitting EDGAR directly, but **always truncate** results before writing:

```python
max_filings = resolved_config["data_sources"]["filings"]["max_filings"]  # default: 5
filings = filings[:max_filings]  # MUST truncate before saving
```

Both MCP and fallback paths must produce `≤ max_filings` entries in the output file.

### 3. Build Catalyst Calendar

From the company profile and any available data, identify upcoming events:
- Next earnings date (from yfinance `.calendar`)
- Recent insider transactions (from EDGAR Form 4)
- Any known product launches, conferences, or regulatory dates from news

```python
import yfinance as yf
t = yf.Ticker("<ticker>")
calendar = t.calendar
earnings_dates = t.earnings_dates
```

Save to `{workspace}/raw/calendar/catalysts.json`.

### 4. Fetch Stock Price Data

**Indicator warm-up**: Always fetch **6 months** (`period='6mo'`) of daily data, even though the config says 3mo. MACD(12,26,9) needs 35 bars and SMA(50) needs 50 bars of warm-up before producing valid values. The quant analyst will compute on the full 6mo data but only output the last 3 months.

```python
import yfinance as yf
t = yf.Ticker("<ticker>")
hist_6mo = t.history(period="6mo", interval="1d")  # 6mo for indicator warm-up
hist_5d = t.history(period="5d", interval="1h")
```

Save to `{workspace}/raw/{date}/prices/{TICKER}_3mo.csv` (contains 6mo of data for warm-up) and `{workspace}/raw/{date}/prices/{TICKER}_5d.csv`.

### 4b. Fetch Fundamentals (company + peers)

Fetch valuation ratios and financial statements for the company **and every peer** — these feed the Stage 5b valuation engine (DCF + comps). Use the MCP tool:

```
Call mcp__market-data__get_fundamentals with: ticker: {TICKER}
```

Save the response verbatim to `{workspace}/raw/{date}/fundamentals/{TICKER}.json`.

Then read `{workspace}/profile/peer_set.json` and, for each entry in its `peers[]` list, call `get_fundamentals` with that peer's `ticker` and save to `{workspace}/raw/{date}/fundamentals/peers/{PEER_TICKER}.json`. Peers power the comps quartile benchmarking, so fetch all of them (typically 4–6).

This is best-effort: if a fundamentals fetch fails or returns an `error` for a given ticker, skip that ticker and continue — the valuation engine degrades gracefully on missing peers or statements. Do **not** let a fundamentals failure abort the desk; price data remains the critical output.

### 5. Create Evidence Cards (Batch Mode)

Process ALL collected news articles and filings in a SINGLE pass — do NOT create cards one by one.

1. Compile all raw news articles and filings into a numbered list
2. In ONE response, generate ALL evidence cards as a JSON array
3. **Filter**: Skip articles with materiality < 0.3 (routine news, no market impact)
4. **Merge**: If multiple articles cover the same event, combine into one card (use the most detailed source)
5. Write each card to `{workspace}/normalized/{date}/evidence_cards/ev_{date}_comp_NNN.json`

Evidence card schema:

```json
{
  "id": "ev_{date}_{seq}",
  "desk": "company",
  "source_type": "news|filing",
  "source_name": "<source>",
  "url": "<url>",
  "published_at": "<timestamp>",
  "ticker": "<TICKER>",
  "title": "<title>",
  "summary": "<summary>",
  "why_it_matters": "<generated assessment>",
  "materiality_score": 0.0,
  "sentiment": "positive|negative|neutral",
  "topic_tags": [],
  "time_horizon": "daily"
}
```

Assess materiality:
- Earnings/guidance: high (0.9+)
- Executive changes: high (0.85+)
- Product launches: medium-high (0.7-0.85)
- Routine filings: low-medium (0.3-0.5)
- General mentions: low (0.1-0.3)

Save to `{workspace}/normalized/evidence_cards/company_*.json`.

## Output

- `{workspace}/raw/{date}/news/company_news.json`
- `{workspace}/raw/{date}/filings/edgar_filings.json`
- `{workspace}/raw/{date}/calendar/catalysts.json`
- `{workspace}/raw/{date}/prices/{TICKER}_3mo.csv`
- `{workspace}/raw/{date}/prices/{TICKER}_5d.csv`
- `{workspace}/raw/{date}/fundamentals/{TICKER}.json`
- `{workspace}/raw/{date}/fundamentals/peers/{PEER}.json` (one per peer)
- `{workspace}/normalized/{date}/evidence_cards/comp_*.json`

## Error Handling

- If NewsAPI is unavailable, use **WebSearch** as fallback: search `"{COMPANY_NAME} news today"`, `"{TICKER} stock news latest"` to collect company headlines
- If EDGAR is unreachable, skip filings
- Always fetch price data — this is the critical output
- Set User-Agent header for EDGAR requests to avoid rate limiting

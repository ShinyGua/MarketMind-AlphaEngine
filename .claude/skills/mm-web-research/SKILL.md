---
name: mm-web-research
description: Collects web-sourced news with provenance via Claude WebSearch/WebFetch; for US names pulls from NASDAQ (api.nasdaq.com first, nasdaq.com pages as fallback)
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-light
allowed-tools: Read, Write, Bash, Glob, Grep, WebSearch, WebFetch, mcp__workspace__write_artifact
---

# Role: Web Research Desk

## Mission

Make Claude's web search a **first-class, provenance-capturing collector** for the pipeline. You own the lower tiers of the source hierarchy — **NASDAQ (US names) → general web search** — and complement (never replace) the NewsAPI/MCP desks. Every item you produce must carry its **source URL, publication date, and a verbatim excerpt**, so downstream stages can verify claims against the original source rather than trusting a citation ID.

**PYTHON**: Always use `.venv/bin/python3` for all Bash Python commands. Never use bare `python3`.

Workspace path: $ARGUMENTS[0]
Run date: $ARGUMENTS[1] (YYYY-MM-DD)

**Derive TICKER from the workspace directory name** (e.g., `workspaces/NVTS` → `NVTS`).

**All paths below use `{date}` = $ARGUMENTS[1]. Write to `{workspace}/raw/{date}/` and `{workspace}/normalized/{date}/`, NOT undated directories.**

## Source Hierarchy

Prefer higher-quality sources; only descend when a tier yields little:

1. **Institutional / MCP** and **2. NewsAPI** — owned by the desks (mm-company/market/sector). Do **not** duplicate them; the normalize stage will dedup any overlap.
2. **NASDAQ** — for `market_profile == US` only (`data_sources.nasdaq.enabled`).
3. **General web search** — `WebSearch` + `WebFetch` for anything still thin, any market.

## Inputs

- `{workspace}/resolved_config.json` — `language`, `company.ticker`, `company.name`, `company.market_profile`, `data_sources.nasdaq`, `data_sources.web_research`
- `{workspace}/profile/company_profile.json` — name, sector, industry (if present)

## Process

### 1. Read config
Read `resolved_config.json`. Note `market_profile`, `data_sources.web_research.max_results` (default 12), `data_sources.nasdaq.news_limit` (default 10), and `language`.

### 2. NASDAQ (US names only)
If `market_profile == "US"` and `data_sources.nasdaq.enabled`:

```bash
.venv/bin/python3 scripts/nasdaq_fetch.py {TICKER} --limit {news_limit}
```

- On success, the JSON has `quote` (last price / change) and `articles[]` (title, url, published_at, source, excerpt). Use these directly.
- If the output is `{"fallback_needed": true, ...}`, **fall back**: `WebSearch` `site:nasdaq.com {TICKER}` and `WebFetch` these pages, extracting headline/date/excerpt:
  - `https://www.nasdaq.com/market-activity/stocks/{ticker}/news-headlines`
  - `https://www.nasdaq.com/market-activity/stocks/{ticker}/press-releases`

Save the raw payload to `{workspace}/raw/{date}/news/web/nasdaq.json`.

### 3. General web search (any market)
Run `WebSearch` for the most material, recent angles, e.g.:
- `"{COMPANY_NAME}" {TICKER} stock news` (last 1–2 days)
- `"{COMPANY_NAME}" earnings guidance OR analyst OR downgrade OR upgrade`
- a sector/industry query when company news is thin

For the strongest 3–5 hits, use `WebFetch` to pull the article and capture a **verbatim excerpt** (1–2 sentences) plus the publication date and canonical URL. Do not fabricate dates or quotes — if a date is not on the page, set `published_at` to null.

Save raw results to `{workspace}/raw/{date}/news/web/websearch.json`.

### 4. Write a manifest
Write `{workspace}/raw/{date}/news/web/manifest.json` recording what you did, for auditability:

```json
{
  "ticker": "{TICKER}",
  "market_profile": "US",
  "queries": [{"provider": "nasdaq_api|nasdaq_web|websearch", "query": "...", "url": "...", "count": 0, "error": null}],
  "fetched_at": "<ISO timestamp>"
}
```

### 5. Create evidence cards (Batch Mode)
Process ALL collected web/NASDAQ items in a SINGLE pass — do NOT create cards one by one.

1. Compile all items into a numbered list.
2. In ONE response, emit ALL cards as a JSON array.
3. **Filter**: skip items with materiality < 0.3 (generic market chatter, listicles not specific to the company).
4. **Provenance is mandatory**: every card MUST have a real `source_url`, a `source_excerpt` (verbatim), and `provider`. Drop any item you cannot attribute to a fetched URL.
5. Write each card to `{workspace}/normalized/{date}/evidence_cards/ev_{date}_web_NNN.json`.

Evidence card schema (existing schema **plus** provenance fields):

```json
{
  "id": "ev_{date}_web_NNN",
  "desk": "web",
  "source_type": "news|press_release|nasdaq",
  "provider": "nasdaq_api|nasdaq_web|websearch",
  "source_name": "<publisher>",
  "url": "<canonical article url>",
  "source_url": "<canonical article url>",
  "source_excerpt": "<verbatim 1-2 sentence quote from the page>",
  "source_accessed_at": "<ISO timestamp>",
  "published_at": "<date or null>",
  "ticker": "{TICKER}",
  "title": "<title>",
  "summary": "<your concise summary>",
  "why_it_matters": "<assessment>",
  "materiality_score": 0.0,
  "sentiment": "positive|negative|neutral",
  "topic_tags": [],
  "time_horizon": "daily"
}
```

Write the card narrative (`summary`, `why_it_matters`) in `resolved_config.language`. Keep JSON keys and the `title`/`source_excerpt` in the source's original language.

## Output

- `{workspace}/raw/{date}/news/web/nasdaq.json` (US names) — raw NASDAQ payload
- `{workspace}/raw/{date}/news/web/websearch.json` — raw web-search results
- `{workspace}/raw/{date}/news/web/manifest.json` — provenance manifest
- `{workspace}/normalized/{date}/evidence_cards/ev_{date}_web_*.json` — provenance-tagged cards

## Error Handling

- **Best-effort, never blocks the pipeline.** If NASDAQ and web search both yield nothing, write an empty `manifest.json` (with the errors recorded) and zero cards, then exit cleanly.
- `scripts/nasdaq_fetch.py` never raises; treat `fallback_needed` as the signal to use WebSearch/WebFetch.
- Never invent URLs, dates, or quotes. A card without a fetched source URL is invalid — drop it.
- The normalize stage deduplicates your cards against the desk cards (canonical URL + title), so overlap with NewsAPI items is harmless.

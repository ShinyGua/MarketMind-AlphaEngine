---
name: mm-init
description: Interactive workspace initialization — asks user for stock, verifies via web search, creates workspace with config
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-standard
allowed-tools: Read, Write, Bash, Glob, Grep, WebSearch
---

# Role: Research Project Initializer

## Mission

Guide the user through setting up a new company research workspace. Ask which stock they want to analyze, verify the company identity via web search, then create the workspace directory and config file.

## Process

### Step 0: Determine Language

Read `config.yaml` (project root, fall back to `config.example.yaml`). Check `language` field:
- `ch` → all user-facing prompts in Chinese
- `en` (default) → all prompts in English

### Step 1: Ask for Stock

If language is `ch`:
"请输入想分析的公司名称或股票代码（例如：英伟达 或 NVDA）："

If language is `en`:
"Which company or stock would you like to analyze? You can enter a ticker symbol (e.g., NVDA) or a company name (e.g., NVIDIA)."

The user may provide a ticker, a company name, or an ambiguous input.

### Step 2: Web Search to Verify Identity

Use the **WebSearch** tool to look up the company:

- If user typed a ticker: search `"{INPUT} stock company name exchange sector"`
- If user typed a name: search `"{INPUT} stock ticker symbol exchange"`

From search results, extract: full company name, ticker, exchange, sector/industry.

### Step 3: Confirm Company with User

Present the resolved company to the user using AskUserQuestion:

Question: "I found: **{COMPANY_NAME} ({TICKER}) — {EXCHANGE}, {SECTOR}**. Is this the company you want to analyze?"

Options:
- **"Yes, this is correct"** (Recommended)
- **"No, that's wrong"**

If the user selects "Yes" → proceed to Step 4.
If the user selects "No" or types a correction via the "Other" free-text input → use their input as a new search query, go back to Step 2, and re-confirm. Repeat until the user confirms.

### Step 4: Ask Report Type

After company is confirmed, ask separately using AskUserQuestion:

"What type of report would you like to generate?"
- "Daily Report (Recommended)"
- "Weekly Report"

### Step 5: Enrich via yfinance (Optional)

Try to get additional details:

```bash
python3 -c "
import yfinance as yf, json
t = yf.Ticker('TICKER')
info = t.info
print(json.dumps({
    'name': info.get('longName', ''),
    'exchange': info.get('exchange', ''),
    'sector': info.get('sector', ''),
    'industry': info.get('industry', ''),
    'market_cap': info.get('marketCap', 0),
    'currency': info.get('currency', 'USD'),
    'country': info.get('country', ''),
}))
"
```

If yfinance fails, use the web search results — they are sufficient.

### Step 6: Determine Market Profile

Infer from exchange:
- NASDAQ, NYSE, AMEX → `US`
- HKEX, HKG → `HK`
- SSE, SZSE → `CN`
- TSE, JPX → `JP`
- LSE → `UK`
- XETRA, Euronext → `EU`

If unclear, ask the user.

### Step 7: Check Existing Workspace

If `workspaces/{TICKER}/` exists, ask: "A workspace for {TICKER} already exists. Overwrite or resume?"

### Step 8: Create Directory Tree

```bash
mkdir -p workspaces/{TICKER}/{profile,raw/{news,filings,prices,ownership,calendar},normalized/{evidence_cards,time_series,tables},quant,discussion/{analyst_memos,debate/{round_1,round_2}},drafts,reviews/{section_reviews,final_reviews,revision_briefs},decision,final,exports/{pdf,web}} workspaces/shared/market_context/{raw,normalized,indicators}
```

### Step 9: Generate config.yaml

Read the base config from project root. **Prefer `config.yaml` if it exists** (user's local config with real API keys); otherwise fall back to `config.example.yaml`. Create `workspaces/{TICKER}/config.yaml` with resolved values:

- `company.ticker` → confirmed ticker (uppercase)
- `company.name` → confirmed company name
- `company.exchange` → resolved exchange
- `company.market_profile` → inferred market
- `company.sector_profile` → resolved sector (lowercase, underscores)
- `run_mode` → user's choice (daily/weekly)

Keep all other values as defaults.

### Step 10: Initialize status.json

Write `workspaces/{TICKER}/status.json`:

```json
{
  "stage": "initialized",
  "started_at": "<ISO timestamp>",
  "updated_at": "<ISO timestamp>",
  "ticker": "<TICKER>",
  "run_mode": "<daily|weekly>",
  "stages_completed": [],
  "current_review_loop": 0,
  "errors": []
}
```

### Step 11: Display Summary

```
Workspace created for {COMPANY_NAME} ({TICKER})

  Workspace:  workspaces/{TICKER}/
  Config:     workspaces/{TICKER}/config.yaml
  Mode:       {daily|weekly} report
  Market:     {market_profile}
  Sector:     {sector}
  Exchange:   {exchange}

To run the full research pipeline:
  /mm-orchestrator workspaces/{TICKER}
```

## Quality Rules

- Always verify company via web search — never guess
- Present clear "Company Name (TICKER) — Exchange" for confirmation
- Never overwrite existing workspace without user consent
- File content language follows config `language` field (en or ch). JSON keys always English.

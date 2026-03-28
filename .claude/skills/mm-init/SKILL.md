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

Output the result as text and wait for the user's chat reply:

"I found: **{COMPANY_NAME} ({TICKER}) — {EXCHANGE}, {SECTOR}**. Is this correct? (yes / or type the correct company name or ticker)"

- If user replies "yes", "y", "correct", "是", "对" etc. → proceed to Step 4
- If user types something else → treat it as a new search query, go back to Step 2
- Repeat until confirmed

### Step 4: Ask Report Type

Output as text and wait for reply:

"Report type? (daily / weekly, default: daily)"

- If user replies "weekly" or "w" → set run_mode to weekly
- Any other reply (including empty/enter) → default to daily

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

If `workspaces/{TICKER}/` already exists:

1. Determine today's trading date (smart trading day logic: pre-market or weekend → use last trading day)
2. Check if a final report exists: look for `workspaces/{TICKER}/final/{date}/daily_report.md` or `weekly_report.md` (depends on `run_mode` in the existing `status.json`)

**Case A: Today's report already exists** — ask: "Today's report for {TICKER} ({date}) already exists. Regenerate? (yes / no, default: no)"
- If no → display existing report location and exit
- If yes → reset status.json and continue

**Case B: No report for today** — auto-reset: set `stages_completed: []`, `stage: "initialized"`, `run_date: "{date}"`. Continue with workspace creation.

### Step 8: Create Directory Tree

```bash
mkdir -p workspaces/{TICKER}/{profile,raw,normalized,quant,discussion/{analyst_memos,debate/{round_1,round_2}},drafts,reviews,decision,final,exports/{pdf,web},eval} workspaces/shared/market_context/{raw,normalized,indicators}
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

Starting pipeline...
```

After displaying the summary, immediately invoke `/mm:run workspaces/{TICKER}` to auto-launch the pipeline. The user does not need to type it separately. The pipeline runs 14 stages: stages 1-12 execute autonomously, stage 13 (`user_review`) pauses to collect user feedback on the report, then stage 14 (`reflect`) runs eval and memory.

**Note:** This skill's behavior must match `plugin/commands/init.md` exactly. If the init command flow changes, update both files.

## Quality Rules

- Always verify company via web search — never guess
- Present clear "Company Name (TICKER) — Exchange" for confirmation
- Existing workspace: check for today's report before regenerating (see Step 7)
- File content language follows config `language` field (en or ch). JSON keys always English.

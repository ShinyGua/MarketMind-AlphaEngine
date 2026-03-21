# /mm:init — Initialize a new company research workspace

## Description

Interactive setup for a new equity research project. Asks which company to analyze, verifies via web search, creates the workspace, and automatically launches the pipeline.

## Arguments

Optional: `$ARGUMENTS[0]` = company name or ticker (skips the first question if provided).

## Steps

### 1. Ask for Company

If `$ARGUMENTS[0]` is provided, use it. Otherwise, output this text and wait for the user to reply in chat:

```
What company or stock would you like to analyze?

Enter a ticker (e.g., NVDA) or company name (e.g., NVIDIA):
```

Wait for the user's chat reply.

### 2. Web Search to Verify

Use **WebSearch** to look up the company:
- If input looks like a ticker (1-5 uppercase letters): search `"{INPUT} stock company name exchange sector"`
- If input looks like a name: search `"{INPUT} stock ticker symbol exchange"`

Extract: full company name, ticker symbol, exchange, sector/industry.

### 3. Confirm with User

Output the result as text and wait for the user's chat reply:

```
I found: **{COMPANY_NAME} ({TICKER}) — {EXCHANGE}, {SECTOR}**

Is this correct? (yes / or type the correct company name or ticker)
```

- If user replies "yes", "y", "correct", etc. → proceed to Step 4
- If user types something else → treat it as a new search query, go back to Step 2
- Repeat until confirmed

### 4. Ask Report Type

Output as text and wait for reply:

```
Report type? (daily / weekly, default: daily)
```

- If user replies "weekly" or "w" → set run_mode to weekly
- Any other reply (including empty/enter) → default to daily

### 5. Enrich via yfinance (Optional)

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

If yfinance fails, use web search results — they are sufficient.

### 6. Determine Market Profile

Use the exchange information from Step 2 (WebSearch) and Step 5 (yfinance) to determine the market profile. If unclear from those results, use **WebSearch** to search `"{TICKER} stock exchange listing market"` and determine which market it belongs to.

Map to one of the allowed values — **must be exactly one of**: `US`, `HK`, `CN`, `JP`, `UK`, `EU`

Do NOT hardcode or guess. Always verify via search results. Common mappings:
- NASDAQ, NYSE, AMEX, CBOE → `US`
- HKEX, HKG, SEHK → `HK`
- SSE, SZSE, Shanghai, Shenzhen → `CN`
- TSE, JPX, Tokyo → `JP`
- LSE, London → `UK`
- XETRA, Euronext, Frankfurt, Paris, Amsterdam → `EU`

If the exchange doesn't match any of these (e.g., ASX, TSX, BSE), default to the closest match and note it in the config as a comment.

### 7. Check Existing Workspace

If `workspaces/{TICKER}/` already exists, output:

```
A workspace for {TICKER} already exists. Overwrite or resume? (overwrite / resume, default: resume)
```

Wait for reply. If overwrite → delete and recreate. If resume → keep existing files.

### 8. Create Directory Tree

Create the undated skeleton. Date subdirectories are created by the orchestrator at runtime.

```bash
MM_ROOT=$(pwd)
mkdir -p "$MM_ROOT/workspaces/{TICKER}"/{profile,raw,normalized,quant,discussion,drafts,reviews,decision,final,exports} "$MM_ROOT/workspaces/shared/market_context"
```

### 9. Generate config.yaml

Read base config from project root: prefer `config.yaml` if it exists, otherwise `config.example.yaml`.

Create `workspaces/{TICKER}/config.yaml` with:
- `company.ticker` → confirmed ticker (uppercase)
- `company.name` → confirmed company name
- `company.exchange` → resolved exchange
- `company.market_profile` → inferred market
- `company.sector_profile` → resolved sector (lowercase, underscores)
- `run_mode` → user's choice (daily/weekly)

Keep all other values as defaults.

### 10. Initialize status.json

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

### 11. Display Summary

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

### 12. Auto-launch Pipeline

Immediately after workspace creation, invoke the pipeline by calling:

```
Skill tool: /mm:run
Arguments: workspaces/{TICKER}
```

This starts the autonomous pipeline — the user does not need to type `/mm:run` separately. The pipeline will run all 12 stages continuously via ralph-loop.

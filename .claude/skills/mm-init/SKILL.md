---
name: mm-init
description: Interactive workspace initialization — asks user for stock, verifies via web search, creates workspace with config
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-standard
allowed-tools: Read, Write, Bash, Glob, Grep, WebSearch, Skill
---

# Role: Research Project Initializer

## Mission

Guide the user through setting up a new company research workspace. Ask which stock they want to analyze, verify the company identity via web search, then create the workspace directory and config file.

## Process

### Step 0: Determine Language

**You MUST complete this step BEFORE outputting any text to the user. Do NOT emit any prompt until you know the language setting.**

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

### Step 4b: Investor Profile (三问 — the cognition layer)

The pipeline writes for a specific investor, not a generic reader. Ask three
questions (in the config language), one at a time; each has a default so the
user can just press enter.

**Q1 — Horizon (周期):**

- en: "Your holding horizon for this name? (short: days–weeks / swing: 1–3 months / long: 6 months+, default: swing)"
- ch: "这只票你打算做什么周期？（short 短线：数日至数周 / swing 波段：1–3个月 / long 中长线：6个月以上，默认：swing）"

Map replies (`短线`/`s`/`short` → `short`; `波段`/`swing` → `swing`; `长线`/`l`/`long` → `long`) → `strategy.horizon`.

**Q2 — Edge hypothesis (选股框架与胜率假设):**

- en: "In one or two sentences: what is your stock-picking framework, and what TYPE of stock has given you the highest win rate? (free text; enter to skip)"
- ch: "用一两句话说说：你的选股框架是什么？哪种类型的股票你的胜率最高？（自由文本；直接回车可跳过）"

Store verbatim → `strategy.edge_hypothesis` (empty string if skipped — do NOT invent one).

**Q3 — Position state (持仓状态):**

- en: "Current position in this name? (none / holding — include cost basis if holding / adding, default: none)"
- ch: "目前持仓状态？（none 未持有 / holding 已持有——请附上成本价 / adding 考虑加仓，默认：none）"

Map → `strategy.position_state` (`none` | `holding` | `adding`) and, when given, `strategy.cost_basis` (number in the listing currency).

These answers change what the analysts weigh (a swing question is not answered
with a 3-year DCF argument) — never skip the questions themselves, only the
user may skip answering.

### Step 5: Enrich Company Details (Optional)

**Yahoo/yfinance is NOT used anywhere in this pipeline.** For a US name, get the
live quote and exchange from the NASDAQ API:

```bash
.venv/bin/python3 scripts/nasdaq_fetch.py TICKER
```

Sector, industry, currency and country come from the Step 2 WebSearch results,
which are sufficient on their own. If neither source resolves a field, leave it
empty rather than guessing — the resolver stage fills the profile properly.

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
- `strategy.horizon` / `strategy.edge_hypothesis` / `strategy.position_state` (+ `strategy.cost_basis` when given) → from Step 4b

Keep all other values as defaults.

**Also write `workspaces/{TICKER}/profile/investor_profile.json`** (undated —
bundled into `shared_context.investor` by the context builder):

```json
{
  "horizon": "short|swing|long",
  "edge_hypothesis": "<the user's verbatim answer, or \"\">",
  "position_state": "none|holding|adding",
  "cost_basis": null,
  "captured_at": "<YYYY-MM-DD>"
}
```

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

**CRITICAL: You MUST use the Skill tool to invoke `/mm:run`. This is the ONLY allowed way to start the pipeline.**

Call the Skill tool with exactly these parameters:

- skill: `"mm:run"`
- args: `"workspaces/{TICKER}"`

**Rules:**
- Do NOT call `Skill("mm:mm-orchestrator")` or any other skill name — only `"mm:run"`
- Do NOT execute the orchestrator protocol yourself or bypass `/mm:run`
- Do NOT skip this step or attempt to "run the pipeline directly"

`/mm:run` handles progress monitor setup, ownership coordination, and pipeline execution. Bypassing it causes the progress checklist to not appear.

The user does not need to type `/mm:run` separately. The pipeline runs 15 stages: stages 1-13 execute autonomously, stage 14 (`user_review`) pauses to collect user feedback on the report, then stage 15 (`reflect`) runs eval and memory.

**Note:** This skill's behavior must match `plugin/commands/init.md` exactly. If the init command flow changes, update both files.

## Quality Rules

- Always verify company via web search — never guess
- Present clear "Company Name (TICKER) — Exchange" for confirmation
- Existing workspace: check for today's report before regenerating (see Step 7)
- File content language follows config `language` field (en or ch). JSON keys always English.

---
name: mm-company-resolver
description: Resolves ticker to full company profile, peer set, and market context link
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-standard
allowed-tools: Read, Write, Bash, Glob, Grep, WebSearch, mcp__market-data__get_company_info
---

# Role: Company Profile Resolver

## MCP Tools Available

This skill can use the **market-data** MCP server for company info fetching. Prefer MCP tools when available; fall back to inline Python if not.

- `mcp__market-data__get_company_info` — fetch company profile from yfinance

## Mission

Resolve a company's full profile from its ticker. Build peer set and market context link files that downstream skills depend on.

Workspace path: $ARGUMENTS[0]

## Inputs

- `{workspace}/config.yaml` or `{workspace}/resolved_config.json`

## Process

### 1. Resolve Company Info

Read ticker from config. Use Bash to query yfinance:

```bash
python3 -c "
import yfinance as yf, json
t = yf.Ticker('TICKER')
info = t.info
print(json.dumps({
    'ticker': 'TICKER',
    'name': info.get('longName', ''),
    'exchange': info.get('exchange', ''),
    'sector': info.get('sector', ''),
    'industry': info.get('industry', ''),
    'market_cap': info.get('marketCap', 0),
    'currency': info.get('currency', 'USD'),
    'country': info.get('country', ''),
    'website': info.get('website', ''),
    'description': (info.get('longBusinessSummary', '') or '')[:500]
}, indent=2))
"
```

If yfinance fails, use WebSearch: search `"{TICKER} company profile sector exchange"`.

### 2. Determine Indices

Based on `market_profile`:
- US → primary: SPY, secondary: [QQQ, sector ETF]
- HK → primary: ^HSI, secondary: [HSTECH]
- CN → primary: 000300.SS
- JP → primary: ^N225

US sector ETF mapping: semiconductors→SOXX, technology→XLK, energy→XLE, healthcare→XLV, financials→XLF, consumer→XLY, industrials→XLI.

### 3. Build Peer Set

Use WebSearch: `"{TICKER} competitors peer companies {industry}"`. Extract 3–5 peer tickers.

### 4. Create Market Context Link

```json
{
  "primary_index": "SPY",
  "secondary_indices": ["QQQ", "SOXX"],
  "macro_assets": ["GLD", "USO", "BTC-USD", "^VIX"],
  "market_context_version": "YYYY-MM-DD"
}
```

## Output

- `{workspace}/profile/company_profile.json`
- `{workspace}/profile/peer_set.json`
- `{workspace}/profile/market_context_link.json`

## Quality Rules

- All output must be valid JSON
- If yfinance fails, fall back to WebSearch — never leave profile empty
- Peer set must not include the company itself
- Include at least 3 peers

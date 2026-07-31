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
t = yf.Ticker('YF_SYMBOL')   # exchange-suffixed: '300685.SZ', '0941.HK', 'MU'
info = t.info
print(json.dumps({
    'ticker': 'TICKER',
    'yf_ticker': 'YF_SYMBOL',
    'name': info.get('longName', ''),
    'exchange': info.get('exchange', ''),
    'sector': info.get('sector', ''),
    'industry': info.get('industry', ''),
    'market_cap': info.get('marketCap', 0),
    'shares_outstanding': info.get('sharesOutstanding', 0),
    'float_shares': info.get('floatShares', 0),
    'currency': info.get('currency', 'USD'),
    'country': info.get('country', ''),
    'website': info.get('website', ''),
    'description': (info.get('longBusinessSummary', '') or '')[:500]
}, indent=2))
"
```

**`yf_ticker` is the canonical yfinance-symbol key** — always emit it, always
exchange-suffixed for non-US names (CN → `.SS`/`.SZ`, HK → `.HK`). Deterministic
scripts resolve it via `contracts.resolve_yf_symbol()`; do not use the historical
spellings `yf_symbol`/`yfinance_symbol` in new profiles.

`shares_outstanding` and `float_shares` are required when yfinance provides them —
the chip-structure layer needs float to compute turnover (换手率). If missing,
emit the key with `null` so downstream can record it in `inputs_missing`.

Also emit **`cap_tier`** — market-cap tier in the company's own market, using
`market_cap` in local currency. Thresholds (from config `cap_tiers`, defaults):

| market_profile | mega | large | mid | small | micro |
|---|---|---|---|---|---|
| US (USD) | ≥200B | ≥10B | ≥2B | ≥300M | <300M |
| CN (CNY) | ≥500B | ≥80B | ≥15B | ≥4B | <4B |
| HK (HKD) | ≥500B | ≥80B | ≥15B | ≥4B | <4B |

(Other markets: use the US bands on the USD-converted cap.) The tier drives the
cap-tier playbook downstream — how capital treats the stock's story differs
fundamentally by size, so record the tier, don't leave it to be re-derived.

If yfinance fails, use WebSearch: search `"{TICKER} company profile sector exchange"`.

### 2. Determine Indices

Based on `market_profile`:
- US → primary: SPY, secondary: [QQQ, sector ETF]
- HK → primary: ^HSI, secondary: [^HSTECH]
- CN → primary: 000001.SS (Shanghai Composite), secondary: [000300.SS (CSI 300)]
- JP → primary: ^N225

US sector ETF mapping: semiconductors→SOXX, technology→XLK, energy→XLE, healthcare→XLV, financials→XLF, consumer→XLY, industrials→XLI.

### 3. Build Peer Cohort (5–10 names, product-differentiated)

The cohort exists so a human can *watch how each same-type stock actually trades* —
some move with the sector, some walk their own path — and so the analysts can ask
why. A list of "5 closest by market cap in the same sector" defeats the purpose.

Use WebSearch (`"{TICKER} competitors peer companies {industry}"`, plus follow-up
searches per product line) and select **5–10 peers** that together:

- cover the **different product niches / value-chain positions** inside the same
  sector — not ten copies of the closest competitor;
- span at least two `cap_tier` levels when the sector has them (capital plays a
  large-cap story and a small-cap story very differently);
- are tradeable tickers with yfinance price history (verify each with a quick
  history call; drop dead tickers and say so in `notes`).

Write `peer_set.json` with **exactly this schema** (fields required per peer):

```json
{
  "ticker": "<TARGET>",
  "sector_profile": "<from config>",
  "selection_method": "<how the cohort was built>",
  "resolved_at": "YYYY-MM-DD",
  "peers": [
    {
      "ticker": "300748.SZ",
      "name": "金力永磁",
      "name_en": "JL Mag Rare-Earth",
      "exchange": "SZSE",
      "industry": "<industry>",
      "product_niche": "<specific product line / value-chain position>",
      "differentiation": "<how its product mix differs from the target's>",
      "market_cap": 36975837184,
      "cap_tier": "large",
      "float_shares": null,
      "currency": "CNY",
      "rationale": "<why it belongs in the cohort>"
    }
  ],
  "notes": "<dropped tickers, unlisted majors (e.g. Samsung for MU), coverage gaps>"
}
```

`product_niche` and `differentiation` are the point of the exercise: two stocks
with identical headline business routinely walk completely different price paths,
and the product-level difference is usually where the divergence lives.

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
- `company_profile.json` must include `yf_ticker` (canonical, exchange-suffixed), `shares_outstanding`, `float_shares`, and `cap_tier`
- Peer set must not include the company itself
- Target 5–10 peers; never fewer than 4 without an explanation in `notes`
- Every peer entry must fill `product_niche` and `differentiation` — a bare sector label is not a rationale

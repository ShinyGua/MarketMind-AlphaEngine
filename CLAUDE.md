# MarketMind-AlphaEngine

Automated equity research system that generates daily and weekly stock analysis reports with BUY / HOLD / SELL decisions.

## System Identity

You are MarketMind-AlphaEngine, a multi-agent research pipeline that:
- Collects market, sector, and company data from free public APIs
- Runs lightweight quantitative analysis (technical indicators, relative strength)
- Conducts structured multi-analyst discussion to form a thesis
- Writes institutional-quality research reports
- Reviews reports against quality thresholds with iterative revision
- Produces a final investment decision with evidence-backed rationale

All output is in English. Internal schemas, file names, and code use English.

## Python Environment (Mandatory)

**All Python calls MUST use `.venv/bin/python3`** (relative to project root). Never use bare `python3`.

The system `python3` may lack required packages (pyyaml, yfinance, pandas, ta, fpdf2). The `.venv/` directory contains all dependencies.

Setup: `source setup.sh` (creates `.venv/` with all dependencies).

---

## Getting Started

### Launch with Plugin

Start Claude Code with the MarketMind plugin:

```bash
claude --plugin-dir "$MM_ROOT/plugin"
```

### Initialize a New Company Research

```
/mm:init
```

The system will ask which company to analyze, verify via web search, and create `workspaces/{TICKER}/`.

### Run the Full Pipeline

```
/mm:run workspaces/NVDA
```

This launches the autonomous pipeline via ralph-loop — it runs all 12 stages continuously without stopping, tracking progress via TodoWrite. If interrupted, re-run the same command to resume.

### Check Status

```
/mm:status
```

Shows all workspaces and their pipeline progress.

---

## Pipeline Stages

```
resolve_config → init_workspace → collect(parallel) → normalize
    → quant → discuss → draft → review_loop → decide → export
```

### Stage 0: Resolve Config
- Read `workspaces/{TICKER}/config.yaml`
- Merge with `config.example.yaml` defaults
- Validate required fields
- Write `resolved_config.json` to workspace

### Stage 1: Initialize Workspace
- Create directory tree under `workspaces/{TICKER}/`
- Resolve company profile (ticker, name, exchange, sector, indices)
- Build peer set
- Link shared market context

### Stage 2: Parallel Data Collection
Run these desks in parallel:
- **mm-market-desk**: Macro headlines, index data, macro asset prices (yfinance, FRED)
- **mm-company-desk**: Company news, SEC filings, catalyst calendar (NewsAPI, EDGAR)
- **mm-sector-desk**: Sector news, peer price data (NewsAPI, yfinance)

### Stage 3: Normalize
- Convert raw data into evidence cards (JSON)
- Build time-series tables
- Deduplicate overlapping news items

### Stage 4: Quant Snapshot
- **mm-quant-analyst** computes technical indicators via Python/pandas
- RSI(14), MACD(12,26,9), SMA(20,50), EMA(12,26), ATR(14)
- Return windows: 1d, 5d, 1m, 3m
- Relative strength vs index, sector, peers
- Output: `quant/technical_indicators.csv`, `quant/quant_summary.json`

### Stage 5: Multi-Analyst Discussion (Debate Loop)

Follows a structured debate protocol inspired by multi-agent collaboration:

**Phase 1 — Independent Memos (parallel)**
All 3 analysts run in parallel. Each reads evidence cards, quant summary, and company profile, but NOT each other's work:
- **mm-market-analyst** → `discussion/analyst_memos/market_analyst.md`
- **mm-company-analyst** → `discussion/analyst_memos/company_analyst.md`
- **mm-risk-analyst** → `discussion/analyst_memos/risk_analyst.md`

Each memo must contain: core thesis, 3–5 supporting points, biggest uncertainty, time horizon judgment.

**Phase 2 — Moderator Quick Scan (selective mode)**
The moderator reads all memos and identifies the top disagreements. Assigns targeted critique pairs — only analysts who meaningfully disagree debate each other. Writes `debate_assignments.json`.

**Phase 3 — Selective Cross-Critique**
Only assigned pairs write critiques (2-3 pairs instead of full N×N). Each critic writes one targeted critique of their assigned opponent. This saves 50-90% of tokens as analyst count scales.

Config: `debate_mode: selective` (default) or `full` (N×N cross, for thoroughness)

**Phase 3 — Synthesis**
- **mm-discussion-moderator** reads all memos + all critique files
- Produces:
  - `discussion/thesis_map.json` — consensus, disagreements, bull/bear cases, key risks, unsupported claims, writer guidance
  - `discussion/debate_summary.md` — human-readable summary of where analysts agreed and disagreed, and why

### Stage 6: Draft Report
- **mm-report-writer** generates daily or weekly report from:
  - Evidence cards, quant summary, thesis map, company profile

### Stage 7: Review Loop
- **mm-report-reviewer** scores on: factuality, evidence_coverage, decision_quality
- If score < threshold → generate `revision_brief.json` → writer rewrites targeted sections
- Max loops defined in config (default: 3)

### Stage 8: Investment Decision
- **mm-decision-maker** produces `final_decision.json`:
  - BUY / HOLD / SELL label
  - Confidence score
  - Top reasons, key risks, disconfirming signals

### Stage 9: Export
- Write final markdown report to `final/`
- Write structured JSON report to `final/`
- (Future: PDF and web PPT export)

---

## Workspace Structure

All time-sensitive data is organized under `{YYYY-MM-DD}/` date folders to preserve multi-day history. Static reference data (profile, config) stays undated.

```
workspaces/
  shared/
    market_context/
      {YYYY-MM-DD}/              # Date-stamped shared macro data
        raw/
        normalized/
        indicators/

  {TICKER}/
    config.yaml                  # Undated — company config
    resolved_config.json         # Undated — merged config
    status.json                  # Undated — current run state (includes run_date)

    profile/                     # Undated — static company reference
      company_profile.json
      peer_set.json
      market_context_link.json

    raw/{YYYY-MM-DD}/            # Date-stamped raw data
      news/
      filings/
      prices/
      ownership/
      calendar/

    normalized/{YYYY-MM-DD}/     # Date-stamped evidence cards
      evidence_cards/
      time_series/
      tables/

    quant/{YYYY-MM-DD}/          # Date-stamped quant analysis
      technical_indicators.csv
      relative_strength.csv
      quant_summary.json

    discussion/{YYYY-MM-DD}/     # Date-stamped analyst discussion
      analyst_memos/
      debate/round_1/
      debate/round_2/
      thesis_map.json
      debate_summary.md

    drafts/{YYYY-MM-DD}/         # Date-stamped drafts
      daily_v1.md

    reviews/{YYYY-MM-DD}/        # Date-stamped reviews
      final_reviews/
      revision_briefs/
      score_history.json

    decision/{YYYY-MM-DD}/       # Date-stamped decision
      final_decision.json

    final/{YYYY-MM-DD}/          # Date-stamped final output
      daily_report.md
      daily_report.json

    exports/{YYYY-MM-DD}/        # Date-stamped exports
      pdf/
      web/
```

---

## Status Tracking

Each workspace maintains `status.json`:

```json
{
  "stage": "collect",
  "run_date": "2026-03-21",
  "started_at": "2026-03-21T10:00:00Z",
  "updated_at": "2026-03-21T10:05:00Z",
  "ticker": "NVDA",
  "run_mode": "daily",
  "stages_completed": ["resolve_config", "init_workspace"],
  "current_review_loop": 0,
  "errors": []
}
```

The `run_date` field (YYYY-MM-DD) determines which date subdirectory all stages read from and write to. The orchestrator sets this at pipeline start and passes it to every skill.

---

## Config System

### Merge Order
1. `config.example.yaml` (project root — hardcoded defaults, always present)
2. `config.yaml` (project root — user's local config with real API keys; **used if exists**, not committed to git)
3. `workspaces/{TICKER}/config.yaml` (company-specific — created by `/mm-init`)
4. Runtime overrides (passed via arguments)

Later values override earlier values. The system checks for `config.yaml` first; if it does not exist, falls back to `config.example.yaml`.

### Config Reference
- `config.example.yaml` — template with all available knobs and placeholder values
- `config.yaml` — your local copy with real API keys and preferences (add to `.gitignore`)

---

## Data Sources (V1 — Free Tier)

| Source | API | Used For |
|--------|-----|----------|
| yfinance | Free, no key | Stock prices, index data, peer prices, macro assets |
| NewsAPI | Free tier, key required | Market news, sector news, company news |
| SEC EDGAR | Free, no key | Company filings (10-K, 10-Q, 8-K), insider transactions |
| FRED | Free, key required | Macro indicators (US10Y, USD index, VIX) |

API keys are configured via environment variables specified in `config.yaml` under `data_sources.*.api_key_env`.

---

## Artifact Contract

All inter-stage data exchange uses JSON files written to the workspace. Key schemas:

- **Evidence Card**: `normalized/evidence_cards/*.json` — standardized source items with scores
- **Quant Summary**: `quant/quant_summary.json` — technical indicators and flags
- **Thesis Map**: `discussion/thesis_map.json` — consensus, disagreements, bull/bear cases
- **Review Output**: `reviews/final_reviews/*.json` — pass/fail, dimension scores, rewrite actions
- **Final Decision**: `decision/final_decision.json` — BUY/HOLD/SELL with evidence

See `market_report_agent_codex_spec.md` sections 11.1–11.6 for complete schema definitions.

---

## Agent Tiers

| Tier | Model | Used For |
|------|-------|----------|
| mm-heavy | claude-opus-4-6 | Orchestration, discussion moderation, review, decision |
| mm-standard | claude-opus-4-6 | Company resolution, report writing, analyst memos |
| mm-light | claude-sonnet-4-6 | Data collection desks, quant computation |

---

## Skill Inventory

| Skill | Tier | User-Invocable | Purpose |
|-------|------|----------------|---------|
| mm-init | mm-standard | Yes | Interactive workspace + config creation |
| mm-orchestrator | mm-heavy | Yes | Pipeline driver — dispatches all stages |
| mm-company-resolver | mm-standard | No | Ticker → company profile + peers |
| mm-market-desk | mm-light | No | Macro + market data collection |
| mm-company-desk | mm-light | No | Company news + filings + catalysts |
| mm-sector-desk | mm-light | No | Sector news + peer data |
| mm-quant-analyst | mm-light | No | Technical indicator computation |
| mm-market-analyst | mm-standard | No | Market environment analysis memo |
| mm-company-analyst | mm-standard | No | Company fundamentals analysis memo |
| mm-risk-analyst | mm-standard | No | Risk identification memo |
| mm-discussion-moderator | mm-heavy | No | Synthesize analyst memos → thesis_map |
| mm-report-writer | mm-standard | No | Generate report draft |
| mm-report-reviewer | mm-heavy | No | Multi-dimensional scoring + revision |
| mm-decision-maker | mm-heavy | No | Final BUY/HOLD/SELL decision |

---

## Quality Gate

The review stage enforces these thresholds (configurable in `config.yaml`):

| Dimension | Default Minimum |
|-----------|----------------|
| Overall score | 8.0 |
| Factuality | 9.0 |

Blocker policy: `hard_fail` — any blocker (ungrounded claim, wrong time window, data contradicts text) fails the draft immediately.

Max revision loops: 3 (configurable via `review.max_revision_loops`).

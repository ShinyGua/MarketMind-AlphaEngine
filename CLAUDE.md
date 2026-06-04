# MarketMind-AlphaEngine

Automated equity research system that generates daily and weekly stock analysis reports with BUY / HOLD / SELL decisions.

## System Identity

You are MarketMind-AlphaEngine, a multi-agent research pipeline that:
- Collects market, sector, and company data from free public APIs
- Runs lightweight quantitative analysis (technical indicators, relative strength)
- Computes a quantitative valuation (scenario DCF, peer comps, margin of safety)
- Conducts structured multi-analyst discussion to form a thesis
- Writes institutional-quality research reports
- Reviews reports against quality thresholds with iterative revision
- Produces a final investment decision with evidence-backed rationale

Output language is determined by the `language` field in config (`en` or `ch`). When `ch`, all user-facing text (analyst memos, reports, PDF, chart labels, `/mm:init` prompts) must be in Chinese. Internal schemas, JSON keys, file names, directory paths, and code are always in English. BUY/HOLD/SELL labels remain English regardless of language setting.

## Python Environment (Mandatory)

**All Python calls MUST use `.venv/bin/python3`** (relative to project root). Never use bare `python3`.

The system `python3` may lack required packages (pyyaml, yfinance, pandas, ta, fpdf2, weasyprint, jinja2). The `.venv/` directory contains all dependencies. (PDF export also needs WeasyPrint's native libs — Pango/cairo/GDK-PixBuf — and a CJK font for Chinese reports.)

Setup: `source setup.sh` (creates `.venv/` with all dependencies).

---

## Getting Started

### Launch with Plugin

Start Claude Code with the MarketMind plugin:

```bash
claude --plugin-dir "$MM_ROOT/plugin" --dangerously-skip-permissions
```

### Initialize a New Company Research

```text
/mm:init
```

The system will ask which company to analyze, verify via web search, and create `workspaces/{TICKER}/`.

### Run the Full Pipeline

```text
/mm:run workspaces/NVDA
```

This launches the autonomous pipeline — it runs all 15 stages continuously, tracking progress via TodoWrite. The `user_review` stage pauses to collect user feedback; all other stages run autonomously. The final stage (reflect) runs code-based graders, produces a release gate verdict, and writes long-term memories including user feedback. If interrupted, re-run the same command to resume.

### Check Status

```text
/mm:status
```

Shows all workspaces and their pipeline progress.

---

## Pipeline Stages

```text
resolve_config → init_workspace → collect(parallel) → normalize
    → quant → valuation → discuss → draft → review_loop → decide → export → reflect
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
Under the headless Codex driver this stage first runs `scripts/check_data_sources.py`, a non-critical preflight that records key presence (never values), DNS reachability, and per-source status (`auth_ok`/`dns_failed`/`auth_failed`/`rate_limited`/`no_key`) to `raw/{date}/diagnostics/data_sources.json` — so a fallback-heavy run self-explains. (Live data requires network egress: Codex's `workspace-write` sandbox needs `[sandbox_workspace_write] network_access = true`; the driver sets it per stage.) Then run these desks in parallel:

- **mm-market-desk**: Macro headlines, index data, macro asset prices (yfinance, FRED)
- **mm-company-desk**: Company news, SEC filings, catalyst calendar (NewsAPI, EDGAR)
- **mm-sector-desk**: Sector news, peer price data (NewsAPI, yfinance)
- **mm-web-research**: Web-sourced news with provenance (Claude WebSearch/WebFetch); for US names pulls from **NASDAQ** (`api.nasdaq.com` first, `nasdaq.com` pages as fallback)

**Source hierarchy** (institutional/MCP > NewsAPI > NASDAQ for US > general web search): the desks own the top tiers, mm-web-research owns the lower tiers and captures source URL/date/excerpt per item. The normalize stage deduplicates cards across all four collectors (canonical URL + title) before building the evidence digest.

### Stage 3: Normalize
- Convert raw data into evidence cards (JSON)
- Build time-series tables
- Deduplicate overlapping news items

### Stage 4: Quant Snapshot
- **mm-quant-analyst** computes technical indicators via Python/pandas
- RSI(14), MACD(12,26,9), SMA(20,50), EMA(12,26), ATR(14)
- Return windows: 1d, 5d, 1m, 3m
- Relative strength vs index, sector, peers
- Output: `quant/{date}/technical_indicators.csv`, `quant/{date}/quant_summary.json`

### Stage 5: Valuation (Scenario DCF + Comps)
- **mm-valuation-engine** runs the formula-first engine in `valuation/` (`dcf.py`, `comps.py`, `run_valuation.py`)
- Inputs: yfinance fundamentals collected by the company desk (`raw/{date}/fundamentals/`), via the `get_fundamentals` MCP tool
- Computes a bull/base/bear **DCF** (CAPM WACC, Gordon terminal value, odd-dimension WACC×terminal-growth sensitivity grid whose center cell equals the base case), peer **comps** with quartile benchmarking, and a **margin of safety** vs the current price → verdict cheap/fair/expensive
- Free-tier and self-degrading: ETFs/funds → `applicable: false`; sparse data → `confidence: "low"` with an `inputs_missing` list (never aborts the pipeline)
- Output: `valuation/{date}/valuation_summary.json`, `valuation/{date}/comps.csv`, `valuation/{date}/dcf_sensitivity.csv`
- Consumed downstream by mm-valuation-analyst, mm-decision-maker (margin of safety → conviction), and mm-report-writer (Valuation section)

### Stage 6: Multi-Analyst Discussion (Debate Loop)

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
  - `discussion/{date}/thesis_map.json` — consensus, disagreements, bull/bear cases, key risks, unsupported claims, writer guidance
  - `discussion/{date}/debate_summary.md` — human-readable summary of where analysts agreed and disagreed, and why

### Stage 7: Draft Report
- **mm-report-writer** generates daily or weekly report from:
  - Evidence cards, quant summary, thesis map, company profile

### Stage 8: Review Loop
- **mm-report-reviewer** scores on: factuality, evidence_coverage, decision_quality
- If score < threshold → generate `revision_brief.json` → writer rewrites targeted sections
- Max loops defined in config (default: 3)

### Stage 9: Investment Decision (Panel Debate Loop)

A multi-round **decision panel** (default; legacy single-shot when
`decision.panel.enabled: false`). Each round:
1. **Ballots (parallel)** — every analyst role (`discussion.analyst_roles`) casts a
   ballot via **mm-decision-panelist**: a vote (BUY/HOLD/SELL), a **conviction
   self-rating** (0–1), and a hedge **risk overlay** (none/hedge/trim/stop) →
   `decision/{date}/panel/round_{N}/{role}_ballot.json`.
2. **Chair tally** — **mm-decision-maker** (`tally` mode) tallies votes and surfaces
   retained dissent → `panel_summary_round_{N}.json`.
3. **Convergence (deterministic)** — `eval/graders/panel_convergence_grader.py`
   computes a conviction-weighted convergence score and decides iterate-vs-exit;
   it **auto-exits at `max_rounds`** (hard cap) → `convergence_round_{N}.json`.

After the loop, **mm-decision-maker** (final mode) produces `final_decision.json`:
- BUY / HOLD / SELL label + `risk_overlay` hedge stance
- Confidence score; conviction-weighted panel lean (not head-count)
- Top reasons, key risks, disconfirming signals, and a `panel` block (rounds,
  final tally, convergence score, retained dissent)

Config: `decision.panel` (`enabled`, `min_rounds`, `max_rounds`,
`convergence_threshold`, `overlay_labels`).

### Stage 10: Export
- Write final markdown report to `final/`
- Write structured JSON report to `final/`
- **mm-pdf-exporter** generates the JPM-style PDF: `templates/charts.py` renders annotated SVG charts, then `templates/render_pdf.py` converts the markdown → HTML/CSS → PDF via **WeasyPrint** (committed templates `report.css` + `report.html.j2`; no LaTeX). Page-1 rating box from JSON, embedded charts, styled tables, running headers/footers, bilingual EN/中文.
- Output: `exports/{date}/pdf/report.pdf` (+ `report.html` for debugging, `charts/*.svg`)
- (Future: web PPT export)

### Stage 11: Reflect (Non-Critical)
- Run code-based graders (factuality, evidence coverage, consistency, cost)
- Finalize run log entry (`logs/run_log.jsonl`)
- **mm-memory-writer** extracts and stores episodic/semantic/procedural memories
- If this stage fails, pipeline still counts as complete

---

## Workspace Structure

All time-sensitive data is organized under `{YYYY-MM-DD}/` date folders to preserve multi-day history. Static reference data (profile, config) stays undated.

```text
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
      fundamentals/             # company + peers/ valuation inputs (yfinance)
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

    valuation/{YYYY-MM-DD}/      # Date-stamped valuation (DCF + comps)
      valuation_summary.json
      comps.csv
      dcf_sensitivity.csv

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
      panel/                     # decision-panel debate loop
        round_{N}/{role}_ballot.json
        panel_summary_round_{N}.json
        convergence_round_{N}.json

    final/{YYYY-MM-DD}/          # Date-stamped final output
      {daily_report|weekly_report}.md   # basename depends on run_mode
      {daily_report|weekly_report}.json

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
| NASDAQ | Free, no key (`api.nasdaq.com`, unofficial) | US-name news + quote (mm-web-research); falls back to nasdaq.com pages |
| Web search | Claude WebSearch/WebFetch | Provenance-tagged web news (mm-web-research), any market |

Source hierarchy: institutional/MCP > NewsAPI > NASDAQ (US) > general web search. The NewsAPI key is read from the `NEWSAPI_KEY` environment variable (passed through `.mcp.json`).

API keys are configured via environment variables specified in `config.yaml` under `data_sources.*.api_key_env`.

---

## Artifact Contract

All inter-stage data exchange uses JSON files written to the workspace. Key schemas:

- **Evidence Card**: `normalized/{date}/evidence_cards/*.json` — standardized source items with scores
- **Quant Summary**: `quant/{date}/quant_summary.json` — technical indicators and flags
- **Valuation Summary**: `valuation/{date}/valuation_summary.json` — DCF intrinsic range, margin of safety, verdict, comps, confidence
- **Thesis Map**: `discussion/{date}/thesis_map.json` — consensus, disagreements, bull/bear cases
- **Review Output**: `reviews/{date}/final_reviews/*.json` — pass/fail, dimension scores, rewrite actions
- **Final Decision**: `decision/{date}/final_decision.json` — BUY/HOLD/SELL with evidence, `risk_overlay` hedge stance, and a `panel` block (rounds, vote tally, convergence, retained dissent)
- **Panel Ballot**: `decision/{date}/panel/round_{N}/{role}_ballot.json` — one role's vote + conviction self-rating + hedge overlay for round N

See `market_report_agent_codex_spec.md` sections 11.1–11.6 for complete schema definitions.

---

## Agent Tiers

| Tier | Model | Used For |
|------|-------|----------|
| mm-heavy | claude-opus-4-6 | Orchestration, discussion moderation, review, decision |
| mm-standard | claude-opus-4-6 | Company resolution, report writing, analyst memos |
| mm-light | claude-sonnet-4-6 | Data collection desks, quant + valuation computation |

---

## MCP Server Architecture

Three MCP servers provide standardized tool/resource/prompt interfaces for the pipeline:

### market-data-mcp (`mcp/market_data_server.py`)
Wraps all external data source calls with rate limiting and fallback logic.

| Tool | Source | Purpose |
|------|--------|---------|
| `get_price_history` | yfinance | OHLCV price data for tickers |
| `get_news` | NewsAPI | News articles (falls back to WebSearch) |
| `get_filings` | SEC EDGAR | Company SEC filings |
| `get_macro_series` | FRED | Macro time series data |
| `get_company_info` | yfinance | Company profile (sector, industry, market cap) |
| `get_fundamentals` | yfinance | Valuation ratios + financial statements (for DCF/comps) |
| `get_earnings_calendar` | yfinance | Upcoming earnings dates |

### workspace-mcp (`mcp/workspace_server.py`)
Manages workspace artifact I/O with path-traversal protection.

**Tools**: `write_artifact`, `update_status`, `create_workspace`, `create_date_dirs`
**Resources**: `workspace://{ticker}/{path}` — URI-addressable workspace files (profile, quant, valuation, evidence, thesis_map, decision)
**Prompts**: `pipeline_status_summary`, `workspace_overview`

### memory-mcp (`mcp/memory_server.py`)
Long-term memory storage and retrieval across pipeline runs.

**Tools**: `store_memory`, `search_memory`, `get_entity_timeline`, `update_memory`, `prune_memories`
**Resources**: `memory://index`, `memory://entity/{name}`, `memory://recent`
**Prompts**: `analysis_reflection`, `lesson_extraction`

Configuration: `.mcp.json` at project root registers all three servers for Claude Code.

---

## Memory Layer

Cross-run memory system with three types stored as JSONL under `memory/`:

| Type | Purpose | Lifecycle | Example |
|------|---------|-----------|---------|
| Episodic | Per-run decision + key themes | 1 per run, never expires | "AMD 2026-03-20: HOLD at 0.72, AI uncertainty vs strong relative strength" |
| Semantic | Persistent company/sector beliefs | Evolves: old superseded by new | "EPYC server adoption accelerating since 2023" |
| Procedural | Process learnings from errors | Never expires | "MACD direction errors: always check sign vs signal" |

**Storage**: `memory/{type}/index.jsonl` — append-only, one JSON line per memory unit

**Retrieval**: `memory/retrieval.py` scores memories by `importance x confidence x recency_decay(days)` and returns top-k. Injected before analyst memos (episodic + semantic for ticker/sector), report writing (procedural + recent episodic), and review (procedural only).

**Memory writer**: `mm-memory-writer` skill runs in the final reflect stage to extract memories from completed pipeline runs, including user review feedback from the user-review stage.

---

## Evaluation Layer

Automated evaluation pipeline under `eval/`:

### Code-Based Graders

| Grader | What It Checks |
|--------|---------------|
| `factuality_grader.py` | Report numbers match quant_summary.json + valuation_summary.json |
| `evidence_grader.py` | High-materiality cards (>=0.7) cited in report |
| `consistency_grader.py` | Decision aligns with thesis_map consensus (and panel vote majority when the panel ran) |
| `panel_convergence_grader.py` | Decision-panel ballots converged (conviction-weighted agreement); drives the decide-stage loop exit |
| `valuation_grader.py` | Valuation math is internally consistent (WACC>g, TV band, sensitivity center == base, margin of safety) |
| `cost_tracker.py` | Token/cost estimation per run |

### Run Log

`logs/run_log.jsonl` — append-only, one entry per completed pipeline run. Contains stage timings, review scores, grader results, decision, and cost estimates.

### Metrics

`eval/metrics.py` computes aggregate dashboards: pipeline health, quality trends, decision analytics, cost analytics.

```bash
.venv/bin/python3 eval/metrics.py --ticker AMD --format markdown
```

---

## Context Governance

Token optimization through layered compression:

```text
raw doc → evidence card → evidence_digest → thesis_map → decision capsule
```

Each layer is ~5-10x smaller. Downstream agents read the most compressed form sufficient for their task.

### Existing Patterns
- **Selective debate**: Moderator assigns 2-3 critique pairs instead of N×N
- **Evidence digest**: All cards consolidated into one file
- **Shared context**: quant + profile + peers + catalysts bundled in one file
- **Memo summaries**: Stored in debate_assignments.json, reused in synthesis
- **Targeted revision**: revision_brief specifies sections to rewrite, not full report

### Memory-Aware Context Budget
When memory context is loaded, it supplements (not replaces) current evidence. The retrieval script returns only top-k scored memories to stay within token budget.

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
| mm-web-research | mm-light | No | Web/NASDAQ news collection with source provenance |
| mm-quant-analyst | mm-light | No | Technical indicator computation |
| mm-valuation-engine | mm-light | No | Scenario DCF + comps + margin of safety (runs `valuation/`) |
| mm-market-analyst | mm-standard | No | Market environment analysis memo |
| mm-company-analyst | mm-standard | No | Company fundamentals analysis memo |
| mm-risk-analyst | mm-standard | No | Risk identification memo |
| mm-discussion-moderator | mm-heavy | No | Synthesize analyst memos → thesis_map |
| mm-report-writer | mm-standard | No | Generate report draft |
| mm-report-reviewer | mm-heavy | No | Multi-dimensional scoring + revision |
| mm-decision-panelist | mm-standard | No | Casts one analyst role's decision ballot (vote + conviction + hedge overlay) per panel round |
| mm-decision-maker | mm-heavy | No | Chair: per-round tally + final BUY/HOLD/SELL decision |
| mm-memory-writer | mm-standard | No | Extract and store memories from completed runs |

---

## Quality Gate

The review stage enforces these thresholds (configurable in `config.yaml`):

| Dimension | Default Minimum |
|-----------|----------------|
| Overall score | 8.0 |
| Factuality | 9.0 |

Blocker policy: `hard_fail` — any blocker (ungrounded claim, wrong time window, data contradicts text) fails the draft immediately.

Max revision loops: 3 (configurable via `review.max_revision_loops`).

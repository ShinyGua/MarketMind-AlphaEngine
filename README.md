<p align="center">
  <h1 align="center">MarketMind-AlphaEngine</h1>
  <p align="center">
    <strong>Multi-Agent Equity Research System with Investment Bank Style Reports</strong>
  </p>
  <p align="center">
    Automated daily & weekly stock analysis · Multi-analyst debate · Scenario DCF + comps valuation · BUY/HOLD/SELL decisions · JPM-style PDF reports
  </p>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/version-dev%200.1-orange.svg" alt="Version">
  <img src="https://img.shields.io/badge/platform-Claude%20Code-purple.svg" alt="Platform">
  <img src="https://img.shields.io/badge/python-3.10%2B-green.svg" alt="Python">
</p>

<p align="center">
  <a href="README_CN.md"><strong>中文版</strong></a>
</p>

---

> **IMPORTANT DISCLAIMER**
>
> This is an **experimental research project** that generates investment-bank-style equity research reports using AI agents. The reports, analysis, and BUY/HOLD/SELL recommendations produced by this system are **for educational and research purposes only**. They do **NOT** constitute investment advice, financial guidance, or a recommendation to buy or sell any security.
>
> **Do not make investment decisions based on outputs from this system.** Always consult qualified financial advisors and conduct your own due diligence before making any investment decisions. The creators of this project bear no responsibility for any financial losses incurred from acting on the system's outputs.

---

## What is MarketMind-AlphaEngine?

MarketMind-AlphaEngine is a fully automated equity research pipeline built natively on [Claude Code](https://code.claude.com). It operates as an **autonomous research organization**: collecting market data, running quantitative analysis, conducting structured multi-analyst debates, writing institutional-quality research reports, and producing investment decisions with evidence-backed rationale.

### What Makes It Different?

- **Multi-Agent Debate**: 3-6 analyst agents independently analyze a stock, then a moderator identifies disagreements and assigns targeted cross-critique pairs to produce a balanced thesis through structured debate rather than a single-agent summary
- **Quantitative Valuation Engine**: A formula-first DCF (CAPM WACC, Gordon terminal value, bull/base/bear scenarios, a WACC×terminal-growth sensitivity grid) plus peer comps with quartile benchmarking, producing an intrinsic-value range and a **margin of safety**. The blended fair value's confidence is **derived from its DCF/comps components** (a low-confidence DCF can't make the blend read "high"), and it feeds the BUY/HOLD/SELL call as a **confidence-weighted reference** that informs — but never forces — the decision
- **Investment-Bank-Style PDF**: Generates JPM-style research reports — Page-1 rating box, embedded annotated charts, styled tables, and bilingual (EN/中文) typography — via a deterministic Markdown → HTML/CSS → PDF pipeline (WeasyPrint)
- **Selective Debate**: Moderator-directed cross-critique saves 50-90% of tokens compared with full N×N debate as analyst count scales
- **Decision Panel (vote → converge → exit)**: The decision is reached by a multi-round panel — each analyst role casts a ballot (BUY/HOLD/SELL + a conviction self-rating + a hedge overlay), a deterministic grader scores convergence, and the loop iterates until the panel agrees or hits a hard round cap. The chair then writes the final call, faithful to the conviction-weighted lean with dissent retained
- **Date-Stamped History**: Every run preserves outputs under `{YYYY-MM-DD}/` folders so the same company can be analyzed daily without losing prior research
- **Smart Trading Day Logic**: Automatically determines the correct data cutoff, including pre-market sessions, weekends, and holidays
- **MCP Server Architecture**: 3 Model Context Protocol servers (market-data, mm-workspace, memory) give agents structured tool access to data, files, and persistent memory
- **Long-Term Memory**: Episodic, semantic, and procedural memory layers let the system recall past analyses, learned patterns, and refined procedures across runs
- **Automated Evaluation Pipeline**: Code-based graders score each run along multiple dimensions — including an **advisory decision risk gate** that flags over-confidence (a non-mutating confidence ceiling from weak convergence, retained dissent, low-confidence valuation, or thin evidence) — with a run log and aggregated metrics to track quality over time
- **Free Data Sources**: Works entirely with free APIs (yfinance, NewsAPI free tier, SEC EDGAR, FRED) with WebSearch fallback when API keys are unavailable

---

## Quick Start

### Prerequisites

- [Claude Code](https://code.claude.com) CLI installed — or [Codex CLI](https://developers.openai.com/codex) (see [Run with Codex CLI](#run-with-codex-cli-alternative-to-claude-code))
- [Ralph Loop plugin](https://github.com/anthropics/claude-code) (recommended for long-running execution)
- Python 3.10+
- PDF generation needs WeasyPrint's native libs (Pango, cairo, GDK-PixBuf). On macOS: `brew install pango gdk-pixbuf libffi`; on Debian/Ubuntu: `apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0`. For Chinese (CJK) reports, install a CJK font (e.g. `fonts-wqy-microhei` or Noto Sans CJK). (`weasyprint` itself is installed by `setup.sh`.)

### Setup

```bash
# 1. Clone the repository
git clone git@github.com:ShinyGua/MarketMind-AlphaEngine.git
cd MarketMind-AlphaEngine

# 2. Create the Python environment + pin the repo root
source setup.sh
export MM_ROOT="$(pwd)"

# 3. (Optional) Add your API keys to config.yaml
cp config.example.yaml config.yaml
# Edit config.yaml for provider settings
# API secrets are typically supplied via environment variables

# 4. (Optional) Add API keys — the recommended path is a .env file at the repo
#    root. The market-data MCP server loads it automatically at startup, so no
#    shell export is needed and keys survive across sessions.
cp .env.example .env
# Edit .env and fill in NEWSAPI_KEY / FRED_API_KEY
# (shell exports still work and take precedence; leaving keys unset is allowed —
#  WebSearch fallback covers the gap)

# 5. Launch Claude Code with the plugin (run from the repo root)
claude --plugin-dir "$MM_ROOT/plugin" --dangerously-skip-permissions
```

### NewsAPI Key Setup

If you want higher-quality news coverage than the fallback web search, create a NewsAPI key first:

1. Register at [newsapi.org/register](https://newsapi.org/register)
2. Verify your email and copy your API key from the NewsAPI dashboard/docs examples
3. Put it in your `.env` as `NEWSAPI_KEY` (or export it in your shell)

This project reads the key from the environment variable named in [`config.example.yaml`](config.example.yaml), under:

```yaml
data_sources:
  news:
    api_key_env: NEWSAPI_KEY
```

The recommended place for the value is a `.env` file at the repo root (copy
`.env.example`). The market-data MCP server loads `.env` at startup, so you don't
have to export anything:

```bash
# .env
NEWSAPI_KEY=your_newsapi_key
FRED_API_KEY=your_fred_key
```

`NEWSAPI_API_KEY` is also accepted as an alias for `NEWSAPI_KEY`. A shell
`export NEWSAPI_KEY=...` still works and takes precedence over `.env`.

> **Note:** the MCP server reads `.env` once when it starts, so after editing keys
> restart Claude Code / Codex for them to take effect.

### Run with Codex CLI (alternative to Claude Code)

MarketMind also runs under [Codex CLI](https://developers.openai.com/codex). The `mm-*` skills are exposed as **native Codex skills** via `.agents/skills/` (a symlink to `.claude/skills/`), and `AGENTS.md` is the always-loaded control surface.

```bash
# 1. Register the MCP servers: merge the three [mcp_servers.*] tables from
#    .agents/references/codex-config.toml into ~/.codex/config.toml
# 2. (Optional) put API keys in .env (loaded automatically), then launch Codex
#    FROM THE REPO ROOT (the MCP server command/args paths are repo-relative).
#    A shell export still works as an alternative:
export NEWSAPI_KEY="your_newsapi_key"   # optional; web-search fallback otherwise
codex
```

In Codex, list skills with `/skills`. Only **`mm-init`** and **`mm-orchestrator`** are auto-matched entry points (e.g. *"initialize a workspace for NVDA"*, *"run the MarketMind pipeline for workspaces/NVDA"*). The internal pipeline-stage skills are explicit-only (`$mm-…`) — they run inside the orchestrated pipeline, not standalone. See `AGENTS.md` and `.agents/` for the full Codex profile.

**Recommended — run the full pipeline at Claude-parity depth** with the headless driver, which runs each stage as its own `codex exec` (fresh context per stage, the analog of Claude's `context: fork`) so output matches a Claude run instead of compressing into stubs:

```bash
.venv/bin/python3 scripts/run_codex_pipeline.py workspaces/NVDA
.venv/bin/python3 scripts/run_codex_pipeline.py workspaces/NVDA --dry-run   # print the plan only
```

Deterministic stages (valuation, charts/PDF, dedup, graders) run as committed Python; LLM stages (desks, analysts, writer, …) run as parallel `codex exec` calls; the depth gate redoes any thin memo/report.

> **Live data needs network egress.** Codex's `workspace-write` sandbox blocks
> outbound network by default, so the collection desks silently fall back to
> WebSearch/cache (you'll see `dns_failed` in the diagnostics). The headless driver
> sets `-c sandbox_workspace_write.network_access=true` per stage; for interactive
> `codex`, the `[sandbox_workspace_write] network_access = true` block in
> `.agents/references/codex-config.toml` does the same. The collect stage first runs
> `scripts/check_data_sources.py`, which records key presence (never values), DNS,
> and per-source status (`auth_ok` / `dns_failed` / `auth_failed` / `rate_limited` /
> `no_key`) to `raw/{date}/diagnostics/data_sources.json` — check it when a run looks
> fallback-heavy.

### Usage

```text
/mm:init                          # Create a new company workspace (interactive)
/mm:run workspaces/NVDA           # Run the full 15-stage research pipeline
/mm:status                        # Check all workspace statuses
```

The `/mm:init` flow:

1. Asks which company to analyze (ticker or company name)
2. Runs web verification such as: `NVIDIA Corporation (NVDA) - NASDAQ`
3. Prompts you to confirm or correct the match
4. Creates the workspace and automatically starts the pipeline

---

## Pipeline Architecture

```text
/mm:run workspaces/{TICKER}
```

```text
+== MarketMind /mm:run ================================================+
|                                                                      |
|  Existing company workspace                                          |
|       |                                                              |
|       v                                                              |
|  Resolve config + validate status                                    |
|       |                                                              |
|       v                                                              |
|  Init workspace context                                              |
|       |                                                              |
|       v                                                              |
|  Collect (4 collectors in parallel)                                  |
|       |-- Market desk: macro headlines, indices, macro assets        |
|       |-- Company desk: company news, filings, catalysts             |
|       |-- Sector desk: sector news, peer prices                      |
|       |-- Web research: web/NASDAQ news with source provenance       |
|       |                                                              |
|       v                                                              |
|  Normalize evidence cards + time-series tables                       |
|       |                                                              |
|       v                                                              |
|  Quant snapshot                                                      |
|       | RSI, MACD, SMA, ATR, relative strength                       |
|       v                                                              |
|  Valuation (scenario DCF + comps)                                    |
|       | intrinsic-value range, margin of safety, verdict            |
|       v                                                              |
|  Discussion loop                                                     |
|       |-- Independent analyst memos (parallel)                       |
|       |-- Moderator selects critique pairs                           |
|       |-- Selective debate                                           |
|       |-- Thesis synthesis                                           |
|       |                                                              |
|       v                                                              |
|  Draft institutional report                                          |
|       |                                                              |
|       v                                                              |
|  Review loop                                                         |
|       | pass ------------------------------+                         |
|       | fail -> targeted revision -> draft |                         |
|       v                                (max loops from config)       |
|  Investment decision                                                 |
|       | BUY / HOLD / SELL + confidence + risks                       |
|       v                                                              |
|  Export markdown + JSON + charts + PDF (WeasyPrint)                  |
|       |                                                              |
|       v                                                              |
|  User Review (only stage that pauses for input)                      |
|       | Collect user feedback on the report                          |
|       v                                                              |
|  Reflect                                                             |
|       | Evaluate run quality, update long-term memory,               |
|       | log metrics for continuous improvement                       |
|       v                                                              |
|  Final dated report under workspaces/{TICKER}/final/{YYYY-MM-DD}/    |
|                                                                      |
+----------------------------------------------------------------------+
```

### Stage Details

| Stage | What Happens | Agents |
|-------|--------------|--------|
| **Collect** | Macro, company, and sector data from yfinance, NewsAPI, and EDGAR, plus web/NASDAQ news with source provenance | 4 collectors in parallel |
| **Quant** | RSI, MACD, SMA, ATR, and relative strength calculations in Python | `mm-quant-analyst` |
| **Valuation** | Scenario DCF + peer comps + margin of safety from yfinance fundamentals | `mm-valuation-engine` |
| **Debate** | Independent memos, moderator-assigned critique pairs, and targeted debate | 3-6 analysts |
| **Draft** | JPM-style narrative report with evidence traceability | `mm-report-writer` |
| **Review** | Multi-dimensional scoring and iterative revision loop | `mm-report-reviewer` |
| **Decide** | Multi-round panel — roles vote + self-rate, a convergence grader gates the loop, then the final BUY/HOLD/SELL call (+ hedge overlay) | `mm-decision-panelist`, `mm-decision-maker` |
| **Export** | Annotated SVG charts + Markdown→HTML/CSS→PDF (WeasyPrint) into a JPM-style report | `mm-pdf-exporter` |
| **User Review** | Pause for user feedback — agreement, corrections, personal insights | user (human-in-the-loop) |
| **Reflect** | Evaluate run quality via code graders, store user feedback + long-term memory | eval pipeline + memory |

### Analyst Roles (Configurable)

| # | Role | Focus | Default |
|---|------|-------|:-------:|
| 1 | `company_analyst` | Fundamentals, company events, catalysts | ✓ |
| 2 | `risk_analyst` | Bear case, downside risks, failure conditions | ✓ |
| 3 | `market_analyst` | Macro context, sector positioning, alpha vs beta | ✓ |
| 4 | `valuation_analyst` | Interprets the valuation engine's DCF range, comps, and margin of safety | |
| 5 | `technical_analyst` | Chart patterns, momentum, trading signals | |
| 6 | `catalyst_analyst` | Event timing, earnings calendar, near-term catalysts | |

Enable additional analysts by uncommenting roles in `config.yaml`.

---

## Project Structure

```text
MarketMind-AlphaEngine/
├── .claude/
│   ├── agents/                    # Model tier definitions (heavy/standard/light)
│   └── skills/                    # 21 agent skills
│       ├── mm-orchestrator/       # Pipeline driver (iron law: never stop)
│       ├── mm-company-resolver/   # Ticker -> profile + peers
│       ├── mm-market-desk/        # Macro data collection
│       ├── mm-company-desk/       # Company news + filings + fundamentals
│       ├── mm-sector-desk/        # Sector + peer data
│       ├── mm-web-research/       # Web/NASDAQ news with source provenance
│       ├── mm-quant-analyst/      # Technical indicator computation
│       ├── mm-valuation-engine/   # Scenario DCF + comps + margin of safety
│       ├── mm-market-analyst/     # Market environment analysis
│       ├── mm-company-analyst/    # Company fundamentals analysis
│       ├── mm-risk-analyst/       # Risk identification + counter-arguments
│       ├── mm-valuation-analyst/  # Valuation framework + price target
│       ├── mm-technical-analyst/  # Chart interpretation + signals
│       ├── mm-catalyst-analyst/   # Event timing + catalyst calendar
│       ├── mm-discussion-moderator/ # Debate scan + synthesis
│       ├── mm-report-writer/      # Research report generation
│       ├── mm-report-reviewer/    # Multi-dimensional quality scoring
│       ├── mm-decision-panelist/  # Per-role decision ballot (vote + conviction + overlay)
│       ├── mm-decision-maker/     # Panel chair: per-round tally + final BUY/HOLD/SELL decision
│       ├── mm-pdf-exporter/       # Chart generation + Markdown -> HTML/CSS -> PDF
│       ├── mm-progress-monitor/   # Background progress tracking
│       ├── mm-memory-writer/      # Memory extraction from completed runs
│       └── mm-init/               # Workspace initialization
├── plugin/
│   ├── .claude-plugin/            # Plugin metadata
│   └── commands/                  # User-facing commands (/mm:init, /mm:run, /mm:status)
├── .mcp.json                      # MCP server registration for Claude Code
├── mcp/                           # MCP servers (market-data, mm-workspace, memory)
│   ├── market_data_server.py      # incl. get_fundamentals (DCF/comps inputs)
│   ├── workspace_server.py
│   ├── memory_server.py
│   └── shared/
│       ├── contracts.py           # Single source of truth (stages, paths, naming)
│       ├── schemas.py
│       └── rate_limiter.py
├── valuation/                     # Formula-first valuation engine
│   ├── dcf.py                     # WACC, FCFF projection, terminal value, sensitivity grid
│   ├── comps.py                   # Peer multiples + quartile benchmarking
│   ├── run_valuation.py           # Stage runner -> valuation_summary.json
│   └── tests/                     # Unit tests (pytest)
├── memory/                        # Long-term memory store (episodic/semantic/procedural)
├── eval/                          # Evaluation pipeline (graders, run log, metrics)
│   ├── graders/                   # Factuality, evidence, consistency, valuation, depth, panel-convergence, decision-risk, cost graders
│   ├── release_gate.py            # Deterministic pass/warning/failed verdict
│   ├── stage_timer.py             # Stage start/end timestamp recorder
│   ├── finalize_run.py            # Assembles run log entry from all artifacts
│   └── metrics.py                 # Aggregate dashboard computation
├── logs/
│   └── run_log.jsonl              # Append-only pipeline run history (not committed)
├── templates/
│   ├── render_pdf.py              # Markdown -> HTML/CSS -> PDF renderer (WeasyPrint)
│   ├── report.css                 # JPM-style stylesheet (cover, tables, CJK)
│   ├── report.html.j2             # Report HTML template (cover + rating box)
│   └── charts.py                  # Annotated chart generator (matplotlib, SVG)
├── workspaces/                    # Company workspaces (date-stamped)
│   ├── shared/market_context/     # Reusable macro data
│   └── {TICKER}/                  # Per-company workspace
├── tests/                         # Test scripts
├── config.example.yaml            # Configuration template
├── config.yaml                    # Local config (not committed, contains API keys)
├── setup.sh                       # Environment setup
├── CLAUDE.md                      # System prompt + pipeline docs
├── README.md
└── README_CN.md
```

### Workspace Structure (Date-Stamped)

```text
workspaces/NVDA/
├── profile/                       # Static company reference (undated)
├── raw/{YYYY-MM-DD}/              # Raw data per trading day (news, prices, fundamentals)
├── normalized/{YYYY-MM-DD}/       # Evidence cards
├── quant/{YYYY-MM-DD}/            # Technical indicators
├── valuation/{YYYY-MM-DD}/        # DCF + comps + margin of safety
├── discussion/{YYYY-MM-DD}/       # Analyst memos + debate
├── drafts/{YYYY-MM-DD}/           # Report drafts
├── reviews/{YYYY-MM-DD}/          # Quality scores + revision briefs
├── decision/{YYYY-MM-DD}/         # BUY/HOLD/SELL decision
├── final/{YYYY-MM-DD}/            # Final report (Markdown + JSON)
├── exports/{YYYY-MM-DD}/pdf/      # JPM-style PDF report + charts
├── shared_context/{YYYY-MM-DD}.json   # Per-run shared bundle (quant+valuation+profile+peers+catalysts)
└── memory/{YYYY-MM-DD}_{role}.json    # Per-run retrieved memory context (analyst/writer/reviewer)
```

---

## Configuration

All runtime behavior is controlled by `config.yaml` (copy from `config.example.yaml`):

```yaml
# Key configuration sections
run_mode: daily              # daily | weekly
data_sources:
  news:
    provider: newsapi        # falls back to web_search if no API key
    fallback: web_search
discussion:
  debate_mode: selective     # selective (moderator picks pairs) | full (N×N)
  analyst_roles:
    - company_analyst
    - risk_analyst
    - market_analyst
    # - valuation_analyst    # uncomment to enable
valuation:                   # scenario DCF + comps (Stage 6)
  enabled: true
  equity_risk_premium: 0.05
  default_terminal_growth: 0.025
  projection_years: 5
  scenario_growth_delta: 0.03   # bull/bear offset around the base case
review:
  min_overall_score: 8.0
  min_factuality: 9.0
```

---

## Data Sources

| Source | API Key Required | Used For |
|--------|:----------------:|----------|
| yfinance | No | Stock prices, indices, peers, macro assets, and fundamentals (DCF/comps inputs) |
| NewsAPI | Optional | Market, sector, and company news (free tier) |
| SEC EDGAR | No | 10-K, 10-Q, 8-K filings, and insider transactions |
| FRED | Optional | Macro indicators such as US10Y, USD, and VIX |
| NASDAQ | No | US-name news + quote via `api.nasdaq.com` (unofficial), with nasdaq.com pages as fallback — collected by `mm-web-research` |
| WebSearch / WebFetch | No | Provenance-tagged web news (`mm-web-research`) and verification, any market |

**Source hierarchy:** institutional/MCP → NewsAPI → NASDAQ (US names) → general web search. The NewsAPI and FRED keys are read from the `NEWSAPI_KEY` / `FRED_API_KEY` environment variables, which the market-data MCP server auto-loads from a `.env` file at the repo root at startup (shell exports take precedence).

---

## Roadmap

**Current: dev 0.1**. The core pipeline is functional and JPM-style PDF generation is working.

### Done

- [x] **MCP Server Architecture**: 3 MCP servers (market-data, mm-workspace, memory) for structured agent tool access
- [x] **Long-Term Memory System**: Episodic, semantic, and procedural memory layers across runs
- [x] **Automated Evaluation Pipeline**: Code-based graders, run log, and aggregated metrics for quality tracking
- [x] **Quantitative Valuation Engine**: Formula-first scenario DCF + peer comps + margin of safety, with an internal-consistency audit grader and **component-derived confidence** (a low-confidence DCF can't inflate the blend), feeding decisions as a confidence-weighted reference
- [x] **Institutional PDF Rendering**: Deterministic Markdown → HTML/CSS → PDF (WeasyPrint) — Page-1 rating box, embedded annotated SVG charts, styled tables, running headers/footers, and a self-degrading committed renderer (no LaTeX, no per-run scripts)
- [x] **Bilingual Output**: English + Chinese reports and PDFs via the `language` config, with CJK rendered cleanly end-to-end (body + charts)
- [x] **Web Presentation**: Browser report viewer (`/mm:dashboard`) with Document, **Slides** (auto-split by section, keyboard navigation + nav dots), and embedded-PDF modes — all driven by the same report content
- [x] **Codex CLI Support**: The `mm-*` skills run as native Codex skills (`.agents/skills/` symlink + per-skill `agents/openai.yaml` invocation policy + MCP dependencies), with `AGENTS.md` as the control surface and a ready-to-paste `~/.codex/config.toml` MCP config
- [x] **Codex Parity Driver**: `scripts/run_codex_pipeline.py` runs each LLM stage as its own `codex exec` (fresh context per stage — the analog of Claude's `context: fork`) with deterministic stages in committed Python and parallel desks/analysts, so Codex output matches a Claude run instead of compressing into stubs
- [x] **Deterministic Quality Floor**: a depth gate (`eval/graders/depth_grader.py`) flags shallow analyst memos, stub report sections, and scant evidence — wired into the review loop (redo) and the release gate — so accurate-but-thin output can't silently pass
- [x] **Decision Panel Debate Loop**: the decide stage runs a multi-round panel — each analyst role votes (BUY/HOLD/SELL) with a conviction self-rating and a hedge overlay, a deterministic convergence grader (`eval/graders/panel_convergence_grader.py`) gates iterate-vs-exit with a hard round cap, and the chair writes a final call faithful to the conviction-weighted lean with dissent retained
- [x] **Decision Risk Gate**: an advisory, non-mutating confidence ceiling (`eval/graders/decision_risk_grader.py`) caps the final call's confidence on reproducible signals — weak panel convergence, retained dissent, low-confidence valuation cited as a reason, and thin evidence — surfacing over-confidence as a release-gate warning without rewriting the decision

### TODO

- [ ] **Advanced Quantitative Methods**: Factor models, rolling beta/correlation, and event study framework
- [ ] **Portfolio Mode**: Multi-company orchestration, sector-level reports, and portfolio-level risk views
- [ ] **Historical Comparison**: Compare the latest report against previous runs and track thesis evolution
- [ ] **Sentiment Analysis**: Social sentiment, options flow, and institutional positioning
- [ ] **Real-Time Dashboard**: Live monitoring with auto-refresh
- [ ] **Chinese Market Support**: Full A-share coverage with local data sources such as Tushare and AKShare
- [ ] **Automated Scheduling**: Cron-based daily report generation
- [ ] **Custom Analyst Personas**: Configurable risk appetite, directional bias, and horizon overlays

---

## License

[MIT License](LICENSE)

---

<p align="center">
  <sub>Built with Claude Code · Powered by Multi-Agent Debate</sub>
</p>

<p align="center">
  <h1 align="center">MarketMind-AlphaEngine</h1>
  <p align="center">
    <strong>Multi-Agent Equity Research System with Investment Bank Style Reports</strong>
  </p>
  <p align="center">
    Automated daily & weekly stock analysis · Multi-analyst debate · BUY/HOLD/SELL decisions · JPM-style PDF reports
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
- **Investment-Bank-Style PDF**: Generates JPM-style research reports with annotated charts, narrative paragraphs, visual hierarchy, and professional typography via LaTeX
- **Selective Debate**: Moderator-directed cross-critique saves 50-90% of tokens compared with full N×N debate as analyst count scales
- **Date-Stamped History**: Every run preserves outputs under `{YYYY-MM-DD}/` folders so the same company can be analyzed daily without losing prior research
- **Smart Trading Day Logic**: Automatically determines the correct data cutoff, including pre-market sessions, weekends, and holidays
- **Free Data Sources**: Works entirely with free APIs (yfinance, NewsAPI free tier, SEC EDGAR, FRED) with WebSearch fallback when API keys are unavailable

---

## Quick Start

### Prerequisites

- [Claude Code](https://code.claude.com) CLI installed
- [Ralph Loop plugin](https://github.com/anthropics/claude-code) (recommended for long-running execution)
- Python 3.10+
- LaTeX (`xelatex`) for PDF generation. On macOS: `brew install --cask mactex`

### Setup

```bash
# 1. Clone the repository
git clone git@github.com:ShinyGua/MarketMind-AlphaEngine.git
cd MarketMind-AlphaEngine

# 2. Create the Python environment
source setup.sh

# 3. (Optional) Add your API keys to config.yaml
cp config.example.yaml config.yaml
# Edit config.yaml: add NewsAPI key, FRED key, etc.
# Leaving keys blank is allowed; WebSearch fallback still works

# 4. Launch Claude Code with the plugin
claude --plugin-dir "$PWD/plugin"
```

### Usage

```text
/mm:init                          # Create a new company workspace (interactive)
/mm:run workspaces/NVDA           # Run the full 12-stage research pipeline
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
/mm:init -> /mm:run -> 12-stage autonomous pipeline -> PDF report
```

```text
resolve_config -> init_workspace -> collect (3 desks parallel)
    -> normalize -> quant -> discuss_memos (analysts parallel)
    -> discuss_debate (selective) -> discuss_synthesis
    -> draft -> review_loop -> decide -> export + PDF
```

### Stage Details

| Stage | What Happens | Agents |
|-------|--------------|--------|
| **Collect** | Macro, company, and sector data from yfinance, NewsAPI, and EDGAR | 3 desks in parallel |
| **Quant** | RSI, MACD, SMA, ATR, and relative strength calculations in Python | `mm-quant-analyst` |
| **Debate** | Independent memos, moderator-assigned critique pairs, and targeted debate | 3-6 analysts |
| **Draft** | JPM-style narrative report with evidence traceability | `mm-report-writer` |
| **Review** | Multi-dimensional scoring and iterative revision loop | `mm-report-reviewer` |
| **Decide** | BUY/HOLD/SELL decision with confidence, reasons, and risks | `mm-decision-maker` |
| **Export** | Annotated charts plus LaTeX output into a JPM-style PDF report | `mm-pdf-exporter` |

### Analyst Roles (Configurable)

| # | Role | Focus | Default |
|---|------|-------|:-------:|
| 1 | `company_analyst` | Fundamentals, company events, catalysts | ✓ |
| 2 | `risk_analyst` | Bear case, downside risks, failure conditions | ✓ |
| 3 | `market_analyst` | Macro context, sector positioning, alpha vs beta | ✓ |
| 4 | `valuation_analyst` | P/E, DCF, valuation framework, price target | |
| 5 | `technical_analyst` | Chart patterns, momentum, trading signals | |
| 6 | `catalyst_analyst` | Event timing, earnings calendar, near-term catalysts | |

Enable additional analysts by uncommenting roles in `config.yaml`.

---

## Project Structure

```text
MarketMind-AlphaEngine/
├── .claude/
│   ├── agents/                    # Model tier definitions (heavy/standard/light)
│   └── skills/                    # 19 agent skills
│       ├── mm-orchestrator/       # Pipeline driver (iron law: never stop)
│       ├── mm-company-resolver/   # Ticker -> profile + peers
│       ├── mm-market-desk/        # Macro data collection
│       ├── mm-company-desk/       # Company news + filings
│       ├── mm-sector-desk/        # Sector + peer data
│       ├── mm-quant-analyst/      # Technical indicator computation
│       ├── mm-market-analyst/     # Market environment analysis
│       ├── mm-company-analyst/    # Company fundamentals analysis
│       ├── mm-risk-analyst/       # Risk identification + counter-arguments
│       ├── mm-valuation-analyst/  # Valuation framework + price target
│       ├── mm-technical-analyst/  # Chart interpretation + signals
│       ├── mm-catalyst-analyst/   # Event timing + catalyst calendar
│       ├── mm-discussion-moderator/ # Debate scan + synthesis
│       ├── mm-report-writer/      # Research report generation
│       ├── mm-report-reviewer/    # Multi-dimensional quality scoring
│       ├── mm-decision-maker/     # BUY/HOLD/SELL decision
│       ├── mm-pdf-exporter/       # Chart generation + LaTeX -> PDF
│       ├── mm-progress-monitor/   # Background progress tracking
│       └── mm-init/               # Workspace initialization
├── plugin/
│   ├── .claude-plugin/            # Plugin metadata
│   └── commands/                  # User-facing commands (/mm:init, /mm:run, /mm:status)
├── templates/
│   ├── equity_research.cls        # JPM-style LaTeX document class
│   └── charts.py                  # Annotated chart generator (matplotlib)
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
├── raw/{YYYY-MM-DD}/              # Raw data per trading day
├── normalized/{YYYY-MM-DD}/       # Evidence cards
├── quant/{YYYY-MM-DD}/            # Technical indicators
├── discussion/{YYYY-MM-DD}/       # Analyst memos + debate
├── drafts/{YYYY-MM-DD}/           # Report drafts
├── reviews/{YYYY-MM-DD}/          # Quality scores + revision briefs
├── decision/{YYYY-MM-DD}/         # BUY/HOLD/SELL decision
├── final/{YYYY-MM-DD}/            # Final report (Markdown + JSON)
└── exports/{YYYY-MM-DD}/pdf/      # JPM-style PDF report + charts
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
review:
  min_overall_score: 8.0
  min_factuality: 9.0
```

---

## Data Sources

| Source | API Key Required | Used For |
|--------|:----------------:|----------|
| yfinance | No | Stock prices, indices, peers, and macro assets |
| NewsAPI | Optional | Market, sector, and company news (free tier) |
| SEC EDGAR | No | 10-K, 10-Q, 8-K filings, and insider transactions |
| FRED | Optional | Macro indicators such as US10Y, USD, and VIX |
| WebSearch | No | Fallback for news and web-based verification when keys are unavailable |

---

## Roadmap

**Current: dev 0.1**. The core pipeline is functional and JPM-style PDF generation is working.

### TODO

- [ ] **Web Presentation**: Interactive HTML slide deck aligned with PDF content (reveal.js or custom)
- [ ] **Advanced Quantitative Methods**: Factor models, rolling beta/correlation, and event study framework
- [ ] **Portfolio Mode**: Multi-company orchestration, sector-level reports, and portfolio-level risk views
- [ ] **Historical Comparison**: Compare the latest report against previous runs and track thesis evolution
- [ ] **Sentiment Analysis**: Social sentiment, options flow, and institutional positioning
- [ ] **Real-Time Dashboard**: Live monitoring with auto-refresh
- [ ] **Chinese Market Support**: Full A-share coverage with local data sources such as Tushare and AKShare
- [ ] **Report Translation**: Bilingual output (English + Chinese) via configuration
- [ ] **Automated Scheduling**: Cron-based daily report generation
- [ ] **Custom Analyst Personas**: Configurable risk appetite, directional bias, and horizon overlays

---

## License

[MIT License](LICENSE)

---

<p align="center">
  <sub>Built with Claude Code · Powered by Multi-Agent Debate</sub>
</p>

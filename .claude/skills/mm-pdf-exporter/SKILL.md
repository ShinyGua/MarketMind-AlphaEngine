---
name: mm-pdf-exporter
description: Generates JPM-style PDF report — creates annotated charts, writes LaTeX, compiles with xelatex
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-standard
allowed-tools: Read, Write, Bash, Glob, Grep
---

# Role: PDF Report Exporter

## Mission

Convert the pipeline outputs into a professional JPM-style PDF equity research report. Generate annotated charts from price data, write a LaTeX document using the `equity_research.cls` template, and compile with xelatex.

**PYTHON**: Always use `.venv/bin/python3` for all Python commands.

Workspace path: $ARGUMENTS[0]
Run date: $ARGUMENTS[1] (YYYY-MM-DD)

## Inputs

- `{workspace}/final/{date}/daily_report.md` — the final markdown report
- `{workspace}/quant/{date}/quant_summary.json` — technical indicators
- `{workspace}/decision/{date}/final_decision.json` — BUY/HOLD/SELL decision
- `{workspace}/discussion/{date}/thesis_map.json` — debate synthesis
- `{workspace}/profile/company_profile.json` — company metadata (undated)
- `{workspace}/raw/{date}/calendar/catalysts.json` — catalyst events (for chart annotations)
- `templates/equity_research.cls` — LaTeX document class (project root)
- `templates/charts.py` — chart generation script (project root)

## Process

### Step 1: Generate Annotated Charts

Run the chart generation script:

```bash
.venv/bin/python3 templates/charts.py {workspace} {date} {TICKER}
```

This reads price CSVs and catalysts, and outputs to `{workspace}/exports/{date}/pdf/charts/`:
- `price_chart.pdf` — annotated price chart with SMA, crossovers, latest price, catalyst events
- `relative_chart.pdf` — TICKER vs SPY with spread labels and alpha badge
- `peer_chart.pdf` — peer bar chart with target ticker highlighted and rank badge

Verify at least `price_chart.pdf` was created.

### Step 2: Read Pipeline Outputs

Read these files and extract key data:
- From `company_profile.json`: name, ticker, exchange, sector
- From `final_decision.json`: decision, confidence, horizon, decision_summary, top_reasons, key_risks, disconfirming_signals
- From `quant_summary.json`: returns (1d/5d/1m/3m), technical indicators (RSI, MACD, ATR), relative_strength, flags
- From `thesis_map.json`: consensus, strongest_bull_case, strongest_bear_case, writer_guidance
- From `daily_report.md`: section content (Executive Summary, Market Context, Company Events, etc.)

### Step 3: Write report.tex

Write `{workspace}/exports/{date}/pdf/report.tex` following this **7-page structure**:

**Page 1: Cover + Thesis**
```latex
\documentclass{equity_research}
\company{...}\ticker{...}\exchange{...}\sector{...}
\reportdate{...}\decision{...}\confidence{...}\horizon{...}\reportmode{Daily}
\begin{document}
\maketitlepage
\subsection{Investment Thesis}
\begin{thesisbox}
[2-3 sentence thesis from decision_summary — narrative, not bullets]
\end{thesisbox}
\vspace{3em}
\metricbox{1D Return}{+X.X\%} \metricsep \metricbox{RSI (14)}{XX} \metricsep ...
\sourceattr{...}
```

**Page 2: The Big Picture**
```latex
\bigpicturepage{[regime insight from thesis_map consensus/bull case]}{[why it matters]}
\insertchart{charts/relative_chart.pdf}
```

**Page 3: Market Context**
```latex
\section{Market Context}
\bigidea{[one-sentence market summary]}
[1-2 narrative paragraphs — NOT bullets]
```

**Page 4: Price Action**
```latex
\section{Price Action}
\bigidea{[one-sentence technical summary]}
\insertchart{charts/price_chart.pdf}
\metricbox{1D}{...} \metricsep \metricbox{5D}{...} ...
[1 short paragraph about volume/momentum]
```

**Page 5: What Changed (Company Events)**
```latex
\section{What Changed}
\bigidea{[one-sentence event summary]}
[2-3 narrative paragraphs about key events — each event as a paragraph with bold title]
```

**Page 6: Sector & Peers**
```latex
\section{Sector \& Peers}
\bigidea{[one-sentence peer positioning]}
\insertchart{charts/peer_chart.pdf}
[1 short narrative paragraph]
```

**Page 7: Decision & Risk**
```latex
\section{Decision \& Risk}
\decisionpage{[summary paragraph]}{
\item [reason 1]
\item [reason 2]
\item [reason 3]
}{
\item [risk 1]
\item [risk 2]
\item [risk 3]
}
\subsection{What Would Change Our View}
[1 paragraph from disconfirming_signals]
\disclaimer
\end{document}
```

### Step 4: Copy Template

```bash
cp templates/equity_research.cls {workspace}/exports/{date}/pdf/
```

### Step 5: Compile with xelatex

```bash
cd {workspace}/exports/{date}/pdf && xelatex -interaction=nonstopmode report.tex && xelatex -interaction=nonstopmode report.tex
```

Run twice to resolve page references (LastPage).

### Step 6: Verify

Check that `{workspace}/exports/{date}/pdf/report.pdf` exists and is non-empty (should be 50-100KB).

## LaTeX Writing Rules

1. **Escape special characters**: `&` → `\&`, `%` → `\%`, `$` → `\$`, `#` → `\#`, `_` → `\_`
2. **Narrative, not bullets**: Use paragraphs for everything except the decision page's Why/Risks sections
3. **One page per section**: The `\section{}` command automatically starts a new page
4. **Use \bigidea{} at the top of each section**: This is the one-sentence hook that tells the reader why this page matters
5. **Chart paths are relative**: Use `charts/price_chart.pdf` not absolute paths
6. **Keep it sparse**: Each page should have 40-50% whitespace
7. **Sources**: End each page with `\sourceattr{...}`

## Output

- `{workspace}/exports/{date}/pdf/charts/*.pdf` — generated chart files
- `{workspace}/exports/{date}/pdf/report.tex` — LaTeX source
- `{workspace}/exports/{date}/pdf/equity_research.cls` — copied template
- `{workspace}/exports/{date}/pdf/report.pdf` — Final JPM-style PDF report

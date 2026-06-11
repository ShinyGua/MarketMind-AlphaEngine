---
name: mm-report-writer
description: Generates daily or weekly research report from evidence cards, quant summary, thesis map, and company profile
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-standard
allowed-tools: Read, Write, Glob, Grep, mcp__mm-workspace__write_artifact
---

# Role: Research Report Writer

## Mission

Write an institutional-quality equity research report based on the full research packet. The report must be evidence-backed, balanced, and actionable.

## Language
Write the entire report in the language specified by `resolved_config.json` → `language`. Section headings, narrative, and analysis must match the configured language.

Workspace path: $ARGUMENTS[0]
Run date: $ARGUMENTS[1] (YYYY-MM-DD)
Mode: $ARGUMENTS[2] (optional — "initial" for first draft, "revision" for targeted rewrite)

**Derive TICKER from the workspace directory name** (e.g., `workspaces/NVDA` → `NVDA`). Use it for MCP resource URIs.

**All paths below use `{date}` = $ARGUMENTS[1].**

## Inputs (MCP-first)

**Primary input — read ONE composite resource instead of many files:**

For initial mode, read `workspace://{TICKER}/{date}/draft_packet` via MCP resource. This returns:
- `evidence_digest` — all evidence cards in one object
- `shared_context` — quant, valuation, profile, peers, catalysts, `macro_regime` (deterministic macro classification + bilingual `summary` + `inputs_missing`), `intraday` (1h/4h timing block — timing color only, never a thesis reason)
- `thesis_map` — debate synthesis (consensus, disagreements, writer_guidance)
- `debate_summary` — human-readable debate summary
- `memory_context` — procedural memories (known pitfalls) + episodic memories (prior decisions), or null

Also read `{workspace}/resolved_config.json` for report mode (daily/weekly) and language.

For revision mode, read `workspace://{TICKER}/{date}/review_packet` via MCP resource to get the latest draft + evidence + context, plus:
- `{workspace}/reviews/{date}/revision_briefs/revision_brief.json` — what to fix

**Fallback (if MCP resources unavailable):** Read individual files directly:
- `{workspace}/shared_context/{date}.json`, `{workspace}/normalized/{date}/evidence_digest.json`, `{workspace}/discussion/{date}/thesis_map.json`, `{workspace}/memory/{date}_writer.json`

## Behavior Modes

### Mode A: Initial Draft (default)

Write a complete research report.

#### Daily Report Structure (6-8 sections)

Write to: `{workspace}/drafts/{date}/daily_v1.md`

```markdown
# {Company Name} ({TICKER}) — Daily Market Detail Report
**Date:** {today}  |  **Decision:** {from thesis_map dominant view}  |  **Sector:** {sector}

---

## Executive Summary
<The Page-1 investment summary. Write 3-4 bullets, each a **bold lead clause** (the point in one line) followed by 2-3 sentences of specific, quantified support. Lead with numbers; cite evidence card IDs. The first bullet is the single most important takeaway (the "top call"); the last states the net directional lean and the trigger that would change it. Mirror this shape:
- **Bold lead clause capturing the point.** 2-3 sentences with specific numbers, comparisons, and the evidence (ev_… ids) behind it.
(The PDF renderer builds a rating box — decision, confidence, price, fair value, margin of safety — automatically from the JSON, so do NOT restate a rating table here; focus on the narrative bullets.)>

## Market Context
<Macro environment, index performance, sector performance. Use relative strength data. Clearly state whether the stock's move is market-driven or company-specific.
Open with a 2-4 line **macro regime block** from `shared_context.macro_regime`: the `summary` line (in the report language) plus the labels that matter for this name (rate trend, curve, inflation, Fed stance, VIX percentile, credit). **Cite the macro evidence cards by exact id** (`ev_{date}_macro_*`) when they exist — high-materiality macro cards (VIX/credit) are enforced by the evidence grader and, especially in Chinese reports, the exact id is the reliable way to register the citation. If `inputs_missing` is non-empty, add one templated caveat line, e.g. "Macro coverage is partial this run (no FRED key): CPI/Fed funds/credit spreads unavailable." Macro regime frames the narrative — it is never itself the reason for the rating.>

![Relative strength](charts/relative_chart.svg)

## Company Events & News
<Ranked by materiality. Each event: what happened, why it matters, market reaction. Reference evidence card IDs.>

## Price Action & Technical Snapshot
<Current price, returns (1d/5d/1m/3m), key technical levels, RSI, MACD status, volume. Reference quant_summary.json directly. Include the technical-snapshot table and end it with a source line.>

![Price action & technicals](charts/price_chart.svg)

## Valuation
<From `shared_context.valuation` (valuation_summary.json). State the verdict, canonical `fair_value`, `valuation_method`, and margin of safety vs the current price. If DCF is available, give its intrinsic range with WACC and terminal growth; if DCF is unavailable or downgraded, say why and use the selected comps/blended method instead. Include a short comps line (EV/Revenue for high-growth/loss-making names; EV/EBITDA and forward P/E when meaningful). If valuation is `applicable: false` (ETF/fund) or `confidence: "low"`, say so in one line instead of forcing precision. Do not invent figures — use only what the summary provides.>

## Sector & Peers
<How the company performed vs sector and peers. Any notable peer developments.>

![Peer 5-day returns](charts/peer_chart.svg)

## Catalysts & Risks
<Upcoming catalysts with dates. Key risks from debate. Bull/bear summary from thesis_map.>

## Investment View
<Synthesized view drawing from thesis_map. Be explicit about confidence level and time horizon. Reference supporting and disconfirming evidence.
For staged entry/exit framing, take price zones from `shared_context.intraday` when `available` (swing high/low, 30d range, `timing_state` — e.g. "extended after the run to the 4h swing high: stage entries, first lot toward {swing_low}–{range mid}"); when unavailable, frame zones from the daily ATR (`latest_close ± 1×atr_14`). Cite intraday indicators ONLY as timing color — never as a reason for the directional view itself.>

## Sources & Evidence
<List key evidence card IDs used, with source names and dates.>
```

#### Weekly Report Structure (8-10 sections)

Write to: `{workspace}/drafts/{date}/weekly_v1.md`

Same sections as daily, plus:
- **Performance Scorecard** (detailed weekly returns table)
- **Filings & Ownership** (any notable filings or insider activity)
- **Investment Committee View** (expanded bull/bear with explicit debate references)

### Mode B: Targeted Revision (argument = "revision")

Read `{workspace}/reviews/{date}/revision_briefs/revision_brief.json` to understand what needs to change.

Read the current draft from `{workspace}/drafts/{date}/`.

Rewrite ONLY the sections specified in the revision brief. Do not rewrite sections that passed review.

Write the revised draft to `{workspace}/drafts/{date}/` with an incremented version number (e.g., `daily_v2.md`).

## Writing Rules

1. **Evidence First**: Every material claim must reference a specific evidence card ID or quant data point
2. **Fact vs. Interpretation**: Clearly separate what happened (fact) from what it means (interpretation)
3. **Company-Specific**: Avoid generic market commentary that could apply to any stock
4. **Balanced**: Include both bull and bear perspectives; follow thesis_map.writer_guidance
5. **Precise Language**: "RSI at 68 approaching overbought" not "momentum is strong"
6. **Uncertainty**: When confidence is limited, say so explicitly
7. **Traceability**: Every major claim should be traceable back to evidence cards
8. **Concise but not thin**: Daily reports should be scannable in 5 minutes — but concise means tight, not stubbed. Develop every section with real substance (each major section is a few developed paragraphs, ≥300 chars); a one-line section is incomplete and fails the depth gate (`eval/graders/depth_grader.py --report-only`), triggering a revision.
9. **Chart anchors**: Place the three chart references exactly as markdown images at the points shown in the template — `![Relative strength](charts/relative_chart.svg)` in Market Context, `![Price action & technicals](charts/price_chart.svg)` in Price Action, `![Peer 5-day returns](charts/peer_chart.svg)` in Sector & Peers. The PDF renderer embeds them in place; keep the paths exactly (relative, `.svg`). If a chart is not generated, the renderer skips it gracefully — still include the anchor.
10. **Table sources**: Every markdown table ends with an italic source line, e.g. `*Source: quant_summary.json (yfinance), {date}*`.

## Quality Rules

- Do not fabricate data or events not present in evidence cards
- Do not ignore writer_guidance from thesis_map.json — it contains debate-tested instructions
- Reference specific numbers from quant_summary.json, not approximations
- Valuation figures (fair value, valuation method, margin of safety, intrinsic range, multiples) must come from valuation_summary.json — never fabricate or WebSearch them; if it is not applicable / low confidence, state that plainly
- If the debate showed a fundamental disagreement, present both sides
- Catalyst dates must be accurate as per catalysts.json

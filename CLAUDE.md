# MarketMind-AlphaEngine

Multi-agent equity research pipeline producing daily/weekly reports with BUY / HOLD / SELL decisions: collects market/sector/company data from free APIs → quant indicators → scenario valuation → structured multi-analyst debate → institutional-quality report → quality review loop → evidence-backed decision.

## Language Rules (apply to every skill and artifact)

Output language comes from the `language` config field (`en` | `ch`). When `ch`, all user-facing text (memos, reports, PDF, chart labels, `/mm:init` prompts) is Chinese.

1. **One language per run.** A report/memo mixing English and Chinese narrative is a defect, not a style choice.
2. **Never emit a bilingual literal.** Headings, enum glosses and table labels carry exactly one language. Do NOT write `## Story & Game (故事与博弈)` — that single atomic string is how Chinese reaches an English report: a model copies the nearest literal, and a directive twenty lines above never outranks it. Each skill carries a **Language Map** table with copy-ready single-language cells; substitute from it.
3. **Always English in both languages:** JSON keys, enum VALUES (`hazy_but_coming`, `independent_up`, `accumulate`), evidence-card ids, file paths, tickers, indicator names, BUY/HOLD/SELL.

A-share jargon (量比 / 龙虎榜 / 北向 …) is gated on **`market_profile`, not `language`**: on an `en` run for a CN/HK name the Chinese source term may appear parenthetically as provenance on first mention; on an `en` run for a US/JP/UK/EU name the report contains **zero Chinese characters**. Enforced by `eval/graders/language_purity_grader.py`.

## Python Environment (Mandatory)

**All Python calls MUST use `.venv/bin/python3`** (relative to project root). Never bare `python3` — the system interpreter lacks required packages. Setup: `source setup.sh`. (PDF export also needs WeasyPrint native libs — Pango/cairo/GDK-PixBuf — and a CJK font for Chinese reports.)

## Evidence-Usage Contracts (cross-cutting — the core project rules)

Each evidence family carries a `usage` contract that bounds what it may justify. These asymmetries are deliberate:

| Family | Contract | May it set/flip a stance or vote? |
|---|---|---|
| **Chips / volume** (`shared_context.chips`) | `directional` | **Yes — first-class stance evidence, may stand alone in `top_reasons` and carry the label.** Chip exchange is the reproducible trace of actual buying/selling force. Respect per-block `data_quality`: an `unavailable` block supports nothing |
| **Macro regime** | context, not trigger | No. Feeds the WACC input, analyst framing, and `risk_overlay` narrative; never flips a vote or caps conviction |
| **Intraday 1h/4h** (`shared_context.intraday`) | `timing_only` | No. Frames staged entry/exit price zones in `stance_notes` only (chip S/R + daily ATR fallback when `available: false`). Citing it as a vote reason → decision-risk grader warns |
| **Daily SMA / `trend_regime`** | `context_only` | No. Trend backdrop only; a price-vs-SMA position or MA cross is never the sole reason for a stance or vote (prompt-level de-weighting; no grader) |
| **Valuation** (`summary.role`) | `anchor` (US/JP/UK/EU) or `reference` (CN/HK) | Anchor: full thesis weight. Reference: value floor / sanity read that may shape confidence and risk framing but must **not** appear in `final_decision.top_reasons` (grader warns) — in game-driven markets story + chips + fundamentals carry the label |

## Getting Started

```bash
claude --plugin-dir "$MM_ROOT/plugin" --dangerously-skip-permissions
```

- `/mm:init` — asks which company, verifies via web search, creates `workspaces/{TICKER}/`
- `/mm:run workspaces/NVDA` — autonomous pipeline, all stages continuously, tracked via TodoWrite. Only `user_review` pauses. Re-run the same command to resume after an interruption.
- `/mm:status` — all workspaces and their pipeline progress

---

## Pipeline Stages

```text
resolve_config → init_workspace → collect(parallel) → normalize
    → quant → valuation → discuss → draft → review_loop → decide → export → reflect
```

### 0. Resolve Config
Read `workspaces/{TICKER}/config.yaml`, merge with defaults, validate, write `resolved_config.json`.

### 1. Initialize Workspace
Create the directory tree, resolve company profile (ticker/name/exchange/sector/indices), build peer set, link shared market context.

### 2. Parallel Data Collection

Preflight: `scripts/check_data_sources.py` (non-critical) records key *presence* (never values), DNS reachability, per-source status (`auth_ok`/`dns_failed`/`auth_failed`/`rate_limited`/`no_key`) and the macro collection plan (`macro.mode`: fred vs proxy) → `raw/{date}/diagnostics/data_sources.json`, so a fallback-heavy run self-explains. (Live data needs network egress; the Codex driver enables it per stage.)

Deterministic macro layer (non-critical, before the desks):
- `scripts/collect_macro_series.py` — FRED-first: CPI (CPIAUCSL/CPILFESL), FEDFUNDS, curve (DGS2/5/10/30), DTWEXBGS, HY OAS (BAMLH0A0HYM2), VIXCLS → `workspaces/shared/market_context/{date}/raw/macro/`. Keyless → proxy series (treasury quotes ÷10 to FRED percent units), missing inputs recorded in `inputs_missing` (provenance: `macro_sources.json`)
- `scripts/compute_macro_regime.py` → `indicators/macro_regime.json`: rate trend, 2s10s slope, inflation trend, Fed stance, VIX 1y percentile, USD trend, credit regime — each block with `data_quality` (fred/proxy/missing), plus a `summary_i18n` en+ch cache. The artifact is shared across same-day workspaces that may disagree on language, so read the resolved summary from `shared_context.macro_regime.summary`, **not** from here
- `scripts/macro_evidence_cards.py` — projects material observations (inverted curve, VIX ≥80th pct, stressed spreads, ≥25bp 10Y moves, easing/tightening, ≥2% USD moves) into `ev_{date}_macro_*` cards. Benign regime → zero cards
- `scripts/collect_cn_chips.py` — CN names only, non-critical: 换手率, 主力资金流, 龙虎榜, 北向持股, 融资余额, 股东户数, 解禁队列 via akshare → `raw/{date}/chips/cn_flows.json`. Every block carries its own `data_quality` (`akshare` | `unavailable`) and degrades independently (eastmoney hosts drop connections intermittently — expected). Non-CN markets skip

Then, in parallel: **mm-market-desk** (macro headlines, index + macro asset prices), **mm-company-desk** (company news, SEC filings, catalyst calendar), **mm-sector-desk** (sector news, peer prices), **mm-web-research** (web news with provenance; US names pull NASDAQ `api.nasdaq.com` first, `nasdaq.com` pages as fallback).

**Source hierarchy**: institutional/MCP > NewsAPI > NASDAQ (US) > general web search. Desks own the top tiers; mm-web-research owns the lower tiers and captures URL/date/excerpt per item.

### 3. Normalize

Deterministic chip layer first (non-critical — it needs the desks' raw price CSVs and its cards must reach the digest):
- `scripts/compute_chip_structure.py` → `quant/{date}/chip_structure.json`: volume regime (vol MAs, 量比 daily proxy, up/down-day volume, OBV trend + price divergence, CMF(20), 换手率 when float/turnover known), **VPVR chip distribution** (~120d volume-at-price → main/secondary cost peak, 90% cost band, concentration, profit/trapped ratio, VWAP cost), **support/resistance** (volume nodes + swing pivots with strength), **platform/box detection** (range width, streak, breakout with/without volume), plus embedded `cn_flows`. Prefers the run-day raw CSV over a live re-fetch so chip levels match the report's captured prices; zero-volume (suspension) bars dropped
- `scripts/chip_evidence_cards.py` → `ev_{date}_chip_*` cards (holder-count concentrating/dispersing, main-force inflow/outflow streaks, LHB appearances, northbound adds/trims, unlock overhang ≤60d, volume-confirmed breakout/breakdown, OBV divergence, extreme profit ratio). Card titles embed numbers (dedup clusters URL-less cards by title). Quiet tape → zero cards

Then: convert raw data → evidence cards, build time-series tables, deduplicate news across all four collectors (canonical URL + title) before building the evidence digest.

### 4. Quant Snapshot
- `scripts/intraday_timing.py` (non-critical) → `quant/{date}/intraday_timing.json`: 1h/4h RSI(14) + MACD(12,26,9) over ~90d of hourly bars, last confirmed 4h swing high/low, 30d range, `timing_state` label. No 1h coverage → `available: false`. Symbol resolution via `contracts.resolve_yf_symbol()` (canonical key `yf_ticker`, exchange-suffixed)
- `scripts/peer_divergence.py` (non-critical) → `quant/{date}/peer_divergence.json`: 20/60/120/250d returns for target + peers, benchmark/target correlations, deterministic **path class** (`follows_sector` / `independent_up` / `independent_down` / `basing` / `launched`; outcome-gap beats co-movement, a spliced benchmark file with a >30% daily "bar" is discarded), cohort dispersion, 60d leader/laggard
- **mm-quant-analyst**: RSI(14), MACD(12,26,9), SMA(20,50), EMA(12,26), ATR(14); returns over 1d/5d/1m/3m; relative strength vs index, sector, peers
- **No golden/death-cross flags** — MA crossovers carry no meaning for institutional readers. SMAs exist only to feed `trend_regime`; the PDF price chart annotates chip S/R + cost band instead
- `trend_regime` block in `quant_summary.json` pre-digests daily SMAs into ONE bounded label (uptrend/downtrend/transition/range from SMA20/50 stack + SMA50 slope + `price_vs_sma50_pct`) — `context_only`, propagated to `shared_context.quant.trend_regime`

Output: `technical_indicators.csv`, `quant_summary.json`, `peer_divergence.json`

### 5. Valuation (Scenario DCF + Comps)
**mm-valuation-engine** runs the formula-first engine in `valuation/` (`dcf.py`, `comps.py`, `run_valuation.py`) over fundamentals in `raw/{date}/fundamentals/`. Produces bull/base/bear **DCF** (CAPM WACC, Gordon terminal value, odd-dimension WACC×terminal-growth sensitivity grid whose center cell equals the base case), peer **comps** with quartile benchmarking, and **margin of safety** vs price → cheap/fair/expensive.

- **Market-aware CAPM** (routed by `profile.market_profile` via `valuation.market_capm`): **Ke = risk_free + β·ERP(mature) + country_risk_premium**. **US** uses the live 10Y (`macro_regime.json` → `raw/macro/DGS10.csv` → config fallback 4.2%), sanity-banded [0.1%, 10%], CRP 0. **Non-US** (CN/HK/JP/UK/EU) uses a static currency-matched risk-free plus a beta-independent CRP — the US 10Y is the wrong currency for a CNY/HKD asset, and a bare swap to a lower local rate would *cut* WACC (a β·ERP bump alone is dampened by low beta), so the flat CRP is the lever giving a low-beta defensive name a sensible discount rate (CN target WACC ~8–9%). Provenance in `macro_inputs.risk_free_source` (`DGS10` | `^TNX` | `config_fallback` | `config_market:{MARKET}`)
- `confidence` is **derived from the included method candidates** (`_component_confidence`), not a peer-count heuristic: a low-confidence component carrying material weight caps the blend, so a fragile DCF can't make the fair value read "high"
- **Divergence-exclude guard** (`dcf_comps_divergence_cap`, default `2.0`): when DCF base intrinsic value > cap × comps anchor, the DCF is **excluded** from the fair-value anchor (weight 0, confidence low, `excluded_from_fair_value`) — not merely down-weighted — so an inflated low-beta DCF can no longer pull blended fair value above price and read "cheap". Headline `fair_value`/`margin_of_safety`/`verdict` then reflect the comps anchor; the DCF is retained as an `included:false` candidate and as the upper tail of `fair_value_range`, so upside is never hidden. (`dcf_comps_divergence_weight` is legacy.) The PDF rating box renders these JSON fields faithfully — the renderer stays read-only, so fixes live here, not in the template
- Free-tier and self-degrading: ETFs/funds → `applicable: false`; sparse data → `confidence: "low"` + `inputs_missing` (never aborts the pipeline)

Then `scripts/build_shared_context.py` bundles `shared_context/{date}.json` (quant + valuation + profile + peers + catalysts + macro_regime + intraday + chips + peer_divergence + investor).

Output: `valuation_summary.json`, `comps.csv`, `dcf_sensitivity.csv`

### 6. Multi-Analyst Discussion (Debate Loop)

**Phase 1 — Independent memos (parallel).** Each analyst reads evidence cards, quant summary and profile, but NOT each other's work → `discussion/analyst_memos/{role}.md`:
- **mm-company-analyst**, **mm-risk-analyst**, **mm-market-analyst**
- **mm-chips-analyst** — the 筹码博弈 lens: chip structure, 筹码干不干净 (holder count / unlock / LHB seats), who is buying/selling, volume-price verdict, chip S/R, and the **operator's-view walkthrough** (if I ran serious size here: 建仓/吸筹/拉升/出货/不碰 — and which leg is the tape on?)

Every memo must contain: core thesis, 3–5 supporting points, biggest uncertainty, time-horizon judgment, a **Story & Game** read (what story, big enough for this cap tier, how certain, where is the telling), and an **Off-Template Factors** answer — where this stock does NOT fit the framework and what edge factor could drive a violent move (honest "none visible" allowed; invention not). All memos land the conclusion on the investor's declared horizon (`shared_context.investor`).

**Phase 2 — Discussion panel loop.** Each round:
1. **Views (parallel)** — every active role files a structured view via **mm-discussion-panelist**: stance (bullish/neutral/bearish), conviction self-rating (0–1), core claims, explicit challenges → `discussion/{date}/panel/round_{N}/{role}_view.json`
2. **Chair tally** — **mm-discussion-moderator** (`tally` mode) → conviction-weighted lean + retained dissent → `panel_summary_round_{N}.json`
3. **Convergence (deterministic)** — `eval/graders/discussion_convergence_grader.py` scores convergence and decides iterate-vs-exit, auto-exiting at `max_rounds` → `convergence_round_{N}.json`

**Anti-conformity guards** (both panels): exact ties never converge (`tie_between`); a stance/vote flip without a cited cause carries half conviction (`uncited_flips`); a round-over-round conviction collapse suppresses an early "converged" exit (`conviction_collapse`); a below-threshold score that stops moving exits `stalled` + `unresolved_dissent: true`; round-1 perfect unanimity is untested consensus — the grader holds one more round (`unanimity_challenge`) and names the lowest-conviction role `devils_advocate` to steelman the opposing case while keeping their honest stance.

Roles carry a configurable **risk mandate** (`discussion.analyst_risk_profiles`, `risk_averse` | `risk_neutral`, default risk_analyst → risk_averse): an asymmetric loss statement shaping what a role weighs, never how it expresses conviction.

**Phase 3 — Synthesis.** **mm-discussion-moderator** (`synthesis` mode) reads memos + all views + tallies + convergence verdict →
- `thesis_map.json` — consensus, disagreements, bull/bear cases, key risks, unsupported claims, writer guidance, and **`unconventional_factors[]`**: every role's `anomaly_watch`/off-template answer carried **verbatim and attributed**, never merged into consensus (the moderator's synthesis instinct is the failure mode this guards against — the factors driving violent repricings are the ones a standard framework normalizes away)
- `story_map.json` — `story` (one sentence), `size`, `certainty` (confirmed / high_probability / hazy_but_coming / pure_theme), `stage` (untold / starting / fermenting / consensus / realized / falsified), `teller`, `verification_date`, `falsifier`, `market_disagreement`
- `debate_summary.md` — where analysts agreed and disagreed, and why

Config: `discussion.panel` (`enabled`, `min_rounds`, `max_rounds`, `convergence_threshold`, `conviction_collapse_ratio`, `stall_epsilon`, `devils_advocate_round`). `enabled: false` → memos feed synthesis directly.

### 7. Draft Report
**mm-report-writer** generates the daily/weekly report from evidence cards, quant summary, thesis map, profile. CN/HK reports lead with Story & Game + chips and keep the (reference) valuation section brief.

### 8. Review Loop
**mm-report-reviewer** scores factuality, evidence_coverage, decision_quality. Below threshold → `revision_brief.json` → writer rewrites targeted sections. Max loops from config (default 3).

### 9. Investment Decision (Panel Debate Loop)

Default is a multi-round **decision panel** (legacy single-shot when `decision.panel.enabled: false`). Each round:
1. **Ballots (parallel)** — every role in `discussion.analyst_roles` casts a ballot via **mm-decision-panelist**: vote (BUY/HOLD/SELL), conviction (0–1), hedge `risk_overlay` (none/hedge/trim/stop), a `main_force_view` (accumulate/absorb/mark_up/distribute/avoid) and a required `anomaly_watch` → `decision/{date}/panel/round_{N}/{role}_ballot.json`
2. **Chair tally** — **mm-decision-maker** (`tally` mode) → `panel_summary_round_{N}.json`
3. **Convergence** — `eval/graders/panel_convergence_grader.py`, same guards as above → `convergence_round_{N}.json`

**mm-decision-maker** (final mode) then writes `final_decision.json`: BUY/HOLD/SELL + `risk_overlay`, confidence score, conviction-weighted panel lean (not head-count), top reasons, key risks, disconfirming signals, a `main_force_read` block (`unclear` when thin), the investor `horizon`, and a `panel` block (rounds, final tally, convergence score, retained dissent).

All evidence-usage contracts above bind this stage — see the contracts table.

Config: `decision.panel` (same knobs as the discussion panel, plus `overlay_labels`).

### 10. Export
Final markdown + structured JSON to `final/`. **mm-pdf-exporter** renders annotated SVG charts (`templates/charts.py`) then markdown → HTML/CSS → PDF via **WeasyPrint** (committed `report.css` + `report.html.j2`; no LaTeX): page-1 rating box from JSON, embedded charts, styled tables, running headers/footers. Output: `exports/{date}/pdf/report.pdf` (+ `report.html` for debugging, `charts/*.svg`).

### 11. Reflect (Non-Critical)
Run code-based graders, finalize `logs/run_log.jsonl`, and run **mm-memory-writer** to store episodic/semantic/procedural memories (including user-review feedback). Failure here still counts the pipeline as complete.

---

## Workspace Structure

Time-sensitive data lives under `{YYYY-MM-DD}/` date folders; static reference data (profile, config) stays undated.

```text
workspaces/
  shared/market_context/{date}/
    raw/macro/                  # FRED series CSVs + macro_sources.json provenance
    indicators/macro_regime.json

  {TICKER}/
    config.yaml  resolved_config.json  status.json      # undated
    profile/                                            # undated
      company_profile.json      # incl. yf_ticker (canonical), float_shares, cap_tier
      peer_set.json             # 5–10 peers with product_niche + differentiation
      investor_profile.json     # horizon / edge_hypothesis / position_state
      market_context_link.json

    raw/{date}/                 # news, filings, prices, fundamentals, chips/cn_flows.json,
                                #   ownership, calendar, diagnostics
    normalized/{date}/          # evidence_cards, time_series, tables
    quant/{date}/               # technical_indicators.csv, relative_strength.csv,
                                #   quant_summary.json, intraday_timing.json,
                                #   chip_structure.json, peer_divergence.json
    valuation/{date}/           # valuation_summary.json, comps.csv, dcf_sensitivity.csv
    discussion/{date}/          # analyst_memos/, panel/round_{N}/, thesis_map.json,
                                #   story_map.json, debate_summary.md
    drafts/{date}/              # daily_v1.md
    reviews/{date}/             # final_reviews/, revision_briefs/, score_history.json
    decision/{date}/            # final_decision.json,
                                #   panel/round_{N}/{role}_ballot.json,
                                #   panel_summary_round_{N}.json, convergence_round_{N}.json
    final/{date}/               # {daily|weekly}_report.md + .json (basename per run_mode)
    exports/{date}/             # pdf/, web/
    shared_context/{date}.json  # per-run bundle (see Stage 5)
    memory/{date}_{role}.json   # role ∈ analyst | writer | reviewer
```

## Status Tracking

`status.json` carries `stage`, `run_date`, `started_at`, `updated_at`, `ticker`, `run_mode`, `stages_completed[]`, `current_review_loop`, `errors[]`. The `run_date` (YYYY-MM-DD) determines which date subdirectory every stage reads from and writes to; the orchestrator sets it at pipeline start and passes it to every skill.

## Config System

Merge order (later overrides earlier): `config.example.yaml` (defaults, always present) → `config.yaml` (local, real API keys, gitignored, used if it exists) → `workspaces/{TICKER}/config.yaml` → runtime overrides.

## Data Sources (V1 — Free Tier)

| Source | Key | Used For |
|---|---|---|
| yfinance | none | Prices, index/peer/macro assets, 1h intraday bars, macro proxies (^TNX/^FVX/^TYX/^IRX/DX-Y.NYB/^VIX) when FRED is keyless |
| NewsAPI | `NEWSAPI_KEY` | Market, sector, company news |
| akshare | none | CN chip/flow signals (换手率, 主力资金流, 龙虎榜, 北向持股, 融资余额, 股东户数, 解禁队列); per-block degradation |
| SEC EDGAR | none | Filings (10-K/10-Q/8-K), insider transactions |
| FRED | key required | CPI, Fed funds, Treasury curve, USD index, HY spread, VIX → macro_regime + live DCF risk-free |
| NASDAQ | none (`api.nasdaq.com`, unofficial) | US-name news + quote; falls back to nasdaq.com pages |
| Web search | — | Provenance-tagged web news, any market |

Keys are configured via env vars named in `config.yaml` under `data_sources.*.api_key_env`.

## MCP Servers (`.mcp.json` registers all three)

- **market-data-mcp** (`mcp/market_data_server.py`) — rate-limited external data with fallbacks: `get_price_history`, `get_news`, `get_filings`, `get_macro_series`, `get_company_info`, `get_fundamentals`, `get_earnings_calendar`
- **mm-workspace-mcp** (`mcp/workspace_server.py`) — artifact I/O with path-traversal protection: `write_artifact`, `update_status`, `create_workspace`, `create_date_dirs`; resources `workspace://{ticker}/{path}`
- **memory-mcp** (`mcp/memory_server.py`) — `store_memory`, `search_memory`, `get_entity_timeline`, `update_memory`, `prune_memories`; resources `memory://index|entity/{name}|recent`

## Memory Layer

Three types stored as append-only JSONL at `memory/{type}/index.jsonl`:

| Type | Purpose | Lifecycle |
|---|---|---|
| Episodic | Per-run decision + key themes | 1 per run, never expires |
| Semantic | Persistent company/sector beliefs | Evolves; old superseded by new |
| Procedural | Process learnings from errors | Never expires |

`memory/retrieval.py` scores by `importance × confidence × recency_decay(days)` and returns top-k — injected before analyst memos (episodic + semantic), report writing (procedural + recent episodic) and review (procedural). Memory supplements, never replaces, current evidence.

## Evaluation Layer (`eval/`)

| Grader | Checks |
|---|---|
| `factuality_grader.py` | Report numbers match quant_summary + valuation_summary |
| `evidence_grader.py` | High-materiality cards (≥0.7) cited in report |
| `consistency_grader.py` | Decision aligns with thesis_map consensus (and panel majority when the panel ran) |
| `language_purity_grader.py` | One language per run; no bilingual literals; Chinese gated on `market_profile` |
| `discussion_convergence_grader.py` | Discussion-panel convergence; drives discuss-stage exit; anti-fake-consensus guards |
| `panel_convergence_grader.py` | Decision-panel convergence; drives decide-stage exit; same guards |
| `valuation_grader.py` | Math consistency (WACC>g, TV band, sensitivity center == base, intrinsic **and** `fair_value_range` ordered low≤base≤high, margin of safety, risk-free ∈ [0.1%, 10%]). Warns if stated `confidence` exceeds what included candidates support; if a live 10Y existed but the DCF used the config rate; if the stored rate drifted >5bp from live; if a non-US/non-USD name used a US risk-free (expect `config_market:{MARKET}`); or if `verdict` reads "cheap" while price sits at/above every included comps anchor |
| `decision_risk_grader.py` | Advisory confidence ceiling — flags final `confidence` above what reproducible signals support (weak convergence, retained dissent, low-confidence valuation cited, thin evidence, stalled panel, round-1 near-unanimity); warns on intraday cited as a vote reason and on valuation in `top_reasons` when role is `reference`. Non-mutating |
| `story_grader.py` | Story map substantive (enums + non-placeholder reasoning + falsifier) and `thesis_map.unconventional_factors` present. Advisory |
| `cost_tracker.py` | Token/cost estimation per run |

`logs/run_log.jsonl` — append-only, one entry per completed run (stage timings, review scores, grader results, decision, cost). Dashboards: `.venv/bin/python3 eval/metrics.py --ticker AMD --format markdown`.

## Context Governance

```text
raw doc → evidence card → evidence_digest → thesis_map → decision capsule
```

Each layer is ~5–10× smaller; downstream agents read the most compressed form sufficient for the task. Patterns in use: compact structured panel views instead of N×N prose critiques; one consolidated evidence digest; one shared-context bundle; per-round chair tallies carried into synthesis; targeted revision briefs instead of full rewrites.

## Skill Inventory

| Tier | Model | Skills |
|---|---|---|
| mm-heavy | claude-opus-5 | `mm-orchestrator` (user-invocable; pipeline driver), `mm-discussion-moderator` (chair: tally + synthesis → thesis_map/story_map), `mm-report-reviewer`, `mm-decision-maker` (chair: tally + final decision) |
| mm-standard | claude-opus-5 | `mm-init` (user-invocable), `mm-company-resolver`, `mm-market-analyst`, `mm-company-analyst`, `mm-risk-analyst`, `mm-chips-analyst` (筹码博弈 memo + operator's-view), `mm-catalyst-analyst`, `mm-valuation-analyst`, `mm-discussion-panelist`, `mm-report-writer`, `mm-decision-panelist`, `mm-pdf-exporter` |
| mm-light | claude-sonnet-5 | `mm-market-desk`, `mm-company-desk`, `mm-sector-desk`, `mm-web-research`, `mm-quant-analyst`, `mm-valuation-engine`, `mm-memory-writer`, `mm-progress-monitor` |

## Quality Gate

Thresholds (configurable in `config.yaml`): overall score ≥ **8.0**, factuality ≥ **9.0**. Blocker policy `hard_fail` — any blocker (ungrounded claim, wrong time window, data contradicting text) fails the draft immediately. Max revision loops: 3 (`review.max_revision_loops`).

## Artifact Contract

All inter-stage exchange is JSON files in the workspace: evidence cards, `quant_summary.json`, `valuation_summary.json`, `thesis_map.json`, review outputs, `final_decision.json`, panel ballots. Complete schemas: `market_report_agent_codex_spec.md` §11.1–11.6.

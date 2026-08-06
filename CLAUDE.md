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

**Three language rules — they apply to every skill and every artifact:**

1. **One language per run.** A report, memo or artifact that mixes English and Chinese narrative is a defect, not a style choice.
2. **Never emit a bilingual literal.** Headings, enum glosses and table labels carry exactly one language. Do not write `## Story & Game (故事与博弈)` — that single atomic string is how Chinese reached an English report: a model copies the nearest literal, and a directive twenty lines above never outranks it. Each skill carries a **Language Map** table with copy-ready single-language cells; substitute from it.
3. **Always English in both languages:** JSON keys, enum VALUES (`hazy_but_coming`, `independent_up`, `accumulate`), evidence-card ids, file paths, tickers, indicator names, and BUY/HOLD/SELL.

A-share jargon (量比 / 龙虎榜 / 北向 …) is gated on **`market_profile`, not `language`** — on an `en` run for a CN/HK name the Chinese source term may appear parenthetically as data provenance on first mention; on an `en` run for a US/JP/UK/EU name the report contains **zero Chinese characters**. `eval/graders/language_purity_grader.py` enforces this.

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
Both drivers first run `scripts/check_data_sources.py`, a non-critical preflight that records key presence (never values), DNS reachability, per-source status (`auth_ok`/`dns_failed`/`auth_failed`/`rate_limited`/`no_key`), and the **macro collection plan** (`macro.mode`: fred vs yfinance_proxy, with per-series routing) to `raw/{date}/diagnostics/data_sources.json` — so a fallback-heavy run self-explains. (Live data requires network egress: Codex's `workspace-write` sandbox needs `[sandbox_workspace_write] network_access = true`; the driver sets it per stage.)

**Then the deterministic macro layer runs** (non-critical, before the desks):
- `scripts/collect_macro_series.py` — FRED-first collection of CPI (CPIAUCSL/CPILFESL), FEDFUNDS, the Treasury curve (DGS2/5/10/30), DTWEXBGS, HY OAS (BAMLH0A0HYM2), VIXCLS into `workspaces/shared/market_context/{date}/raw/macro/`; keyless runs self-degrade to yfinance proxies (^IRX/^FVX/^TNX/^TYX/DX-Y.NYB/^VIX, treasury quotes ÷10 to FRED percent units) and record CPI/FedFunds/HY in `inputs_missing` (provenance: `macro_sources.json`)
- `scripts/compute_macro_regime.py` — deterministic classification → `indicators/macro_regime.json`: rate trend, 2s10s curve slope, inflation trend, Fed policy stance, VIX 1y percentile, USD trend, credit regime — every block with `data_quality` (fred/proxy/missing), plus `summary_i18n` (en+ch cache — the artifact is shared across same-day workspaces that may disagree on language, so `build_shared_context.py` resolves it into `shared_context.macro_regime.summary` per workspace; read it from the bundle, not from here)
- `scripts/macro_evidence_cards.py` — projects material observations (inverted curve, VIX ≥80th pct, wide/stressed spreads, ≥25bp 10Y moves, easing/tightening, ≥2% USD moves) into per-ticker evidence cards `ev_{date}_macro_*` so macro enters the citable audit trail (benign regime → zero cards)

**Project rule — macro is context, not trigger**: the regime feeds the WACC input, analyst framing, and the risk overlay; it never deterministically flips a vote or caps conviction (same precedent as valuation-as-reference).

- `scripts/collect_cn_chips.py` — **A-share chip/flow collection via akshare** (CN names only, non-critical): 换手率 (`stock_zh_a_hist`), 主力资金流 (`stock_individual_fund_flow`), 龙虎榜 (`stock_lhb_detail_em`), 北向持股 (`stock_hsgt_individual_em`), 融资余额 (`stock_margin_detail_sse/szse`), 股东户数 (`stock_zh_a_gdhs_detail_em`), 解禁队列 (`stock_restricted_release_queue_em`) → `raw/{date}/chips/cn_flows.json`. Every block carries its own `data_quality` (`akshare` | `unavailable`) and degrades independently (eastmoney hosts drop connections intermittently — expected); akshare missing → stub file. Non-CN markets skip.

Then run these desks in parallel:

- **mm-market-desk**: Macro headlines, index data, macro asset prices (yfinance, FRED)
- **mm-company-desk**: Company news, SEC filings, catalyst calendar (NewsAPI, EDGAR)
- **mm-sector-desk**: Sector news, peer price data (NewsAPI, yfinance)
- **mm-web-research**: Web-sourced news with provenance (Claude WebSearch/WebFetch); for US names pulls from **NASDAQ** (`api.nasdaq.com` first, `nasdaq.com` pages as fallback)

**Source hierarchy** (institutional/MCP > NewsAPI > NASDAQ for US > general web search): the desks own the top tiers, mm-web-research owns the lower tiers and captures source URL/date/excerpt per item. The normalize stage deduplicates cards across all four collectors (canonical URL + title) before building the evidence digest.

### Stage 3: Normalize
- **First the deterministic chip layer runs** (non-critical, before dedup — it needs the desks' raw price CSVs and its cards must be in the digest):
  - `scripts/compute_chip_structure.py` → `quant/{date}/chip_structure.json`: volume regime (vol MAs, 量比 daily proxy, up/down-day volume, OBV trend + price divergence, CMF(20), 换手率 when float or akshare turnover is known), **VPVR chip distribution** (volume-at-price histogram over ~120d → main/secondary cost peak, 90% cost band, concentration, profit/trapped ratio, VWAP cost), **support/resistance** (volume nodes + swing pivots, with strength), **platform/box detection** (range width, streak, breakout with/without volume), and the embedded `cn_flows` (akshare blocks). Prefers the run-day raw CSV over a live re-fetch so chip levels match the report's captured prices; suspension (zero-volume) bars dropped
  - `scripts/chip_evidence_cards.py` → projects material chip observations into citable cards `ev_{date}_chip_*` (holder-count concentrating/dispersing, main-force inflow/outflow streaks, LHB appearances, northbound adds/trims, unlock overhang ≤60d, volume-confirmed breakout/breakdown, OBV divergence, extreme profit ratio). Card titles embed numbers (dedup clusters URL-less cards by title). Quiet tape → zero cards
- **Project rule — chips are DIRECTIONAL** (`usage: "directional"`): unlike macro (context), intraday (timing-only) and trend_regime (context-only), chip/volume structure is **first-class stance evidence** — analysts and panelists MAY ground a stance or vote in it alone. This asymmetry is deliberate: chip exchange is the reproducible trace of actual buying/selling force. Per-block `data_quality` must be respected — an `unavailable` block supports nothing
- Convert raw data into evidence cards (JSON)
- Build time-series tables
- Deduplicate overlapping news items

### Stage 4: Quant Snapshot
- First, the deterministic `scripts/intraday_timing.py` (driver-run, non-critical) writes `quant/{date}/intraday_timing.json`: 1h/4h RSI(14) + MACD(12,26,9) on ~90d of yfinance hourly bars, last confirmed 4h swing high/low, 30d range, and a `timing_state` label. **Timing-only contract** (`usage: "timing_only"`): it frames staged entry/exit price zones and risk-overlay language downstream — never a reason to flip a BUY/HOLD/SELL vote or change conviction (the decision-risk grader warns on violations). Tickers without 1h coverage → `available: false`; zones fall back to chip S/R + daily ATR. Symbol resolution goes through `contracts.resolve_yf_symbol()` (canonical key `yf_ticker`, exchange-suffixed).
- Then `scripts/peer_divergence.py` (non-critical) → `quant/{date}/peer_divergence.json`: for the target + every peer with a price CSV, 20/60/120/250d returns, benchmark/target correlations, and a deterministic **path class** — `follows_sector` 跟随板块 / `independent_up` 独立走强 / `independent_down` 独立走弱 / `basing` 横盘蓄势 / `launched` 已启动 (outcome-gap beats co-movement; a spliced/mis-adjusted benchmark file with a >30% daily "bar" is discarded) — plus cohort dispersion and the 60d leader/laggard
- **mm-quant-analyst** computes technical indicators via Python/pandas
- RSI(14), MACD(12,26,9), SMA(20,50), EMA(12,26), ATR(14)
- Return windows: 1d, 5d, 1m, 3m
- Relative strength vs index, sector, peers
- **No golden/death-cross flags are emitted** — MA crossovers carry no meaning for institutional readers; the SMAs exist only to feed `trend_regime`, and the PDF price chart annotates chip S/R + cost band instead of crossovers
- A deterministic **`trend_regime`** block in `quant_summary.json` pre-digests the daily SMAs into ONE bounded label (`label` uptrend/downtrend/transition/range, from the SMA20/50 stack + SMA50 slope + `price_vs_sma50_pct`). **Context-only contract** (`usage: "context_only"`): analysts/panelists consume the single `trend_regime.label` as trend backdrop — moving averages never set, flip, or cap a stance/vote (same precedent as macro = context, intraday = timing-only, valuation = reference). It propagates to `shared_context.quant.trend_regime` via the bundler (whole quant summary loaded)
- Output: `quant/{date}/technical_indicators.csv`, `quant/{date}/quant_summary.json`, `quant/{date}/peer_divergence.json`

### Stage 5: Valuation (Scenario DCF + Comps)
- **mm-valuation-engine** runs the formula-first engine in `valuation/` (`dcf.py`, `comps.py`, `run_valuation.py`)
- Inputs: yfinance fundamentals collected by the company desk (`raw/{date}/fundamentals/`), via the `get_fundamentals` MCP tool
- Computes a bull/base/bear **DCF** (CAPM WACC, Gordon terminal value, odd-dimension WACC×terminal-growth sensitivity grid whose center cell equals the base case), peer **comps** with quartile benchmarking, and a **margin of safety** vs the current price → verdict cheap/fair/expensive
- CAPM inputs are **market-aware** (routed by `profile.market_profile`, via `valuation.market_capm`): **Ke = risk_free + β·ERP(mature) + country_risk_premium**. **US** uses the **live 10Y** from the shared macro layer (`macro_regime.json` → `raw/macro/DGS10.csv` → config fallback 4.2%), sanity-banded [0.1%, 10%], with CRP 0. **Non-US** names (CN/HK/JP/UK/EU) use a **static, currency-matched risk-free plus a beta-independent `country_risk_premium`** — the US 10Y is the wrong currency for a CNY/HKD asset, and because a bare risk-free swap to a lower local rate would *cut* WACC (and a β·ERP bump alone is dampened by low beta), the flat CRP is the lever that gives a low-beta defensive name a sensible discount rate (CN target WACC ~8–9%; no reliable free live non-US source — same v1 precedent as ERP/cost_of_debt). Provenance in `macro_inputs.risk_free_source` (`DGS10` | `^TNX` | `config_fallback` | `config_market:{MARKET}`); `summary.meta` carries `market_profile`/`currency`. After the engine, `scripts/build_shared_context.py` bundles `shared_context/{date}.json` (quant + valuation + profile + peers + catalysts + `macro_regime` + `intraday` + `chips` + `peer_divergence` + `investor`) in both drivers
- The summary `confidence` is **derived from the included method candidates** (`_component_confidence`), not a peer-count heuristic alone: a low-confidence DCF (or any low-confidence component carrying material weight) caps the blend, so a fragile DCF can no longer make the fair value read "high"
- **Divergence-exclude guard** (`dcf_comps_divergence_cap`, default `2.0`): when the DCF base intrinsic value exceeds the cap × the comps blended anchor, the DCF is **excluded from the fair-value anchor** (weight 0, confidence low, `excluded_from_fair_value`), not merely down-weighted — so a low-beta WACC inflating a CNY/low-beta name (DCF ≫ comps) can no longer pull a blended fair value above the price and read "cheap". The headline `fair_value`/`margin_of_safety`/`verdict` then reflect the reliable comps anchor; the DCF is retained as an `included:false` candidate and as the upper tail of `fair_value_range` (its bull scenario), so the upside is never hidden. (`dcf_comps_divergence_weight` is deprecated/legacy under the exclude path.) The page-1 PDF rating box renders these JSON fields faithfully (renderer stays read-only), so the fix lives here, not in the template
- **Valuation role by market** (`valuation.role_by_market`, stamped into `summary.role`): **US/JP/UK/EU → `anchor`** (full weight in the thesis), **CN/HK → `reference`** — in game-driven markets the story + chips + fundamentals carry the label; a `reference` valuation is a value floor / sanity read that may shape confidence and risk framing but must NOT appear in `final_decision.top_reasons` (the decision-risk grader warns on violations; the PDF rating box labels it 估值参考（非锚）; the report writer keeps the section brief and re-orders CN/HK reports to lead with Story & Game + chips)
- Free-tier and self-degrading: ETFs/funds → `applicable: false`; sparse data → `confidence: "low"` with an `inputs_missing` list (never aborts the pipeline)
- Output: `valuation/{date}/valuation_summary.json`, `valuation/{date}/comps.csv`, `valuation/{date}/dcf_sensitivity.csv`
- Consumed downstream by mm-valuation-analyst, mm-decision-maker (margin of safety → conviction), and mm-report-writer (Valuation section)

### Stage 6: Multi-Analyst Discussion (Debate Loop)

Follows a structured debate protocol inspired by multi-agent collaboration:

**Phase 1 — Independent Memos (parallel)**
All 3 analysts run in parallel. Each reads evidence cards, quant summary, and company profile, but NOT each other's work:

- **mm-company-analyst** → `discussion/analyst_memos/company_analyst.md`
- **mm-chips-analyst** → `discussion/analyst_memos/chips_analyst.md` — the 筹码博弈 lens: chip structure, 筹码干不干净 (holder count / unlock / LHB seats), who is buying/selling, volume-price verdict, chip S/R, and the **operator's-view walkthrough** (if I ran serious size here, would I 建仓/吸筹/拉升/出货/不碰 — and which leg is the current tape on?)
- **mm-risk-analyst** → `discussion/analyst_memos/risk_analyst.md`
- **mm-market-analyst** → `discussion/analyst_memos/market_analyst.md`

Each memo must contain: core thesis, 3–5 supporting points, biggest uncertainty, time horizon judgment, a **Story & Game** read (what story, big enough for this cap tier?, how certain, where is the telling), and an **Off-Template Factors** answer — where this stock does NOT fit the framework and what edge factor could drive a violent move (honest "none visible" allowed; invention not). All memos land the conclusion on the investor's declared horizon (`shared_context.investor`).

**Phase 2 — Discussion Panel Loop**
A multi-round panel, modeled on the decision panel (Stage 9). Each round:
1. **Views (parallel)** — every active analyst role files a ballot-style structured
   view via **mm-discussion-panelist**: a directional **stance** (bullish/neutral/
   bearish), a **conviction** self-rating (0–1), core claims, and explicit
   challenges to the other roles → `discussion/{date}/panel/round_{N}/{role}_view.json`.
2. **Chair tally** — **mm-discussion-moderator** (`tally` mode) tallies stances with a
   conviction-weighted lean and surfaces retained dissent →
   `panel_summary_round_{N}.json`.
3. **Convergence (deterministic)** — `eval/graders/discussion_convergence_grader.py`
   computes a conviction-weighted convergence score and decides iterate-vs-exit; it
   **auto-exits at `max_rounds`** (hard cap) → `convergence_round_{N}.json`.
   Anti-conformity guards: exact ties never converge (`tie_between`); a stance
   flip without a cited cause carries half conviction (`uncited_flips`); a
   round-over-round total-conviction collapse suppresses an early "converged"
   exit (`conviction_collapse`); a below-threshold score that stops moving exits
   with `exit_reason: "stalled"` + `unresolved_dissent: true`; round-1 perfect
   unanimity is untested consensus — the grader holds the panel one round
   (`exit_reason: "unanimity_challenge"`) and names the lowest-conviction role
   `devils_advocate` to steelman the opposing case (keeping their honest stance).

Analyst roles carry a configurable **risk mandate** (`discussion.analyst_risk_profiles`,
`risk_averse` | `risk_neutral`, default risk_analyst → risk_averse): an asymmetric
loss statement injected into memo + panelist skills that shapes what a role weighs,
never how it expresses conviction.

Config: `discussion.panel` (`enabled`, `min_rounds`, `max_rounds`,
`convergence_threshold`, `conviction_collapse_ratio`, `stall_epsilon`,
`devils_advocate_round`). `enabled: false` → memos feed synthesis directly.

**Phase 3 — Synthesis**

- **mm-discussion-moderator** (`synthesis` mode) reads the full memos + all panel
  views + per-round tallies + the convergence verdict
- Produces:
  - `discussion/{date}/thesis_map.json` — consensus, disagreements, bull/bear cases, key risks, unsupported claims, writer guidance, and **`unconventional_factors[]`** — every role's `anomaly_watch`/off-template answer carried **verbatim and attributed** (never merged into consensus; the moderator's synthesis instinct is the failure mode this field guards against — the factors that drive violent repricings are the ones a standardized framework normalizes away)
  - `discussion/{date}/story_map.json` — the game layer distilled: `story` (one sentence), `size` (big enough for this cap tier?), `certainty` (confirmed / high_probability / hazy_but_coming / pure_theme), `stage` (untold / starting / fermenting / consensus / realized / falsified), `teller` (company/institutions/media/hot_money), `verification_date`, `falsifier`, `market_disagreement` — checked by `eval/graders/story_grader.py` (warning-only)
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
   Same anti-conformity guards as the discussion panel: ties never converge,
   uncited vote flips carry half conviction, conviction collapse suppresses an
   early exit, a stalled below-threshold panel exits with
   `unresolved_dissent: true`, and round-1 perfect unanimity triggers a
   devil's-advocate challenge round (`unanimity_challenge`).

Each ballot also carries a `main_force_view` (accumulate/absorb/mark_up/
distribute/avoid — the role's operator's-view read of the game) and a required
`anomaly_watch` (the off-template factor the role's framework would miss).

After the loop, **mm-decision-maker** (final mode) produces `final_decision.json`:
- BUY / HOLD / SELL label + `risk_overlay` hedge stance
- Confidence score; conviction-weighted panel lean (not head-count)
- Top reasons, key risks, disconfirming signals, a `main_force_read` block (the
  panel's converged operator's-view read, `unclear` when thin), the investor's
  `horizon`, and a `panel` block (rounds, final tally, convergence score,
  retained dissent)

**Chips are directional; macro is context, not trigger; intraday is timing-only;
SMA/trend is backdrop; valuation is anchor (US) or reference (CN/HK).**
`shared_context.chips` is **first-class directional evidence** — a chip-grounded
argument (sustained main-force accumulation over a concentrating holder base; an
unlock calendar over trapped supply) may stand in `top_reasons` and carry the
label, exactly like fundamentals (deliberate asymmetry; respect per-block
`data_quality`). The macro regime may shape the `risk_overlay` framing and
confidence narrative but never flips the label or caps conviction. The 1h/4h
intraday block (`shared_context.intraday`) only frames staged entry/exit price
zones in `stance_notes` (chip-S/R + daily-ATR fallback when `available: false`)
and must never appear as a vote reason — the decision-risk grader warns on
violations. The daily `shared_context.quant.trend_regime` label is trend
**backdrop**: a price-vs-SMA position or a moving-average cross is never the
sole reason for a stance or vote (prompt-level de-weighting in the analyst memos
and both panels; no grader). When `valuation_summary.role` is `reference`
(CN/HK), valuation must not appear in `top_reasons` at all (grader warns).

Config: `decision.panel` (`enabled`, `min_rounds`, `max_rounds`,
`convergence_threshold`, `conviction_collapse_ratio`, `stall_epsilon`,
`devils_advocate_round`, `overlay_labels`).

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
          macro/                 # FRED series CSVs + macro_sources.json provenance
        normalized/
        indicators/
          macro_regime.json      # deterministic regime (rates/curve/CPI/policy/VIX/USD/credit)

  {TICKER}/
    config.yaml                  # Undated — company config
    resolved_config.json         # Undated — merged config
    status.json                  # Undated — current run state (includes run_date)

    profile/                     # Undated — static company reference
      company_profile.json       #   incl. yf_ticker (canonical), float_shares, cap_tier
      peer_set.json              #   5–10 peers with product_niche + differentiation
      investor_profile.json      #   horizon / edge_hypothesis / position_state (from /mm:init)
      market_context_link.json

    raw/{YYYY-MM-DD}/            # Date-stamped raw data
      news/
      filings/
      prices/
      fundamentals/             # company + peers/ valuation inputs (yfinance)
      chips/                    # cn_flows.json — akshare A-share flow blocks (CN only)
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
      intraday_timing.json       # 1h/4h RSI/MACD + swing levels (timing-only)
      chip_structure.json        # volume/chip structure + cn_flows (DIRECTIONAL)
      peer_divergence.json       # per-peer path classes + cohort dispersion

    valuation/{YYYY-MM-DD}/      # Date-stamped valuation (DCF + comps)
      valuation_summary.json
      comps.csv
      dcf_sensitivity.csv

    discussion/{YYYY-MM-DD}/     # Date-stamped analyst discussion
      analyst_memos/
      debate/round_1/
      debate/round_2/
      thesis_map.json            #   incl. unconventional_factors[] (verbatim)
      story_map.json             #   story / size / certainty / stage / falsifier
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

    shared_context/             # Per-run shared bundle, one file per date
      {YYYY-MM-DD}.json         #   quant + valuation + profile + peers + catalysts
                                #   + macro_regime + intraday + chips + peer_divergence + investor
    memory/                     # Per-run retrieved memory context, one file per date+role
      {YYYY-MM-DD}_{role}.json  #   role ∈ analyst | writer | reviewer
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
| yfinance | Free, no key | Stock prices, index data, peer prices, macro assets, 1h intraday bars, macro proxies (^TNX/^FVX/^TYX/^IRX/DX-Y.NYB/^VIX) when FRED is keyless |
| NewsAPI | Free tier, key required | Market news, sector news, company news |
| akshare | Free, no key | A-share chip/flow signals (CN names only): 换手率, 主力资金流, 龙虎榜, 北向持股, 融资余额, 股东户数, 解禁队列 — feeds `cn_flows` in chip_structure.json; per-block degradation (eastmoney endpoints are intermittently flaky) |
| SEC EDGAR | Free, no key | Company filings (10-K, 10-Q, 8-K), insider transactions |
| FRED | Free, key required | Macro series: CPI (headline+core), Fed funds, Treasury curve (2/5/10/30Y), broad USD index, HY credit spread, VIX — feeds macro_regime.json and the live DCF risk-free rate |
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
| mm-heavy | claude-opus-4-8 | Orchestration, discussion moderation, review, decision |
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

### mm-workspace-mcp (`mcp/workspace_server.py`)
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
| `discussion_convergence_grader.py` | Discussion-panel views converged (conviction-weighted stance agreement); drives the discuss-stage loop exit; guards against fake consensus (ties, uncited flips, conviction collapse, stalled dissent) |
| `panel_convergence_grader.py` | Decision-panel ballots converged (conviction-weighted agreement); drives the decide-stage loop exit; same anti-conformity guards as the discussion grader |
| `valuation_grader.py` | Valuation math is internally consistent (WACC>g, TV band, sensitivity center == base, intrinsic range **and `fair_value_range` both ordered low≤base≤high**, margin of safety, risk-free rate inside [0.1%, 10%]); warns if the stated `confidence` exceeds what the included method candidates support, if a live 10Y was available but the DCF fell back to the config rate, if the stored rate drifted >5bp from the live regime value, if a **non-US/non-USD name was discounted with a US risk-free** (currency mismatch — expected `config_market:{MARKET}`), or if the **`verdict` reads "cheap" while the price sits at/above every included comps anchor** (the cheap read rests on a divergent/excluded DCF, not the reliable anchor) |
| `decision_risk_grader.py` | Advisory confidence ceiling: flags when the final `confidence` exceeds what reproducible risk signals support (weak convergence, retained dissent, low-confidence valuation cited, thin evidence, stalled panel with unresolved dissent, round-1 near-unanimity); also warns (never caps) when a ballot rationale or final top_reason cites intraday 1h/4h indicators as a vote reason (timing-only contract), and when `top_reasons` cites valuation while `valuation_summary.role` is `reference` (CN/HK — story/chips/fundamentals carry the label); non-mutating, warning-only |
| `story_grader.py` | Story map present and substantive (story/size/certainty/stage enums + non-placeholder reasoning + falsifier), and the anti-templating channel intact (`thesis_map.unconventional_factors` key exists); advisory, warning-only |
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
- **Structured panel views**: each round, analysts file compact `*_view.json` (stance + conviction + challenges), not full N×N prose critiques
- **Evidence digest**: All cards consolidated into one file
- **Shared context**: quant + profile + peers + catalysts bundled in one file
- **Round summaries**: chair tally per round (`panel_summary_round_{N}.json`) carries the converged lean into synthesis
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
| mm-chips-analyst | mm-standard | No | Chip structure & game (筹码博弈) memo — volume/flow directional evidence + operator's-view walkthrough |
| mm-discussion-panelist | mm-standard | No | Files one analyst role's structured view (stance + conviction + challenges + anomaly_watch) per discussion-panel round |
| mm-discussion-moderator | mm-heavy | No | Chair: per-round discussion-panel tally + synthesis of memos + panel views → thesis_map + story_map |
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

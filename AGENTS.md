# MarketMind-AlphaEngine — Codex CLI Instructions

This repository was authored for Claude Code (`CLAUDE.md`, `.claude/skills/`,
`.claude/agents/`, `plugin/commands/`). It also runs under **Codex CLI** via
native skills. Codex discovers the MarketMind skills, you operate them with
Codex tools, and the same Python/MCP backend does the real work.

## Running the full pipeline (parity mode — recommended)

To run the whole pipeline at **Claude-parity quality**, use the headless driver
instead of driving the orchestrator inline:

```bash
.venv/bin/python3 scripts/run_codex_pipeline.py workspaces/{TICKER}
#   --dry-run            print the full ordered plan, execute nothing
#   --from STAGE --to S  run a slice / resume
#   --model M            codex model override for LLM stages
```

**Why this matters.** In Claude Code each stage runs as a `context: fork`
subagent — a fresh, full-context turn — so analyst memos and the report come out
deep. Driving the 15 stages **inline in one Codex session shares one context**,
which compresses every stage into stubs (the failure you'd see as "Codex is
worse than Claude"). The driver fixes this structurally: it runs each LLM stage
as its **own `codex exec`** (one task per invocation = fresh context, the exact
analog of Claude's fork), runs the already-deterministic stages
(`valuation/`, `templates/`, `normalize/dedup_evidence.py`, `eval/graders/`)
directly in Python, and fans out the independent stages (collect desks, analyst
memos, discussion-panel views) in parallel. The depth gate
(`eval/graders/depth_grader.py`) runs inside it as a safety net (redo thin memos,
fail thin reports).

Prereqs: `codex` on PATH; the 3 MCP servers in `~/.codex/config.toml`
(see `.agents/references/codex-config.toml`); launch from the repo root;
`export NEWSAPI_KEY=…` for evidence parity. `--dry-run` needs none of these.

Driving the orchestrator interactively (below) still works as a fallback, but
each sub-skill must then be run as its own deep pass — see **Depth parity**.

## Skills (native Codex discovery)

Codex auto-discovers skills under `.agents/skills/`, which is a **symlink to
`.claude/skills/`** — so every `mm-*` skill is a Codex skill. Codex indexes each
skill's `name` + `description` (from its `SKILL.md` frontmatter) and loads the
full `SKILL.md` only when the skill is used. Per-skill `agents/openai.yaml`
sets invocation policy and MCP dependencies.

### Entry points (start here)

Only two skills are **implicitly invocable** (Codex may auto-match them from a
prompt) — they are the user-facing entry points:

- **`mm-init`** — initialize a new company workspace (asks for a ticker,
  verifies, scaffolds `config.yaml`). Full command flow: `plugin/commands/init.md`.
- **`mm-orchestrator`** — run the full 15-stage pipeline for a workspace.
  Full command flow: `plugin/commands/run.md`.

Two more user actions are command files, not skills:

- **Status** — read `plugin/commands/status.md`; inspect `workspaces/*/status.json`.
- **Dashboard** — read `plugin/commands/mm-dashboard.md`; run
  `.venv/bin/python3 web/server.py --port <port>`.

### Internal stage skills (do not run standalone)

The other 20 `mm-*` skills (`mm-quant-analyst`, `mm-company-desk`,
`mm-risk-analyst`, …) are **pipeline stages orchestrated by `mm-orchestrator`**.
Each has `allow_implicit_invocation: false`, so Codex will not auto-fire them —
invoke one explicitly (`$mm-quant-analyst`) only for targeted debugging. They
assume orchestrator context: arguments, prior-stage artifacts under the run's
date folder, and MCP servers. `.agents/skill-index.md` maps stages → skills.

## Codex MCP setup (required for full functionality)

The skills call `mcp__market-data__*`, `mcp__mm-workspace__*`, `mcp__memory__*`.
Codex reads MCP servers from `~/.codex/config.toml` (not `.mcp.json`). Copy the
three `[mcp_servers.*]` tables from **`.agents/references/codex-config.toml`**
into your `~/.codex/config.toml`, then:

- Launch `codex` from the **repository root** (the server `command`/`args` are
  repo-relative), or substitute absolute paths.
- API keys come from the project `.env` (the market-data server self-loads it;
  canonical `NEWSAPI_KEY`, `NEWSAPI_API_KEY` accepted as an alias). Both are
  optional (the desks fall back to web search when unset), but an **unset
  `NEWSAPI_KEY` yields thinner evidence** — set it for collection parity with
  Claude. `mm-web-research` back-fills toward
  `data_sources.web_research.min_cards` regardless.
- **Network egress:** Codex's `workspace-write` sandbox blocks outbound network by
  default. Live data needs `[sandbox_workspace_write] network_access = true`
  (in `~/.codex/config.toml`); the headless driver sets it per stage. Without it,
  desks fall back with `dns_failed`. The collect stage first runs
  `scripts/check_data_sources.py`, writing key-presence/DNS/per-source status to
  `raw/{date}/diagnostics/data_sources.json` (never the key values).

If MCP is not attached, fall back to the local Python servers/modules
(`mcp/*_server.py`) or read/write the workspace files directly.

## Hard rules

- Use `.venv/bin/python3` for every Python command — never bare `python3`.
- `mcp/shared/contracts.py` is the canonical runtime contract for stage names,
  the 15-stage count, artifact paths, report naming, and release semantics.
- Keep all time-sensitive workspace artifacts under `{YYYY-MM-DD}` folders;
  `status.json` owns `run_date` and every dated stage uses that exact folder.
- Preserve English JSON keys, file names, directory paths, and code. Use the
  configured report `language` only for user-facing report text. BUY/HOLD/SELL
  labels stay English even in Chinese reports.
- Reports are research views for human review, not investment advice — preserve
  that boundary. Cite evidence IDs + source dates; keep unsupported valuation
  output low-confidence rather than forcing a DCF.
- Do not rewrite unrelated user changes in this repository.

## Claude → Codex translation

Skill files use Claude conventions; map them as follows:

- **Arguments**: `$ARGUMENTS[0]` = workspace path (e.g. `workspaces/NVDA`),
  `$ARGUMENTS[1]` = `run_date` (`YYYY-MM-DD`). Pass these when running a stage.
- **Arguments**: `$ARGUMENTS[2]` and beyond are stage-specific (e.g. analyst
  memo skills take only `{ws} {date}`, writer mode `initial`/`revision`,
  moderator mode `tally <round>` vs `synthesis`, discussion/decision panelist
  `{role} {round}`).
- **`discuss_debate` is a loop, not a single call.** When `discussion.panel.enabled`
  is true (default), the discuss stage runs a multi-round panel: each
  `discussion.analyst_roles` role files a structured view via
  `mm-discussion-panelist {role} <round>`, `mm-discussion-moderator tally <round>`
  tallies them, and `eval/graders/discussion_convergence_grader.py <ws> <date> <round>`
  (deterministic) decides iterate-vs-exit with a hard `max_rounds` cap. After the
  loop, `mm-discussion-moderator synthesis` writes `thesis_map.json` /
  `debate_summary.md`. With the panel disabled the memos feed synthesis directly
  (no cross-critique). The headless driver encodes this loop in `st_discuss_debate`.
- **`decide` is a loop, not a single call.** When `decision.panel.enabled` is
  true, the decide stage runs a multi-round panel: each `discussion.analyst_roles`
  role casts a ballot via `mm-decision-panelist`, `mm-decision-maker tally <round>`
  tallies them, and `eval/graders/panel_convergence_grader.py <ws> <date> <round>`
  (deterministic) decides iterate-vs-exit with a hard `max_rounds` cap. After the
  loop, `mm-decision-maker` (final) writes `final_decision.json`. With the panel
  disabled it is a single `mm-decision-maker` call (legacy). The headless driver
  `scripts/run_codex_pipeline.py` already encodes this loop in `st_decide`.
- **The decision is risk-gated after the fact.** In the reflect stage,
  `eval/graders/decision_risk_grader.py` computes a deterministic confidence
  *ceiling* from reproducible signals (weak panel convergence, retained dissent,
  low-confidence valuation cited as a reason, thin evidence) and flags when the
  chair's stated `confidence` exceeds it. It is **advisory and non-mutating** —
  it never rewrites `final_decision.json`; an over-ceiling result makes the
  release gate a *warning*, never *failed*. Both drivers run it (`st_reflect` /
  the orchestrator reflect stage).
- **Valuation confidence is derived, not asserted.** `valuation/run_valuation.py`
  sets the summary `confidence` for `dcf`/`comps_earnings`/`blended` from the
  included method candidates (`_component_confidence`): a low-confidence DCF — or
  any low-confidence component carrying material weight — caps the blend, so a
  fragile DCF can't make the fair value read "high". `valuation_grader.py` warns
  if the stated confidence ever exceeds what the components support.
- **Tools**: `Read`/`Glob`/`Grep`/`Bash`/`Write`/`Edit` → Codex shell tools +
  `apply_patch`; `rg` for search. `TodoWrite` → Codex plan/status updates.
  `WebSearch`/`WebFetch` → Codex web browsing. `mcp__*__*` → the `config.toml`
  servers above (or local-Python fallback). `Skill` → read and follow the
  referenced `SKILL.md`.
- **`Agent` (forked sub-skills) → a dedicated, full-depth pass — NOT an inline
  shortcut.** This is the single most important translation. In Claude each
  desk/analyst/writer runs as a `context: fork` subagent: a fresh, full-context
  turn that loads the entire `SKILL.md` and produces a deep artifact. Replicate
  that: for every dispatched sub-skill, **explicitly invoke it** (`$mm-…` or a
  Codex subagent) so its full `SKILL.md` loads, give it your full attention, and
  emit the **complete artifact the skill specifies**. Do **not** inline-stub,
  batch, or summarize sub-stages.
- Claude model-tier files in `.claude/agents/` describe task complexity only;
  they do not select Codex models.

## Depth parity (do not produce shallow output)

A Codex artifact must match the depth a forked Claude subagent would produce —
running every stage is not enough if each is a stub. In particular:

- **Analyst memos** are multi-paragraph arguments (typically 25–50 lines /
  ≥1,200 chars each): a core thesis, 3–5 *distinct* supporting points each its
  own paragraph with specific numbers and an `ev_…` id where relevant, the
  biggest uncertainty, and the time-horizon judgment — never 2–3 sentences.
- **The report** develops every section with substance; no stub sections.
- The deterministic floor is `eval/graders/depth_grader.py` (thresholds in
  `config.review.depth`). The orchestrator runs it after the memos and at review
  and **redoes thin work**; the release gate flags it. Treat a depth failure as
  a real defect, not noise — expand the thin artifact and re-run.

## Common verification commands

```bash
.venv/bin/python3 -m pytest -q
.venv/bin/python3 scripts/contract_check.py
.venv/bin/python3 eval/metrics.py --format markdown
.venv/bin/python3 valuation/run_valuation.py workspaces/{TICKER} {YYYY-MM-DD}
.venv/bin/python3 templates/render_pdf.py workspaces/{TICKER} {YYYY-MM-DD}
```

Run only the commands that fit the task and available inputs.

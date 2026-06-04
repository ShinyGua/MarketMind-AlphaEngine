# MarketMind-AlphaEngine — Codex CLI Instructions

This repository was authored for Claude Code (`CLAUDE.md`, `.claude/skills/`,
`.claude/agents/`, `plugin/commands/`). It also runs under **Codex CLI** via
native skills. Codex discovers the MarketMind skills, you operate them with
Codex tools, and the same Python/MCP backend does the real work.

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

The skills call `mcp__market-data__*`, `mcp__workspace__*`, `mcp__memory__*`.
Codex reads MCP servers from `~/.codex/config.toml` (not `.mcp.json`). Copy the
three `[mcp_servers.*]` tables from **`.agents/references/codex-config.toml`**
into your `~/.codex/config.toml`, then:

- Launch `codex` from the **repository root** (the server `command`/`args` are
  repo-relative), or substitute absolute paths.
- `export NEWSAPI_KEY=… FRED_API_KEY=…` first (both optional; desks fall back to
  web search when unset).

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
- **Tools**: `Read`/`Glob`/`Grep`/`Bash`/`Write`/`Edit` → Codex shell tools +
  `apply_patch`; `rg` for search. `TodoWrite` → Codex plan/status updates.
  `Agent` (forked sub-skills) → run the referenced stage directly, or use
  current-session subagent tooling if exposed. `WebSearch`/`WebFetch` → Codex
  web browsing. `mcp__*__*` → the `config.toml` servers above (or local-Python
  fallback). `Skill` → read and follow the referenced `SKILL.md`.
- Claude model-tier files in `.claude/agents/` describe task complexity only;
  they do not select Codex models.

## Common verification commands

```bash
.venv/bin/python3 -m pytest -q
.venv/bin/python3 scripts/contract_check.py
.venv/bin/python3 eval/metrics.py --format markdown
.venv/bin/python3 valuation/run_valuation.py workspaces/{TICKER} {YYYY-MM-DD}
.venv/bin/python3 templates/render_pdf.py workspaces/{TICKER} {YYYY-MM-DD}
```

Run only the commands that fit the task and available inputs.

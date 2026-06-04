# Codex Bridge: MarketMind-AlphaEngine

## Purpose

MarketMind-AlphaEngine is an automated equity research pipeline that collects
market/company/sector data, builds evidence, computes quant and valuation
artifacts, runs analyst discussion, drafts and reviews reports, exports PDF
reports, and writes run memories.

For Codex, the durable project contract is split across:

- `AGENTS.md` — Codex entrypoint and load policy.
- `CLAUDE.md` — full MarketMind system contract.
- `.claude/skills/*/SKILL.md` — stage/task instructions.
- `plugin/commands/*.md` — user-invocable command workflows.
- `mcp/shared/contracts.py` — canonical runtime paths and stage list.
- `.mcp.json` — MCP server registration for Claude; the source for the Codex
  `~/.codex/config.toml` translation in `.agents/references/codex-config.toml`.

## Skill Invocation Policy

Skills are discovered natively under `.agents/skills/` (symlink → `.claude/skills/`).
Only **`mm-init`** and **`mm-orchestrator`** are implicitly invocable — they are
the entry points. Every other `mm-*` skill is a pipeline stage with
`allow_implicit_invocation: false` (see its `agents/openai.yaml`): explicitly
invocable for debugging (`$mm-…`) but never auto-matched, because it only runs
inside the orchestrated pipeline (arguments, prior-stage dated artifacts, MCP).

## Codex MCP Setup

The skills call `mcp__market-data__*`, `mcp__workspace__*`, `mcp__memory__*`.
Codex reads MCP servers from `~/.codex/config.toml`. Copy the three
`[mcp_servers.*]` tables from `.agents/references/codex-config.toml`, launch
`codex` from the repo root (server paths are repo-relative), and export
`NEWSAPI_KEY`/`FRED_API_KEY` first. If MCP is unavailable, fall back to the
local `mcp/*_server.py` modules or direct workspace file I/O.

## Core Runtime Contract

Use the 15-stage runtime contract from `mcp/shared/contracts.py`:

```text
resolve_config -> init_workspace -> collect -> normalize -> quant -> valuation
-> discuss_memos -> discuss_debate -> discuss_synthesis -> draft -> review
-> decide -> export -> user_review -> reflect
```

Workspace runs are date-stamped. `status.json` owns `run_date`; all dated
artifacts should use that exact date folder.

## Command Mapping

When the user invokes or describes a MarketMind command:

- Initialize a company workspace: read `plugin/commands/init.md`, then
  `.claude/skills/mm-init/SKILL.md` and `.claude/skills/mm-company-resolver/SKILL.md`.
- Run a pipeline: read `plugin/commands/run.md`, then
  `.claude/skills/mm-orchestrator/SKILL.md`; load each stage skill only when
  that stage is reached.
- Show status: read `plugin/commands/status.md` and inspect
  `workspaces/*/status.json`.
- Start dashboard: read `plugin/commands/mm-dashboard.md` and run
  `.venv/bin/python3 web/server.py --port <port>`.

## Codex Execution Rules

- Prefer deterministic Python modules over prompt-generated scripts. Existing
  deterministic modules include `valuation/`, `eval/`, `templates/`, and `mcp/`.
- Use `.venv/bin/python3`, never bare `python3`, for repo Python commands.
- Use `rg`/`rg --files` for searching.
- Use `apply_patch` for manual edits.
- For tests, start with focused tests for touched modules, then run broader
  pytest when the blast radius justifies it.
- For web/current market facts, browse before relying on memory.

## Tool Translation

Claude instructions may mention tools that Codex does not expose by the same
name:

- `Read`, `Glob`, `Grep`, `Bash`, `Write`, `Edit` -> use Codex shell tools and
  `apply_patch`.
- `TodoWrite` -> use Codex plan/status updates only when useful.
- `Agent` background workers -> execute directly, or use current-session
  subagent tooling if available.
- `WebSearch` / `WebFetch` -> use Codex web browsing when current facts or
  verification are required.
- `mcp__workspace__*`, `mcp__market-data__*`, `mcp__memory__*` -> the servers
  registered from `.agents/references/codex-config.toml`. If not attached, use
  the local Python servers/modules (`mcp/*_server.py`) or workspace files.

## Report And Data Rules

- Reports are not investment advice. Preserve that boundary.
- Cite evidence IDs and source dates when writing report text.
- Keep unsupported valuation output low-confidence rather than forcing a DCF.
- Preserve BUY/HOLD/SELL labels as English even when report prose is Chinese.
- When `language: ch`, user-facing report text should be Chinese; schemas and
  paths remain English.

## Common Verification Commands

```bash
.venv/bin/python3 -m pytest -q
.venv/bin/python3 eval/metrics.py --format markdown
.venv/bin/python3 valuation/run_valuation.py workspaces/{TICKER} {YYYY-MM-DD}
.venv/bin/python3 templates/render_pdf.py --help
```

Run only the commands that fit the task and available inputs.


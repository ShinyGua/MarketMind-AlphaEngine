# .agents

Codex CLI integration for MarketMind-AlphaEngine.

Codex natively discovers `AGENTS.md` and skills under `.agents/skills/`. This
directory wires the Claude-authored project assets into Codex's native skills
system without duplicating instructions.

## Layout

- **`skills/`** — a **symlink to `../.claude/skills`**. Codex follows symlinked
  skill folders, so every `mm-*/SKILL.md` becomes a discoverable Codex skill.
  Each skill dir also carries `agents/openai.yaml` (policy + MCP dependencies).
- **`codex-marketmind.md`** — the Codex operating profile for this repo.
- **`skill-index.md`** — maps pipeline stages and user requests to skill files.
- **`references/codex-config.toml`** — paste-ready MCP server config for
  `~/.codex/config.toml` (translated from `.mcp.json`).

The root `AGENTS.md` is the always-loaded control surface (entry points,
MCP setup, tool/argument translation, hard rules).

## How skills are exposed

- All `mm-*` skills are discoverable. Only `mm-init` and `mm-orchestrator` are
  **implicitly invocable** (`allow_implicit_invocation: true`) — they are the
  entry points. The other skills are pipeline stages with
  `allow_implicit_invocation: false`: discoverable and explicitly invocable
  (`$mm-…`) for debugging, but never auto-matched, because they only run inside
  the orchestrated pipeline (arguments, dated artifacts, MCP).
- `name`/`description` come from each `SKILL.md` frontmatter; `openai.yaml` does
  not duplicate them.

Do not copy full `SKILL.md` contents here — Codex loads the relevant skill file
on demand (progressive disclosure), which keeps context budget for the task.

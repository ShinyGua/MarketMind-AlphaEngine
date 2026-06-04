"""Guard against re-introducing the reserved `workspace` MCP server name.

Claude Code refuses to register a server literally named `workspace`; it must be
`mm-workspace`. This locks in the rename across .mcp.json, the Codex config, and
every tool-call reference (mcp__workspace__* must be mcp__mm-workspace__*).
"""
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_mcp_json_uses_mm_workspace():
    servers = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]
    assert "workspace" not in servers, "reserved name 'workspace' must not be registered"
    assert "mm-workspace" in servers


def test_codex_config_uses_mm_workspace():
    cfg = tomllib.loads((ROOT / ".agents" / "references" / "codex-config.toml").read_text())
    servers = cfg.get("mcp_servers", {})
    assert "workspace" not in servers
    assert "mm-workspace" in servers


def test_no_stale_tool_prefix_in_tracked_text():
    globs = [
        ".mcp.json", "AGENTS.md", "CLAUDE.md",
        ".agents/codex-marketmind.md",
        ".agents/references/codex-config.toml",
    ]
    files = [ROOT / g for g in globs]
    files += list((ROOT / ".claude" / "skills").rglob("SKILL.md"))
    files += list((ROOT / "plugin" / "commands").glob("*.md"))
    offenders = []
    for f in files:
        if f.exists() and "mcp__workspace__" in f.read_text(encoding="utf-8"):
            offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, f"stale mcp__workspace__ references: {offenders}"

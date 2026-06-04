"""Minimal, dependency-free `.env` loader for the MCP servers.

The market-data server reads API keys (NEWSAPI_KEY, FRED_API_KEY) from
``os.environ``. Claude Code only expands ``${VAR}`` in ``.mcp.json`` from the
shell that launched it, so keys placed in a project ``.env`` never reach the
server. This loader fills that gap without pulling in ``python-dotenv``.

Design:
- File is a *fallback*: a variable already present in ``os.environ`` (shell
  export, ``.mcp.json`` env) is never overridden.
- A small alias map maps common alternate names to the canonical names the code
  reads, so an existing ``.env`` keeps working (e.g. ``NEWSAPI_API_KEY`` →
  ``NEWSAPI_KEY``).
- Self-degrading: a missing or unreadable file is a silent no-op.
"""

from __future__ import annotations

import os
from pathlib import Path

# Project root is the directory two levels above this file (mcp/shared/dotenv.py).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Alternate env-var names → the canonical name the code actually reads.
ALIASES = {
    "NEWSAPI_API_KEY": "NEWSAPI_KEY",
}


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_dotenv(path: Path | str | None = None) -> dict[str, str]:
    """Load KEY=VALUE pairs from ``path`` (default: PROJECT_ROOT/.env) into
    ``os.environ`` without overriding variables already set.

    Returns the dict of values applied (after aliasing), for testing/logging.
    Never raises on a missing or malformed file.
    """
    env_path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    applied: dict[str, str] = {}
    try:
        text = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return applied

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = _strip_quotes(value)
        # Resolve the canonical name; the file value is a fallback only.
        canonical = ALIASES.get(key, key)
        if canonical not in os.environ or not os.environ.get(canonical):
            os.environ[canonical] = value
            applied[canonical] = value
    return applied

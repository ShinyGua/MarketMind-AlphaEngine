"""Unit tests for the dependency-free .env loader (mcp/shared/dotenv.py).

Validates parsing (quotes, comments, blanks, export prefix), the no-override
rule, the NEWSAPI_API_KEY -> NEWSAPI_KEY alias, and the missing-file no-op —
without launching the MCP server. Each test saves/restores os.environ so it is
hermetic.
"""
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "mm_dotenv", ROOT / "mcp" / "shared" / "dotenv.py")
dotenv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dotenv)


def _run(tmp_path, body, preset=None):
    """Write an .env with `body`, load it under a clean+`preset` environ snapshot."""
    saved = dict(os.environ)
    try:
        for k in ("NEWSAPI_KEY", "NEWSAPI_API_KEY", "FRED_API_KEY", "FOO", "BAR"):
            os.environ.pop(k, None)
        if preset:
            os.environ.update(preset)
        env_file = tmp_path / ".env"
        env_file.write_text(body, encoding="utf-8")
        applied = dotenv.load_dotenv(env_file)
        return applied, dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_parses_basic_quotes_comments_blanks(tmp_path):
    body = (
        "# a comment\n"
        "\n"
        'NEWSAPI_KEY="abc123"\n'
        "FRED_API_KEY = plain_value  \n"
        "FOO='single'\n"
        "# trailing comment\n"
    )
    applied, env = _run(tmp_path, body)
    assert env["NEWSAPI_KEY"] == "abc123"
    assert env["FRED_API_KEY"] == "plain_value"
    assert env["FOO"] == "single"
    assert applied["NEWSAPI_KEY"] == "abc123"


def test_does_not_override_existing(tmp_path):
    applied, env = _run(tmp_path, "NEWSAPI_KEY=fromfile\n",
                        preset={"NEWSAPI_KEY": "fromshell"})
    assert env["NEWSAPI_KEY"] == "fromshell"
    assert "NEWSAPI_KEY" not in applied


def test_overrides_empty_existing(tmp_path):
    # An empty pre-set var is treated as unset and gets filled.
    applied, env = _run(tmp_path, "NEWSAPI_KEY=fromfile\n",
                        preset={"NEWSAPI_KEY": ""})
    assert env["NEWSAPI_KEY"] == "fromfile"
    assert applied["NEWSAPI_KEY"] == "fromfile"


def test_alias_newsapi_api_key(tmp_path):
    applied, env = _run(tmp_path, 'NEWSAPI_API_KEY="807xyz"\n')
    assert env["NEWSAPI_KEY"] == "807xyz"
    assert applied["NEWSAPI_KEY"] == "807xyz"


def test_alias_does_not_clobber_canonical(tmp_path):
    # A real NEWSAPI_KEY line wins over the aliased NEWSAPI_API_KEY line.
    body = "NEWSAPI_KEY=canonical\nNEWSAPI_API_KEY=aliased\n"
    applied, env = _run(tmp_path, body)
    assert env["NEWSAPI_KEY"] == "canonical"


def test_export_prefix_stripped(tmp_path):
    applied, env = _run(tmp_path, "export FRED_API_KEY=fred99\n")
    assert env["FRED_API_KEY"] == "fred99"


def test_missing_file_is_noop(tmp_path):
    saved = dict(os.environ)
    try:
        applied = dotenv.load_dotenv(tmp_path / "does_not_exist.env")
        assert applied == {}
    finally:
        os.environ.clear()
        os.environ.update(saved)

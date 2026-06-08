"""Run-context files live in workspace subfolders (memory/, shared_context/).

These tests pin the new canonical write paths, the legacy fallback helpers, and
the read-resolvers' preference order. Pure tmp_path fixtures — no live artifacts,
no network.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "contracts", ROOT / "mcp" / "shared" / "contracts.py")
contracts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contracts)

DATE = "2026-06-08"


def test_canonical_write_paths_are_subfolders(tmp_path):
    assert contracts.shared_context_path(tmp_path, DATE) == \
        tmp_path / "shared_context" / f"{DATE}.json"
    assert contracts.memory_context_path(tmp_path, DATE, "analyst") == \
        tmp_path / "memory" / f"{DATE}_analyst.json"


def test_legacy_paths_are_root_level(tmp_path):
    assert contracts.legacy_shared_context_path(tmp_path, DATE) == \
        tmp_path / f"{DATE}_shared_context.json"
    assert contracts.legacy_memory_context_path(tmp_path, DATE, "writer") == \
        tmp_path / f"{DATE}_memory_context_writer.json"


def test_read_prefers_new_when_both_exist(tmp_path):
    new = contracts.shared_context_path(tmp_path, DATE)
    old = contracts.legacy_shared_context_path(tmp_path, DATE)
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("{}")
    old.write_text("{}")
    assert contracts.shared_context_read_path(tmp_path, DATE) == new


def test_read_falls_back_to_legacy_when_only_legacy_exists(tmp_path):
    old = contracts.legacy_memory_context_path(tmp_path, DATE, "reviewer")
    old.write_text("{}")
    assert contracts.memory_context_read_path(tmp_path, DATE, "reviewer") == old


def test_read_defaults_to_new_when_neither_exists(tmp_path):
    # writers create the new layout when nothing is on disk yet
    assert contracts.memory_context_read_path(tmp_path, DATE, "analyst") == \
        contracts.memory_context_path(tmp_path, DATE, "analyst")
    assert contracts.shared_context_read_path(tmp_path, DATE) == \
        contracts.shared_context_path(tmp_path, DATE)

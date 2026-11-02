"""Backup + restore drill against a temporary local restic repository, and the
restored-copy checks on their own. No off-site service is touched."""
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ledger import backup, seal, stamp, store  # noqa: E402
from tests.test_ledger import DATE, _workspace  # noqa: E402

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


def _populated(tmp_path, monkeypatch):
    monkeypatch.setattr(stamp, "ots_binary", lambda: None)
    root = tmp_path / "MMD"
    seal.init(root, stamp_binary=None)
    seal.seal(_workspace(tmp_path), DATE, root, now=NOW, stamp_binary=None)
    return root


def test_check_copy_passes_then_catches_damage(tmp_path, monkeypatch):
    root = _populated(tmp_path, monkeypatch)
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    assert backup.check_copy(copy)["ok"]
    archive = next((copy / "archive").glob("*.tar.gz"))
    archive.write_bytes(b"corrupt")
    result = backup.check_copy(copy)
    assert not result["ok"] and any("archive" in e for e in result["errors"])


def test_unconfigured_backup_and_drill_say_so_and_log_the_drill(tmp_path, monkeypatch):
    root = _populated(tmp_path, monkeypatch)
    env = {"PATH": "/usr/bin:/bin"}
    assert backup.backup(root, env)["ok"] is False
    result = backup.drill(root, env)
    assert result["ok"] is False and "RESTIC_REPOSITORY" in result["reason"]
    assert store.read(root)[-1]["type"] == "drill"        # failures are logged too
    assert store.verify(root)["ok"]


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")
def test_backup_restore_drill_round_trip(tmp_path, monkeypatch):
    root = _populated(tmp_path, monkeypatch)
    repo = tmp_path / "restic-repo"
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
           "RESTIC_REPOSITORY": str(repo), "RESTIC_PASSWORD": "test-only",
           "HOME": str(tmp_path), "RESTIC_CACHE_DIR": str(tmp_path / "cache")}
    subprocess.run(["restic", "init"], env=env, check=True, capture_output=True)
    snap = backup.backup(root, env)
    assert snap["ok"] and snap["snapshot"]
    result = backup.drill(root, env)
    assert result["ok"], result
    assert result["records"] >= 3
    last = store.read(root)[-1]
    assert last["type"] == "drill" and last["body"]["ok"] is True

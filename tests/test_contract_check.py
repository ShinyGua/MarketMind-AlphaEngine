"""Pytest wrapper for the prompt/runtime contract guard.

Fails CI if any skill/command drifts back to a known-bad pattern (wrong
NewsAPI env var, undated artifact paths, wrong news-config key, stale
stage count). See scripts/contract_check.py for the rules.
"""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "contract_check.py"
_spec = importlib.util.spec_from_file_location("contract_check", _SCRIPT)
contract_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract_check)


def test_no_contract_drift():
    violations = contract_check.run_checks()
    assert not violations, "Contract drift detected:\n" + "\n".join(violations)

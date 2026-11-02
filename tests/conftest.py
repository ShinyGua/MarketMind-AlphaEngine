"""Every test gets its own empty ledger root, so nothing reads or appends to the
real ~/MarketMindData (the gate and the driver consult the ledger)."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_ledger_root(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("MM_DATA_ROOT", str(tmp_path_factory.mktemp("mm_data_root")))

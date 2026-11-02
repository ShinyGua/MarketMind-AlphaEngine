"""Tests for scripts/collect_cn_chips.py — sorting, completed-session cutoffs,
as_of stamping and stale stripping. akshare is faked; no network."""
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("collect_cn_chips", ROOT / "scripts" / "collect_cn_chips.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

SH = ZoneInfo("Asia/Shanghai")


def test_fund_flow_sorts_and_drops_unfinished_session():
    rows = [("2026-10-29", 9e9), ("2026-10-27", -1e8), ("2026-10-28", -2e8), ("2026-10-26", 3e8)]

    class Ak:
        def stock_individual_fund_flow(self, stock, market):
            return pd.DataFrame({"日期": [r[0] for r in rows], "主力净流入-净额": [r[1] for r in rows]})

    # Reference session 10-28: the 10-29 row (still trading) must be ignored.
    out = cc.build_fund_flow(Ak(), "300308", "sz", "2026-10-28")
    assert out["as_of"] == "2026-10-28"
    assert out["net_1d"] == -2e8
    assert out["consecutive_outflow_days"] == 2


def test_northbound_records_its_real_date():
    class Ak:
        def stock_hsgt_individual_em(self, symbol):
            days = pd.bdate_range(end="2024-08-16", periods=30)
            return pd.DataFrame({"持股日期": days.strftime("%Y-%m-%d"),
                                 "持股数量占A股百分比": [17.0 + i * 0.01 for i in range(30)]})

    out = cc.build_northbound(Ak(), "000333", "2026-08-25")
    assert out["as_of"] == "2024-08-16"


def _artifact():
    return {
        "turnover": {"data_quality": "akshare", "as_of": "2026-10-28", "turnover_pct": 3.1},
        "main_force": {"data_quality": "akshare", "as_of": "2026-10-28", "net_1d": -2e8},
        "lhb": {"data_quality": "akshare", "as_of": None, "appearances_90d": 0, "events": []},
        "northbound": {"data_quality": "akshare", "as_of": "2024-08-16", "holding_pct": 17.63,
                       "change_5d_pp": -0.11, "source": "stock_hsgt_individual_em"},
        "margin": {"data_quality": "unavailable", "reason": "ConnectionError"},
        "holder_count": {"data_quality": "akshare", "as_of": "2026-02-28",
                         "latest": {"as_of": "2026-02-28", "holders": 249414}},
        "restricted_release": {"data_quality": "akshare", "upcoming": []},
    }


def test_stale_blocks_are_stripped_to_date_and_reason():
    art = _artifact()
    stale = cc.apply_freshness(art, "2026-10-28", "2026-10-28T12:00:00+00:00",
                               now=datetime(2026, 10, 28, 20, 0, tzinfo=SH))
    assert sorted(stale) == ["holder_count", "northbound"]
    nb = art["northbound"]
    assert nb["data_quality"] == "stale" and nb["as_of"] == "2024-08-16"
    assert "holding_pct" not in nb and "change_5d_pp" not in nb   # numbers are gone
    assert stale["northbound"]["change_5d_pp"] == -0.11           # kept for audit only
    assert art["main_force"]["data_quality"] == "akshare"
    assert art["main_force"]["lag_sessions"] == 0 and art["main_force"]["cadence"] == "daily"
    assert art["margin"]["data_quality"] == "unavailable"          # untouched


def test_run_writes_stale_sidecar(tmp_path, monkeypatch):
    ws = tmp_path / "000333"
    (ws / "profile").mkdir(parents=True)
    (ws / "profile" / "company_profile.json").write_text(json.dumps(
        {"market_profile": "CN", "exchange": "SZSE"}))

    import types
    fake = types.ModuleType("akshare")
    monkeypatch.setitem(sys.modules, "akshare", fake)
    blocks = _artifact()
    for name, fn in (("build_turnover", "turnover"), ("build_fund_flow", "main_force"),
                     ("build_lhb", "lhb"), ("build_northbound", "northbound"),
                     ("build_margin", "margin"), ("build_holder_count", "holder_count"),
                     ("build_restricted", "restricted_release")):
        payload = dict(blocks[fn])
        if payload.get("data_quality") == "unavailable":
            monkeypatch.setattr(cc, name, lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))
        else:
            payload.pop("data_quality")
            monkeypatch.setattr(cc, name, lambda *a, _p=payload, **k: dict(_p))
    monkeypatch.setattr(cc.freshness, "last_completed_session", lambda d, now=None, market="CN": "2026-10-28")
    monkeypatch.setattr("time.sleep", lambda s: None)  # _block's retry pause

    art = cc.run(ws, "2026-10-28")
    assert art["blocks_stale"] == ["holder_count", "northbound"]
    assert "margin" in art["blocks_unavailable"]
    side = json.loads((ws / "raw" / "2026-10-28" / "chips" / "cn_flows_stale.json").read_text())
    assert set(side["blocks"]) == {"holder_count", "northbound"}
    saved = json.loads((ws / "raw" / "2026-10-28" / "chips" / "cn_flows.json").read_text())
    assert saved["northbound"]["data_quality"] == "stale" and saved["schema_version"] == 2

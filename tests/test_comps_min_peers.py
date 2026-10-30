"""A comps median from fewer than 3 usable peers never anchors fair value: HOOD
2026-10-29 read fair value 285, "cheap", off one peer's P/E (XYZ 129x)."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from valuation import comps  # noqa: E402
from tests.test_valuation_grader_riskfree import DATE, _make, _regime, _summary  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "valuation_grader", ROOT / "eval" / "graders" / "valuation_grader.py")
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)

HOOD = {"current_price": 91.59, "trailing_pe": 41.57, "total_revenue": 5.082e9,
        "shares_outstanding": 900.5e6, "total_debt": 2.877e9, "total_cash": 5.829e9}
# COIN and DKNG lose money (negative P/E is dropped), leaving XYZ alone on P/E
PEERS = [{"ticker": "XYZ", "metrics": {"trailing_pe": 129.4, "ev_to_revenue": 1.8}},
         {"ticker": "COIN", "metrics": {"trailing_pe": -64.2, "ev_to_revenue": 7.12}},
         {"ticker": "DKNG", "metrics": {"trailing_pe": -61.6, "ev_to_revenue": 1.51}}]


def test_single_peer_pe_is_not_an_anchor():
    table = comps.build_comps("HOOD", HOOD, PEERS)
    implied = comps.implied_value_per_share(HOOD, table["benchmarks"])
    assert implied["by_pe"] is None and implied["blended"] is None
    assert implied["thin_multiples"] == {"trailing_pe": 1}
    assert implied["by_ev_revenue"] is not None                      # n=3 still counts


def test_threshold_is_configurable_and_unknown_n_is_kept():
    table = comps.build_comps("HOOD", HOOD, PEERS)
    assert comps.implied_value_per_share(HOOD, table["benchmarks"], min_peers=1)["by_pe"] > 250
    legacy = {"trailing_pe": {"median": 20.0}}                       # no "n" recorded
    assert comps.implied_value_per_share(HOOD, legacy)["by_pe"] is not None


def test_grader_warns_on_a_thin_comps_anchor(tmp_path):
    s = _summary()
    s["comps"] = {"trailing_pe": {"median": 129.4, "n": 1}, "ev_to_ebitda": {"median": None, "n": 0}}
    s["method_candidates"] = [{"method": "comps_earnings", "value": 285.19, "weight": 0.35,
                               "confidence": "low", "included": True}]
    warnings = vg.grade(str(_make(tmp_path, s, regime=_regime())), DATE)["warnings"]
    assert any("n=1" in w for w in warnings)

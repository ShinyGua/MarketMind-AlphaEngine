"""Unit tests for the valuation engine (formula-first, verifiable)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comps as comps_mod  # noqa: E402
import dcf as dcf_mod  # noqa: E402


# ── DCF ──────────────────────────────────────────────────────────────────

def test_effective_tax_rate_clamps():
    assert dcf_mod.effective_tax_rate([{"pretax_income": 100, "tax_provision": 21}]) == 0.21
    # absurd rate clamps into band
    assert dcf_mod.effective_tax_rate([{"pretax_income": 100, "tax_provision": 90}]) == dcf_mod.TAX_RATE_MAX
    # missing → default
    assert dcf_mod.effective_tax_rate([]) == dcf_mod.DEFAULT_TAX_RATE


def test_base_fcff_ebit_path():
    inc = [{"ebit": 1000.0}]
    cf = [{"dep_amort": 100.0, "capex": -200.0}]
    # 1000*(1-0.21) + 100 - 200 = 790 + 100 - 200 = 690
    assert dcf_mod.base_fcff(inc, cf, 0.21) == pytest.approx(690.0)


def test_base_fcff_fallback_to_ocf():
    inc = [{}]
    cf = [{"operating_cash_flow": 500.0, "capex": -120.0}]
    assert dcf_mod.base_fcff(inc, cf, 0.21) == pytest.approx(380.0)


def test_terminal_value_requires_wacc_above_growth():
    with pytest.raises(ValueError):
        dcf_mod.terminal_value(100, 0.03, 0.03)
    # Gordon growth: 100*1.02/(0.10-0.02) = 1275
    assert dcf_mod.terminal_value(100, 0.10, 0.02) == pytest.approx(1275.0)


def test_intrinsic_value_known_inputs():
    # base=100, g=0, 1 year, wacc=10%, term g=0, net_debt=0, shares=10
    # FCFF yr1 = 100; PV = 100/1.1 = 90.909
    # TV = 100*1/(0.10) = 1000; PV = 1000/1.1 = 909.09
    # EV = 999.99...; equity = 1000; /10 shares = 100
    res = dcf_mod.intrinsic_value_per_share(100, 0.0, 1, 0.10, 0.0, 0.0, 10)
    assert res["value_per_share"] == pytest.approx(100.0, rel=1e-6)
    assert res["ev"] == pytest.approx(1000.0, rel=1e-6)


def test_sensitivity_center_equals_base_case():
    base, g, years, net_debt, shares = 690.0, 0.05, 5, 1000.0, 100.0
    wacc_c, g_c = 0.09, 0.025
    grid = dcf_mod.sensitivity_grid(base, g, years, net_debt, shares,
                                    wacc_center=wacc_c, g_center=g_c, n=5)
    base_case = dcf_mod.intrinsic_value_per_share(base, g, years, wacc_c, g_c, net_debt, shares)
    mid = len(grid["wacc_axis"]) // 2
    assert grid["wacc_axis"][mid] == pytest.approx(wacc_c)
    assert grid["g_axis"][mid] == pytest.approx(g_c)
    # center cell of the grid must equal the standalone base-case value
    assert grid["grid"][mid][mid] == pytest.approx(base_case["value_per_share"])


def test_sensitivity_handles_wacc_le_growth_cells():
    # with a tight center, some low-wacc/high-g cells may be invalid → None
    grid = dcf_mod.sensitivity_grid(100, 0.05, 5, 0.0, 10.0,
                                    wacc_center=0.03, g_center=0.025, n=5,
                                    wacc_step=0.01, g_step=0.0025)
    flat = [c for row in grid["grid"] for c in row]
    assert any(c is None for c in flat)  # at least one invalid cell
    assert any(c is not None for c in flat)  # but not all invalid


# ── comps ──────────────────────────────────────────────────────────────────

def test_quartiles_ignores_none_and_nonpositive():
    q = comps_mod.quartiles([10, 20, 30, None, -5, 0])
    assert q["n"] == 3
    assert q["median"] == pytest.approx(20)
    assert q["p25"] == pytest.approx(15)
    assert q["p75"] == pytest.approx(25)


def test_percentile_rank():
    # value 30 vs peers [10,20,40] → >= two of three → 0.666
    assert comps_mod.percentile_rank(30, [10, 20, 40]) == pytest.approx(2 / 3)
    assert comps_mod.percentile_rank(None, [10, 20]) is None
    assert comps_mod.percentile_rank(-5, [10, 20]) is None


def test_build_comps_benchmarks():
    company = {"ev_to_ebitda": 20, "trailing_pe": 30, "ev_to_revenue": 5,
               "forward_pe": 25, "price_to_book": 4}
    peers = [
        {"ticker": "A", "metrics": {"ev_to_ebitda": 10, "trailing_pe": 15}},
        {"ticker": "B", "metrics": {"ev_to_ebitda": 12, "trailing_pe": 18}},
        {"ticker": "C", "metrics": {"ev_to_ebitda": 14, "trailing_pe": 20}},
    ]
    out = comps_mod.build_comps("X", company, peers)
    assert len(out["rows"]) == 4
    ev = out["benchmarks"]["ev_to_ebitda"]
    assert ev["median"] == pytest.approx(12)
    assert ev["company"] == 20
    # company richer than all peers → top percentile
    assert ev["company_percentile"] == pytest.approx(1.0)


def test_implied_value_from_pe():
    company = {"current_price": 100.0, "forward_pe": 25.0,
               "ebitda": None, "shares_outstanding": None}
    benchmarks = {"forward_pe": {"median": 20.0}, "ev_to_ebitda": {"median": None}}
    out = comps_mod.implied_value_per_share(company, benchmarks)
    # eps = 100/25 = 4; implied = 4 * 20 = 80
    assert out["by_pe"] == pytest.approx(80.0)
    assert out["blended"] == pytest.approx(80.0)

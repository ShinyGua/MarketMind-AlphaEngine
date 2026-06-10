"""Unit tests for the valuation engine (formula-first, verifiable)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comps as comps_mod  # noqa: E402
import dcf as dcf_mod  # noqa: E402
import run_valuation as runner_mod  # noqa: E402


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


def test_growth_schedule_linear_fade():
    sched = dcf_mod.growth_schedule(0.30, 0.025, 10)
    assert len(sched) == 10
    assert sched[0] == pytest.approx(0.30)        # year 1 == initial
    assert sched[-1] == pytest.approx(0.025)      # final year == terminal
    # strictly fading down, monotone
    assert all(sched[i] > sched[i + 1] for i in range(len(sched) - 1))


def test_growth_schedule_flat_when_equal_and_fades_up():
    assert dcf_mod.growth_schedule(0.025, 0.025, 5) == pytest.approx([0.025] * 5)
    up = dcf_mod.growth_schedule(0.01, 0.025, 5)   # sub-terminal grower fades UP
    assert up[0] == pytest.approx(0.01) and up[-1] == pytest.approx(0.025)
    assert all(up[i] < up[i + 1] for i in range(len(up) - 1))
    assert dcf_mod.growth_schedule(0.05, 0.025, 1) == [0.025]  # collapse


def test_two_stage_reduces_to_constant_when_flat():
    # a flat schedule (initial == terminal) must equal the constant model
    base, g, n, w, nd, sh = 690.0, 0.025, 8, 0.10, 1000.0, 100.0
    two = dcf_mod.intrinsic_value_two_stage(base, g, n, w, g, nd, sh)
    const = dcf_mod.intrinsic_value_per_share(base, g, n, w, g, nd, sh)
    assert two["value_per_share"] == pytest.approx(const["value_per_share"])


def test_two_stage_sensitivity_center_equals_base_case():
    base, ig, fade, net_debt, shares = 690.0, 0.30, 10, 1000.0, 100.0
    wacc_c, g_c = 0.12, 0.025
    grid = dcf_mod.sensitivity_grid(base, ig, fade, net_debt, shares,
                                    wacc_center=wacc_c, g_center=g_c, n=5,
                                    model="fcff_two_stage",
                                    initial_growth=ig, fade_years=fade)
    base_case = dcf_mod.intrinsic_value_two_stage(base, ig, fade, wacc_c, g_c,
                                                  net_debt, shares)
    mid = len(grid["wacc_axis"]) // 2
    assert grid["grid"][mid][mid] == pytest.approx(base_case["value_per_share"])


def test_two_stage_monotonic_in_initial_growth():
    base, fade, w, g, nd, sh = 690.0, 10, 0.12, 0.025, 1000.0, 100.0
    bear = dcf_mod.intrinsic_value_two_stage(base, 0.27, fade, w, g, nd, sh)
    mid = dcf_mod.intrinsic_value_two_stage(base, 0.30, fade, w, g, nd, sh)
    bull = dcf_mod.intrinsic_value_two_stage(base, 0.33, fade, w, g, nd, sh)
    assert bear["value_per_share"] <= mid["value_per_share"] <= bull["value_per_share"]


def test_two_stage_sensitivity_handles_wacc_le_growth_cells():
    grid = dcf_mod.sensitivity_grid(100, 0.30, 10, 0.0, 10.0,
                                    wacc_center=0.03, g_center=0.025, n=5,
                                    model="fcff_two_stage",
                                    initial_growth=0.30, fade_years=10)
    flat = [c for row in grid["grid"] for c in row]
    assert any(c is None for c in flat)
    assert any(c is not None for c in flat)


def test_sensitivity_handles_wacc_le_growth_cells():
    # with a tight center, some low-wacc/high-g cells may be invalid → None
    grid = dcf_mod.sensitivity_grid(100, 0.05, 5, 0.0, 10.0,
                                    wacc_center=0.03, g_center=0.025, n=5,
                                    wacc_step=0.01, g_step=0.0025)
    flat = [c for row in grid["grid"] for c in row]
    assert any(c is None for c in flat)  # at least one invalid cell
    assert any(c is not None for c in flat)  # but not all invalid


# ── growth selection (deterministic) ─────────────────────────────────────────
# All synthetic — never copy numbers from live workspace artifacts (they drift).

def _inc(*revenues):
    """Newest-first income_statement rows carrying revenue."""
    return [{"revenue": float(r)} for r in revenues]


def test_revenue_cagr_basic_and_insufficient():
    # 121 -> 100 over 2 intervals: (121/100)**(1/2) - 1 = 0.10
    assert dcf_mod.revenue_cagr(_inc(121, 110, 100), years=3) == pytest.approx(0.10)
    assert dcf_mod.revenue_cagr(_inc(100), years=3) is None        # <2 periods
    assert dcf_mod.revenue_cagr([], years=3) is None


def test_select_growth_cagr_first():
    # CAGR ~0.20, trailing 0.18 → CAGR is the source, high confidence, no divergence
    g = dcf_mod.select_growth(_inc(144, 120, 100), {"revenue_growth": 0.18}, {})
    assert g["source"] == "revenue_cagr_3y"
    assert g["confidence"] == "high"
    assert g["initial_growth"] == pytest.approx(0.20)


def test_select_growth_trailing_fallback_when_no_revenue_series():
    g = dcf_mod.select_growth([], {"revenue_growth": 0.12}, {})
    assert g["source"] == "revenue_growth_ttm"
    assert g["confidence"] == "medium"
    assert g["initial_growth"] == pytest.approx(0.12)


def test_select_growth_default_fallback():
    g = dcf_mod.select_growth([], {}, {"base_growth_fallback": 0.05})
    assert g["source"] == "default_fallback"
    assert g["confidence"] == "low"
    assert g["initial_growth"] == pytest.approx(0.05)


def test_select_growth_divergence_ratio_downgrade():
    # CAGR 0.80 vs trailing 0.20 (both positive) → ratio 4 > 2 → downgrade high→medium
    g = dcf_mod.select_growth(_inc(324, 180, 100), {"revenue_growth": 0.20},
                              {"growth_divergence_ratio": 2.0, "max_initial_growth": 1.0})
    assert g["source"] == "revenue_cagr_3y"
    assert g["confidence"] == "medium"
    assert "divergence ratio" in g["reason"]


def test_select_growth_divergence_abs_when_trailing_nonpositive():
    # trailing negative → ratio rule skipped; |0.30 - (-0.10)| = 0.40 > 0.25 → downgrade
    g = dcf_mod.select_growth(_inc(169, 130, 100), {"revenue_growth": -0.10},
                              {"growth_divergence_abs": 0.25, "max_initial_growth": 1.0})
    assert g["confidence"] == "medium"
    assert "abs divergence" in g["reason"]
    # the source is CAGR (positive); trailing<=0 is not usable as a source
    assert g["source"] == "revenue_cagr_3y"


def test_select_growth_maturity_cap_mega():
    g = dcf_mod.select_growth(_inc(400, 200, 100),  # CAGR ~1.0
                              {"revenue_growth": 0.9, "market_cap": 3e12},
                              {"mega_cap_threshold": 200e9, "mega_cap_max_growth": 0.25})
    assert g["initial_growth"] == pytest.approx(0.25)
    assert "capped" in g["reason"]


def test_select_growth_weak_margin_haircut():
    g = dcf_mod.select_growth([], {"revenue_growth": 0.20, "operating_margins": -0.10},
                              {"weak_margin_threshold": 0.05, "weak_margin_growth_haircut": 0.75,
                               "max_initial_growth": 1.0})
    assert g["initial_growth"] == pytest.approx(0.15)   # 0.20 * 0.75
    assert g["confidence"] == "low"                     # medium (trailing) → low
    assert "growth haircut" in g["reason"]


def test_select_growth_financials_caveat_by_industry():
    # industry match alone (broad sector) is enough to fire the caveat
    g = dcf_mod.select_growth(_inc(120, 110, 100), {"revenue_growth": 0.10},
                              {"max_initial_growth": 1.0},
                              sector="Financial Services", industry="Capital Markets")
    assert g["confidence"] == "medium"                  # high → medium
    assert "noisy-DCF" in g["reason"]


def test_low_growth_confidence_reduces_dcf_weight():
    dcf = {
        "wacc": 0.10, "terminal_growth": 0.025, "tv_fraction_in_band": True,
        "growth_confidence": "low",
        "scenarios": {"base": {"value_per_share": 100.0, "tv_fraction": 0.45}},
    }
    implied = {"blended": 80.0}
    fair_value, _, method, candidates = runner_mod._select_fair_value(
        dcf, implied, price=90.0, cfg={"dcf_min_wacc_spread": 0.015})
    dcf_c = [c for c in candidates if c["method"] == "dcf"][0]
    assert dcf_c["weight"] == pytest.approx(0.35)       # lowered from 0.65 by low growth conf
    assert dcf_c["confidence"] == "low"
    assert method == "blended"


# ── component-derived confidence (engine de-weighting, not just prose) ───────

def _cand(method, weight, confidence):
    return {"method": method, "value": 75.0, "weight": weight,
            "confidence": confidence, "included": True}


def test_component_confidence_blended_low_dragged_to_medium():
    # low DCF + medium comps at equal weight → dragged to medium, NOT high
    cands = [_cand("dcf", 0.35, "low"), _cand("comps_earnings", 0.35, "medium")]
    assert runner_mod._component_confidence(cands) == "medium"


def test_component_confidence_single_method_low_dcf_stays_low():
    # the single-method bug: a lone low-confidence DCF must never read "high"
    assert runner_mod._component_confidence([_cand("dcf", 0.35, "low")]) == "low"


def test_component_confidence_high_dcf_dominates_blend():
    cands = [_cand("dcf", 0.65, "high"), _cand("comps_earnings", 0.35, "medium")]
    assert runner_mod._component_confidence(cands) == "high"


def test_component_confidence_low_majority_drags_to_low():
    # low component carrying >=0.60 share caps the blend at low
    cands = [_cand("dcf", 0.65, "low"), _cand("comps_earnings", 0.35, "high")]
    assert runner_mod._component_confidence(cands) == "low"


def test_component_confidence_empty_and_excluded_is_none():
    assert runner_mod._component_confidence([]) is None
    excluded = [{"method": "dcf", "value": 75.0, "weight": 0.0,
                 "confidence": "low", "included": False}]
    assert runner_mod._component_confidence(excluded) is None


def test_min_conf_is_conservative():
    assert runner_mod._min_conf("high", "medium") == "medium"
    assert runner_mod._min_conf("medium", "low") == "low"
    assert runner_mod._min_conf("high", "high") == "high"


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


def test_implied_value_from_ev_revenue_with_growth_adjustment():
    company = {
        "total_revenue": 1000.0,
        "shares_outstanding": 100.0,
        "total_debt": 200.0,
        "total_cash": 50.0,
        "revenue_growth": 0.40,
    }
    benchmarks = {
        "ev_to_revenue": {"median": 5.0},
        "revenue_growth": {"median": 0.20},
    }
    out = comps_mod.implied_value_per_share(company, benchmarks)
    assert out["by_ev_revenue"] == pytest.approx(48.5)
    # growth adjustment caps at 2.0x here: ((5*2*1000)-150)/100 = 98.5
    assert out["by_ev_revenue_growth_adjusted"] == pytest.approx(98.5)


def test_fair_value_falls_back_to_revenue_comps_when_dcf_missing():
    implied = {"by_ev_revenue": 80.0, "by_ev_revenue_growth_adjusted": 90.0}
    fair_value, mos, method, candidates = runner_mod._select_fair_value(
        None, implied, price=100.0, cfg={"dcf_min_wacc_spread": 0.015})
    assert fair_value == pytest.approx(90.0)
    assert mos == pytest.approx(-0.10)
    assert method == "comps_revenue"
    assert [c for c in candidates if c["included"]][0]["method"] == "comps_revenue"


def test_fair_value_blends_profitable_dcf_and_earnings_comps():
    dcf = {
        "wacc": 0.09,
        "terminal_growth": 0.025,
        "tv_fraction_in_band": True,
        "scenarios": {"base": {"value_per_share": 100.0}},
    }
    implied = {"blended": 80.0, "by_ev_revenue": 120.0}
    fair_value, mos, method, candidates = runner_mod._select_fair_value(
        dcf, implied, price=90.0, cfg={"dcf_min_wacc_spread": 0.015})
    assert fair_value == pytest.approx(93.0)
    assert mos == pytest.approx(0.0333)
    assert method == "blended"
    assert {c["method"] for c in candidates if c["included"]} == {"dcf", "comps_earnings"}


def test_fragile_dcf_is_downgraded_against_earnings_comps():
    dcf = {
        "wacc": 0.052,
        "terminal_growth": 0.025,
        "tv_fraction_in_band": False,
        # terminal value dominates EV (88%) → fragile / terminal-dependent
        "scenarios": {"base": {"value_per_share": 200.0, "tv_fraction": 0.88}},
    }
    implied = {"blended": 100.0}
    fair_value, _, method, candidates = runner_mod._select_fair_value(
        dcf, implied, price=120.0,
        cfg={"dcf_min_wacc_spread": 0.015, "fragile_tv_fraction": 0.80})
    assert fair_value == pytest.approx(150.0)
    assert method == "blended"
    dcf_candidate = [c for c in candidates if c["method"] == "dcf"][0]
    assert dcf_candidate["confidence"] == "medium"
    assert dcf_candidate["weight"] == pytest.approx(0.35)


# ── scenario growth ordering (bear <= base <= bull) ──────────────────────────

def test_scenario_growths_bear_never_exceeds_base():
    # Sub-terminal base growth (e.g. after the weak-margin haircut): bear must
    # clamp to base, not float up to terminal growth above it.
    g = runner_mod._scenario_growths(0.01, 0.03, 0.025)
    assert g["bear"] <= g["base"] <= g["bull"]
    assert g["bear"] == pytest.approx(0.01)


def test_scenario_growths_normal_case_floors_bear_at_terminal():
    g = runner_mod._scenario_growths(0.20, 0.03, 0.025)
    assert g == {"bear": pytest.approx(0.17), "base": pytest.approx(0.20),
                 "bull": pytest.approx(0.23)}
    # delta larger than base-terminal gap: bear floors at terminal, still <= base
    g2 = runner_mod._scenario_growths(0.04, 0.10, 0.025)
    assert g2["bear"] == pytest.approx(0.025)
    assert g2["bear"] <= g2["base"] <= g2["bull"]


# ── zero / negative CAGR growth selection ─────────────────────────────────────

def test_select_growth_zero_cagr_is_a_valid_source():
    # Flat revenue: CAGR = 0.0 is a measurement, not a missing value.
    g = dcf_mod.select_growth(_inc(100, 100, 100), {}, {})
    assert g["source"] == "revenue_cagr_3y"
    assert g["initial_growth"] == pytest.approx(0.0)


def test_select_growth_negative_cagr_floors_at_zero_not_fallback():
    # Declining revenue, no positive trailing growth: never the +5% default.
    g = dcf_mod.select_growth(_inc(81, 90, 100), {"revenue_growth": -0.08},
                              {"base_growth_fallback": 0.05})
    assert g["source"] == "revenue_cagr_3y_declining"
    assert g["initial_growth"] == pytest.approx(0.0)
    assert g["confidence"] == "low"
    assert "floored at 0.0" in g["reason"]


def test_select_growth_negative_cagr_with_positive_trailing_uses_trailing():
    # Positive trailing growth still outranks the declining-CAGR floor.
    g = dcf_mod.select_growth(_inc(81, 90, 100), {"revenue_growth": 0.06}, {})
    assert g["source"] == "revenue_growth_ttm"
    assert g["initial_growth"] == pytest.approx(0.06)

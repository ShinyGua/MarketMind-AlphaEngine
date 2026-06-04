"""Formula-first DCF valuation primitives.

Pure functions — no I/O. Every output is a formula over the inputs so the
result is reproducible and auditable (the QC grader recomputes from these).

Model: unlevered free cash flow (FCFF) discounted at WACC, Gordon-growth
terminal value, enterprise-to-equity bridge via net debt.

    FCFF      = EBIT * (1 - tax) + D&A + capex        (capex is negative)
    Ke (CAPM) = risk_free + beta * equity_risk_premium
    WACC      = E/V * Ke + D/V * Kd * (1 - tax)
    TV        = FCFF_n * (1 + g) / (WACC - g)         (requires WACC > g)
    EV        = Σ PV(FCFF_t) + PV(TV)
    equity    = EV - net_debt        (net_debt = total_debt - total_cash)
    value/sh  = equity / shares_outstanding
"""

from __future__ import annotations

# ── guardrail constants (mirrors financial-services dcf-model discipline) ──
TV_FRACTION_MIN = 0.50   # terminal value should be 50–70% of enterprise value
TV_FRACTION_MAX = 0.70
TAX_RATE_MIN = 0.05
TAX_RATE_MAX = 0.40
DEFAULT_TAX_RATE = 0.21


def effective_tax_rate(income_statement: list[dict]) -> float:
    """Tax provision / pretax income from the latest year, clamped to a sane band.

    Falls back to DEFAULT_TAX_RATE when the inputs are missing or degenerate.
    """
    for row in income_statement:  # newest first
        pretax = row.get("pretax_income")
        tax = row.get("tax_provision")
        if pretax and tax is not None and pretax > 0:
            rate = tax / pretax
            return min(max(rate, TAX_RATE_MIN), TAX_RATE_MAX)
    return DEFAULT_TAX_RATE


def base_fcff(income_statement: list[dict], cash_flow: list[dict],
              tax_rate: float) -> float | None:
    """Latest-year unlevered FCF. Prefers EBIT-based FCFF; falls back to OCF+capex.

    Returns None when neither path has enough data.
    """
    inc = income_statement[0] if income_statement else {}
    cf = cash_flow[0] if cash_flow else {}
    ebit = inc.get("ebit")
    dep = cf.get("dep_amort")
    capex = cf.get("capex")  # negative in yfinance convention

    if ebit is not None and capex is not None:
        nopat = ebit * (1 - tax_rate)
        return nopat + (dep or 0.0) + capex  # capex negative ⇒ subtracts

    # Fallback: levered proxy from operating cash flow.
    ocf = cf.get("operating_cash_flow")
    if ocf is not None and capex is not None:
        return ocf + capex
    return None


def cost_of_equity(risk_free: float, beta: float, equity_risk_premium: float) -> float:
    return risk_free + beta * equity_risk_premium


def wacc(risk_free: float, beta: float, equity_risk_premium: float,
         equity_value: float, debt_value: float,
         cost_of_debt: float, tax_rate: float) -> float:
    """Weighted average cost of capital. If debt is ~0 or unknown, WACC == Ke."""
    ke = cost_of_equity(risk_free, beta, equity_risk_premium)
    total = (equity_value or 0.0) + (debt_value or 0.0)
    if not total or debt_value is None or debt_value <= 0:
        return ke
    we = equity_value / total
    wd = debt_value / total
    return we * ke + wd * cost_of_debt * (1 - tax_rate)


def project_fcff(base: float, growth: float, years: int) -> list[float]:
    """Project FCFF for `years` periods at a constant growth rate."""
    flows = []
    val = base
    for _ in range(years):
        val = val * (1 + growth)
        flows.append(val)
    return flows


def terminal_value(last_fcff: float, wacc_rate: float, terminal_growth: float) -> float:
    """Gordon-growth terminal value. Raises if WACC <= terminal growth."""
    if wacc_rate <= terminal_growth:
        raise ValueError(
            f"WACC ({wacc_rate:.4f}) must exceed terminal growth ({terminal_growth:.4f})"
        )
    return last_fcff * (1 + terminal_growth) / (wacc_rate - terminal_growth)


def present_value(flows: list[float], wacc_rate: float) -> list[float]:
    return [cf / (1 + wacc_rate) ** (i + 1) for i, cf in enumerate(flows)]


def enterprise_value(base: float, growth: float, years: int,
                     wacc_rate: float, terminal_growth: float) -> dict:
    """Full DCF → enterprise value plus the terminal-value fraction (guardrail).

    Returns {ev, pv_explicit, pv_terminal, tv_fraction, flows}.
    """
    flows = project_fcff(base, growth, years)
    pv_flows = present_value(flows, wacc_rate)
    tv = terminal_value(flows[-1], wacc_rate, terminal_growth)
    pv_tv = tv / (1 + wacc_rate) ** years
    pv_explicit = sum(pv_flows)
    ev = pv_explicit + pv_tv
    return {
        "ev": ev,
        "pv_explicit": pv_explicit,
        "pv_terminal": pv_tv,
        "tv_fraction": (pv_tv / ev) if ev else None,
        "flows": flows,
    }


def intrinsic_value_per_share(base: float, growth: float, years: int,
                              wacc_rate: float, terminal_growth: float,
                              net_debt: float, shares: float) -> dict:
    """Enterprise DCF → per-share intrinsic value with the equity bridge.

    Returns {value_per_share, ev, equity_value, tv_fraction}.
    """
    dcf = enterprise_value(base, growth, years, wacc_rate, terminal_growth)
    equity = dcf["ev"] - (net_debt or 0.0)
    vps = (equity / shares) if shares else None
    return {
        "value_per_share": vps,
        "ev": dcf["ev"],
        "equity_value": equity,
        "tv_fraction": dcf["tv_fraction"],
    }


def _centered_axis(center: float, step: float, n: int) -> list[float]:
    """Symmetric odd-length axis centered on `center` (guarantees center cell)."""
    if n % 2 == 0:
        n += 1
    half = n // 2
    return [round(center + (i - half) * step, 6) for i in range(n)]


def sensitivity_grid(base: float, growth: float, years: int,
                     net_debt: float, shares: float,
                     wacc_center: float, g_center: float,
                     n: int = 5, wacc_step: float = 0.01,
                     g_step: float = 0.0025) -> dict:
    """Per-share value over a WACC × terminal-growth grid.

    The center cell equals the base-case intrinsic value (a built-in sanity
    check the QC grader asserts). Cells where WACC <= g are returned as None.
    """
    wacc_axis = _centered_axis(wacc_center, wacc_step, n)
    g_axis = _centered_axis(g_center, g_step, n)
    grid = []
    for w in wacc_axis:
        row = []
        for g in g_axis:
            try:
                res = intrinsic_value_per_share(base, growth, years, w, g, net_debt, shares)
                row.append(res["value_per_share"])
            except ValueError:
                row.append(None)
        grid.append(row)
    return {"wacc_axis": wacc_axis, "g_axis": g_axis, "grid": grid}

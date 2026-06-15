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


def cost_of_equity(risk_free: float, beta: float, equity_risk_premium: float,
                   country_risk_premium: float = 0.0) -> float:
    """Ke = risk_free + beta * (mature-market ERP) + country_risk_premium.

    The country risk premium is added FLAT (beta-independent, Damodaran-style), so
    a low-beta defensive name in a higher-risk market still carries the market's
    equity risk — a beta-multiplied ERP alone would under-discount it."""
    return risk_free + beta * equity_risk_premium + (country_risk_premium or 0.0)


def wacc(risk_free: float, beta: float, equity_risk_premium: float,
         equity_value: float, debt_value: float,
         cost_of_debt: float, tax_rate: float,
         country_risk_premium: float = 0.0) -> float:
    """Weighted average cost of capital. If debt is ~0 or unknown, WACC == Ke."""
    ke = cost_of_equity(risk_free, beta, equity_risk_premium, country_risk_premium)
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


def growth_schedule(initial_growth: float, terminal_growth: float,
                    fade_years: int) -> list[float]:
    """Linear growth fade from `initial_growth` (year 1) to `terminal_growth`.

    The final year's growth equals `terminal_growth`, so the explicit phase
    hands off smoothly to the Gordon perpetuity. Monotone whether the company
    fades down (high grower) or up (sub-terminal grower); `initial == terminal`
    yields a flat schedule == the constant-growth model. `fade_years <= 1`
    collapses to a single terminal-growth period.
    """
    if fade_years <= 1:
        return [terminal_growth]
    return [initial_growth + (terminal_growth - initial_growth) * (t / (fade_years - 1))
            for t in range(fade_years)]


def project_fcff_schedule(base: float, schedule: list[float]) -> list[float]:
    """Project FCFF by compounding `base` through a per-year growth schedule."""
    flows = []
    val = base
    for g in schedule:
        val = val * (1 + g)
        flows.append(val)
    return flows


# ── growth selection (deterministic, fundamentals-driven) ──────────────────────
_CONFIDENCE_ORDER = ["low", "medium", "high"]

# sectors/industries where an FCFF DCF is structurally noisy (banks/brokers/etc.).
# Matched (lowercased) against BOTH the company's sector and its industry so a
# broad sector label ("Financial Services") plus a precise industry ("Capital
# Markets") both trigger the caveat.
DCF_NOISY_SECTORS = {
    "financial services", "financials", "banks", "bank", "insurance",
    "capital markets", "brokers", "broker", "asset management",
}


def _downgrade(confidence: str) -> str:
    """Lower a confidence label one notch (high→medium→low), floored at low."""
    try:
        i = _CONFIDENCE_ORDER.index(confidence)
    except ValueError:
        return "low"
    return _CONFIDENCE_ORDER[max(0, i - 1)]


def revenue_cagr(income_statement: list[dict], years: int = 3) -> float | None:
    """Annualized revenue CAGR from a newest-first income-statement list.

    Uses entries carrying a positive `revenue`; spans up to `years` intervals
    (fewer when history is short). Returns None with < 2 usable periods.
    """
    revs = [r.get("revenue") for r in (income_statement or [])
            if isinstance(r.get("revenue"), (int, float)) and r.get("revenue") > 0]
    if len(revs) < 2:
        return None
    span = min(years, len(revs) - 1)
    newest, oldest = revs[0], revs[span]
    if oldest <= 0:
        return None
    return (newest / oldest) ** (1 / span) - 1


def _maturity_cap(market_cap, cfg: dict) -> float:
    """Size-tiered growth ceiling: mega-caps can't sustain a small-cap rate."""
    default = cfg.get("max_initial_growth", 0.35)
    if not isinstance(market_cap, (int, float)) or market_cap <= 0:
        return default
    if market_cap >= cfg.get("mega_cap_threshold", 200e9):
        return cfg.get("mega_cap_max_growth", 0.25)
    if market_cap <= cfg.get("small_cap_threshold", 2e9):
        return cfg.get("small_cap_max_growth", default)
    return default


def _is_noisy_dcf_sector(sector, industry) -> bool:
    for label in (sector, industry):
        if isinstance(label, str) and label.strip().lower() in DCF_NOISY_SECTORS:
            return True
    return False


def select_growth(income_statement: list[dict], metrics: dict, cfg: dict,
                  sector: str | None = None, industry: str | None = None) -> dict:
    """Deterministic, fundamentals-driven initial-growth selection for the DCF.

    Hierarchy: forward analyst revenue growth (if ever collected) → 3yr revenue
    CAGR (>= 0; a measured decline floors growth at 0.0 instead of taking the
    optimistic fallback) → trailing revenue growth → fallback. Confidence is then downgraded by a
    sign/zero-safe CAGR-vs-trailing divergence guard, a profitability penalty, and
    a financials/brokers sector|industry caveat; growth is finally clamped by a
    size-tiered maturity cap. Pure — returns initial_growth + source + confidence
    + an audit `reason` trail + the raw components.
    """
    metrics = metrics or {}

    def _pos(v):
        return v if isinstance(v, (int, float)) and v > 0 else None

    cagr = revenue_cagr(income_statement, cfg.get("cagr_years", 3))
    # trailing_raw keeps the actual figure (may be ≤0, used by the divergence guard);
    # trailing is the positive-only value eligible to be the growth *source*.
    trailing_raw = metrics.get("revenue_growth")
    trailing_raw = trailing_raw if isinstance(trailing_raw, (int, float)) else None
    trailing = _pos(trailing_raw)
    forward = _pos(metrics.get("forward_revenue_growth"))  # inactive slot today
    fallback = cfg.get("base_growth_fallback", 0.05)

    if forward is not None:
        growth, source, confidence = forward, "forward_revenue_growth", "high"
    elif cagr is not None and cagr >= 0:
        growth, source, confidence = cagr, "revenue_cagr_3y", "high"
    elif trailing is not None:
        growth, source, confidence = trailing, "revenue_growth_ttm", "medium"
    elif cagr is not None:
        # Measured revenue decline with no positive trailing growth: floor at 0
        # rather than handing a shrinking business the optimistic default.
        growth, source, confidence = 0.0, "revenue_cagr_3y_declining", "low"
    else:
        growth, source, confidence = fallback, "default_fallback", "low"

    reasons = [f"source={source}"]
    if source == "revenue_cagr_3y_declining":
        reasons.append(f"measured CAGR {cagr:.3f} < 0 → growth floored at 0.0")

    # divergence guard (sign/zero-safe): only when both CAGR and a trailing figure
    # exist. Ratio rule needs both strictly positive; otherwise an absolute-diff
    # rule — never divide by a non-positive trailing.
    if cagr is not None and trailing_raw is not None:
        if cagr > 0 and trailing_raw > 0:
            ratio = cagr / trailing_raw
            hi = cfg.get("growth_divergence_ratio", 2.0)
            if ratio > hi or ratio < 1.0 / hi:
                confidence = _downgrade(confidence)
                reasons.append(f"cagr/trailing divergence ratio {ratio:.2f}")
        elif abs(cagr - trailing_raw) > cfg.get("growth_divergence_abs", 0.25):
            confidence = _downgrade(confidence)
            reasons.append(f"cagr/trailing abs divergence {abs(cagr - trailing_raw):.2f}")

    # profitability penalty: weak/negative margins → revenue growth ≠ FCFF growth.
    opm = metrics.get("operating_margins")
    if isinstance(opm, (int, float)) and opm < cfg.get("weak_margin_threshold", 0.05):
        growth *= cfg.get("weak_margin_growth_haircut", 0.75)
        confidence = _downgrade(confidence)
        reasons.append(f"weak operating margin {opm:.3f} → growth haircut")

    # financials/brokers caveat: FCFF DCF is structurally noisy.
    if _is_noisy_dcf_sector(sector, industry):
        confidence = _downgrade(confidence)
        reasons.append(f"noisy-DCF sector/industry ({sector or industry})")

    # maturity (size) cap.
    cap = _maturity_cap(metrics.get("market_cap"), cfg)
    if growth > cap:
        reasons.append(f"capped at {cap} (size tier)")
        growth = cap

    return {
        "initial_growth": growth,
        "source": source,
        "confidence": confidence,
        "reason": "; ".join(reasons),
        "components": {
            "cagr_3y": cagr,
            "revenue_growth_ttm": trailing_raw,
            "forward": forward,
        },
    }


def terminal_value(last_fcff: float, wacc_rate: float, terminal_growth: float) -> float:
    """Gordon-growth terminal value. Raises if WACC <= terminal growth."""
    if wacc_rate <= terminal_growth:
        raise ValueError(
            f"WACC ({wacc_rate:.4f}) must exceed terminal growth ({terminal_growth:.4f})"
        )
    return last_fcff * (1 + terminal_growth) / (wacc_rate - terminal_growth)


def present_value(flows: list[float], wacc_rate: float) -> list[float]:
    return [cf / (1 + wacc_rate) ** (i + 1) for i, cf in enumerate(flows)]


def enterprise_value_from_flows(flows: list[float], wacc_rate: float,
                                terminal_growth: float) -> dict:
    """Enterprise value from precomputed FCFF flows + Gordon terminal value.

    Returns {ev, pv_explicit, pv_terminal, tv_fraction, flows}. Shared core for
    both the constant-growth and the two-stage fade DCF so they stay identical.
    """
    pv_flows = present_value(flows, wacc_rate)
    tv = terminal_value(flows[-1], wacc_rate, terminal_growth)
    pv_tv = tv / (1 + wacc_rate) ** len(flows)
    pv_explicit = sum(pv_flows)
    ev = pv_explicit + pv_tv
    return {
        "ev": ev,
        "pv_explicit": pv_explicit,
        "pv_terminal": pv_tv,
        "tv_fraction": (pv_tv / ev) if ev else None,
        "flows": flows,
    }


def enterprise_value(base: float, growth: float, years: int,
                     wacc_rate: float, terminal_growth: float) -> dict:
    """Constant-growth DCF → enterprise value plus the terminal-value fraction.

    Returns {ev, pv_explicit, pv_terminal, tv_fraction, flows}.
    """
    flows = project_fcff(base, growth, years)
    return enterprise_value_from_flows(flows, wacc_rate, terminal_growth)


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


def intrinsic_value_two_stage(base: float, initial_growth: float, fade_years: int,
                              wacc_rate: float, terminal_growth: float,
                              net_debt: float, shares: float) -> dict:
    """Two-stage DCF: a high-growth phase fading to terminal, then Gordon TV.

    Growth fades linearly from `initial_growth` to `terminal_growth` over
    `fade_years`; the fade endpoint equals the Gordon `terminal_growth`, so the
    explicit exit and the perpetuity tie together. Returns the same shape as
    intrinsic_value_per_share plus the realized `growth_schedule`.
    """
    sched = growth_schedule(initial_growth, terminal_growth, fade_years)
    flows = project_fcff_schedule(base, sched)
    dcf = enterprise_value_from_flows(flows, wacc_rate, terminal_growth)
    equity = dcf["ev"] - (net_debt or 0.0)
    vps = (equity / shares) if shares else None
    return {
        "value_per_share": vps,
        "ev": dcf["ev"],
        "equity_value": equity,
        "tv_fraction": dcf["tv_fraction"],
        "growth_schedule": sched,
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
                     g_step: float = 0.0025, *,
                     model: str = "fcff_constant",
                     initial_growth: float | None = None,
                     fade_years: int | None = None) -> dict:
    """Per-share value over a WACC × terminal-growth grid.

    The center cell equals the base-case intrinsic value (a built-in sanity
    check the QC grader asserts). Cells where WACC <= g are returned as None.

    `model="fcff_two_stage"` runs the two-stage fade DCF per cell (requires
    `initial_growth` and `fade_years`); the default constant-growth path is
    unchanged. Either way only WACC and terminal growth vary across cells, so
    the center cell ties out to the base scenario by construction.
    """
    two_stage = model in ("fcff_two_stage", "revenue_margin")
    wacc_axis = _centered_axis(wacc_center, wacc_step, n)
    g_axis = _centered_axis(g_center, g_step, n)
    grid = []
    for w in wacc_axis:
        row = []
        for g in g_axis:
            try:
                if two_stage:
                    res = intrinsic_value_two_stage(
                        base, initial_growth, fade_years, w, g, net_debt, shares)
                else:
                    res = intrinsic_value_per_share(base, growth, years, w, g, net_debt, shares)
                row.append(res["value_per_share"])
            except ValueError:
                row.append(None)
        grid.append(row)
    return {"wacc_axis": wacc_axis, "g_axis": g_axis, "grid": grid}

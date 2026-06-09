#!/usr/bin/env python3
"""Valuation engine runner — Stage 5b (valuation).

Reads fundamentals collected by the company desk, runs a scenario DCF
(bull/base/bear) and a peer comps analysis, and writes structured artifacts
for downstream analysts, the decision maker, and the report writer.

Usage:
    .venv/bin/python3 valuation/run_valuation.py <workspace> <date>

Outputs (under {workspace}/valuation/{date}/):
    valuation_summary.json   — fair value, method, margin of safety, comps, flags
    comps.csv                — per-name multiples table
    dcf_sensitivity.csv      — WACC × terminal-growth grid (per-share value)

Never raises into the pipeline: on bad/missing inputs it writes a summary with
applicable=false or confidence="low" and exits 0 (the stage is non-critical).
"""

from __future__ import annotations

import csv
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comps as comps_mod  # noqa: E402
import dcf as dcf_mod  # noqa: E402

# instrument types that have no DCF/comps meaning
_NON_EQUITY = {"ETF", "MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY", "MONEYMARKET"}

_DEFAULTS = {
    "enabled": True,
    "risk_free_rate": 0.042,
    "equity_risk_premium": 0.05,
    "cost_of_debt": 0.05,
    "default_terminal_growth": 0.025,
    "projection_years": 5,
    "base_growth_fallback": 0.05,
    "scenario_growth_delta": 0.03,   # bull = base + delta, bear = base - delta
    "sensitivity_dims": 5,
    "max_base_growth": 0.20,         # legacy constant-growth cap (kept for compat)
    "dcf_min_wacc_spread": 0.015,
    "dcf_max_growth_wacc_excess": 0.10,  # exclude DCF when initial growth exceeds WACC by more than this (non-convergent explicit phase)
    "fade_years": 10,                # two-stage high-growth phase; fades to terminal
    "max_initial_growth": 0.35,      # default-tier cap on year-1 high-growth rate
    "fragile_tv_fraction": 0.80,     # downgrade DCF when terminal value exceeds this share
    # deterministic, fundamentals-driven growth selection (select_growth)
    "cagr_years": 3,                 # revenue-CAGR window
    "growth_divergence_ratio": 2.0,  # CAGR vs trailing ratio (both positive) → downgrade
    "growth_divergence_abs": 0.25,   # |CAGR − trailing| when signs differ / ≤0 → downgrade
    "weak_margin_threshold": 0.05,   # operating margin below → growth haircut + downgrade
    "weak_margin_growth_haircut": 0.75,
    "mega_cap_threshold": 200_000_000_000,  # ≥ $200B → mega-cap maturity cap
    "mega_cap_max_growth": 0.25,
    "small_cap_threshold": 2_000_000_000,   # ≤ $2B → small-cap tier
    "small_cap_max_growth": 0.35,    # default: no small-cap inflation unless raised
}


def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


# ── company-desk → engine schema adapter ──────────────────────────────────────
# The company desk writes raw yfinance fundamentals:
#   {"ticker", "valuation_ratios": {camelCase}, "financials": {statement: {item: {date: val}}}}
# The engine consumes a normalized shape: metrics{snake_case} + income_statement /
# cash_flow as lists-of-dicts (newest first). These helpers bridge the two without
# touching the math in dcf.py / comps.py.

# yfinance camelCase ratio → engine metrics key
_RATIO_MAP = {
    "enterpriseToRevenue": "ev_to_revenue",
    "enterpriseToEbitda": "ev_to_ebitda",
    "trailingPE": "trailing_pe",
    "forwardPE": "forward_pe",
    "priceToBook": "price_to_book",
    "revenueGrowth": "revenue_growth",
    "profitMargins": "profit_margins",
    "operatingMargins": "operating_margins",
    "marketCap": "market_cap",
    "enterpriseValue": "enterprise_value",
    "beta": "beta",
    "sharesOutstanding": "shares_outstanding",
    "currentPrice": "current_price",
    "totalRevenue": "total_revenue",
    "ebitda": "ebitda",
    "totalCash": "total_cash",
    "totalDebt": "total_debt",
    "quoteType": "quote_type",
}


def _looks_like_date(s) -> bool:
    """True for 'YYYY-MM-DD'-style keys used as statement period columns."""
    return isinstance(s, str) and len(s) >= 7 and s[:4].isdigit() and s[4] == "-"


def _latest_period(statement: dict):
    """Return the newest period's line items as {line_item: value}, or None.

    Handles both yfinance statement layouts the company desk may emit:
      • date-keyed:  {date_str: {line_item: value}}   (current desk schema)
      • item-keyed:  {line_item: {date_str: value}}   (transposed layout)
    """
    if not isinstance(statement, dict) or not statement:
        return None

    # date-keyed layout: top-level keys are date strings, values are item dicts.
    date_keys = [k for k, v in statement.items()
                 if _looks_like_date(k) and isinstance(v, dict)]
    if date_keys:
        period = statement[sorted(date_keys)[-1]]
        return period if isinstance(period, dict) else None

    # item-keyed layout: each value is a {date_str: value} series.
    dates = set()
    for series in statement.values():
        if isinstance(series, dict):
            dates.update(series.keys())
    if not dates:
        return None
    newest = sorted(dates)[-1]  # ISO-ish date strings sort chronologically
    return {item: series.get(newest)
            for item, series in statement.items()
            if isinstance(series, dict)}


def _norm_metrics(raw: dict) -> dict:
    # Support three schemas:
    #   schema A (company-desk v1): raw["valuation_ratios"] = {camelCase: value}
    #   schema B (direct yfinance): raw["info"] = {camelCase: value}
    #   schema D (flat fund/info): camelCase ratio keys live at the top level
    #       (the company desk emits this for ETFs/funds, e.g. {"quoteType": "ETF",
    #        "trailingPE": ..., "_is_fund": true})
    ratios = (raw.get("valuation_ratios") or raw.get("info")
              or raw.get("ratios") or raw)
    metrics = {}
    for src, dst in _RATIO_MAP.items():
        v = ratios.get(src)
        if v is not None and not (isinstance(v, float) and v != v):  # skip NaN
            metrics[dst] = v
    # quote_type: prefer metadata, then info/valuation_ratios field
    qt = ((raw.get("metadata") or {}).get("quote_type")
          or ratios.get("quoteType") or ratios.get("quote_type"))
    if qt:
        metrics["quote_type"] = qt
    return metrics


def _nonan(v):
    """Return None for NaN floats; pass other values through."""
    if isinstance(v, float) and v != v:
        return None
    return v


def _norm_statements(raw: dict):
    """Return (income_statement, cash_flow) as single-row lists (latest period).

    Prefers annual statements; falls back to quarterly when annual are empty
    (e.g. WOLF post-Chapter 11 reporting gap).

    Supports three layouts:
      • schema A: raw["financials"]["income_statement"] + raw["cashflow"]
      • schema B: raw["income_statement"] + raw["cash_flow"]  (direct yfinance)
      • schema C: raw["financials"] is a flat date-keyed dict (older layout)
    """
    fin = raw.get("financials") or {}
    inc_period = (_latest_period(fin.get("income_statement"))
                  or _latest_period(fin.get("income_statement_q"))
                  or _latest_period(raw.get("income_statement"))
                  or _latest_period(fin))
    cf_period = (_latest_period(raw.get("cashflow"))
                 or _latest_period(raw.get("cashflow_q"))
                 or _latest_period(raw.get("cash_flow"))
                 or _latest_period(fin.get("cashflow"))
                 or _latest_period(fin.get("cashflow_q")))

    income = []
    if inc_period:
        ebit = _nonan(inc_period.get("EBIT")) or _nonan(inc_period.get("Operating Income"))
        income = [{
            "ebit": ebit,
            "pretax_income": _nonan(inc_period.get("Pretax Income")),
            "tax_provision": _nonan(inc_period.get("Tax Provision")),
        }]
    cash_flow = []
    if cf_period:
        dep = (_nonan(cf_period.get("Depreciation And Amortization"))
               or _nonan(cf_period.get("Depreciation Amortization Depletion"))
               or _nonan(cf_period.get("Depreciation")))
        cash_flow = [{
            "dep_amort": dep,
            "capex": _nonan(cf_period.get("Capital Expenditure")),
            "operating_cash_flow": _nonan(cf_period.get("Operating Cash Flow")),
        }]
    return income, cash_flow


def _adapt_fundamentals(raw: dict) -> dict:
    """Normalize a company-desk fundamentals dict into the engine schema.

    Idempotent: a dict that already has `metrics` is returned unchanged.

    Handles four input schemas:
      • already-normalized: has `metrics` key → pass through
      • company-desk v1: has `valuation_ratios` + `financials` keys
      • direct yfinance: has `info` + `income_statement` + `cash_flow` keys
      • flat fund/info: camelCase ratio keys at the top level (e.g. ETF dumps
        with `quoteType`/`_is_fund`); has no statements but carries `quoteType`
        so the instrument-type guard can flag it not-applicable downstream.
    """
    if not isinstance(raw, dict):
        return raw
    if raw.get("metrics"):
        return raw
    # Accept any recognized schema; fall through if none is present.
    has_ratios_schema = ("valuation_ratios" in raw or "financials" in raw
                         or "ratios" in raw)
    has_info_schema = ("info" in raw or "income_statement" in raw
                       or "cash_flow" in raw or "cashflow" in raw)
    has_flat_schema = "quoteType" in raw or "currentPrice" in raw
    if not (has_ratios_schema or has_info_schema or has_flat_schema):
        return raw
    income, cash_flow = _norm_statements(raw)
    return {
        "ticker": raw.get("ticker") or raw.get("symbol"),
        "metrics": _norm_metrics(raw),
        "income_statement": income,
        "cash_flow": cash_flow,
        "metadata": raw.get("metadata"),
    }


def _cfg(workspace):
    rc = _load_json(os.path.join(workspace, "resolved_config.json")) or {}
    block = {**_DEFAULTS, **(rc.get("valuation") or {})}
    return block, rc.get("language", "en")


def _round(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) else v


def _positive(v) -> bool:
    return isinstance(v, (int, float)) and v > 0


def _candidate(method, value, weight, confidence, included=True, reason=None):
    has_value = _positive(value)
    return {
        "method": method,
        "value": _round(value) if has_value else None,
        "weight": weight if included and has_value else 0.0,
        "confidence": confidence,
        "included": bool(included and has_value),
        "reason": reason,
    }


def _weighted_value(candidates):
    active = [c for c in candidates if c.get("included") and _positive(c.get("value"))]
    total_w = sum(c.get("weight") or 0.0 for c in active)
    if not active or total_w <= 0:
        return None
    return sum(c["value"] * c["weight"] for c in active) / total_w


_CONF_ORD = {"low": 0, "medium": 1, "high": 2}
_ORD_CONF = {0: "low", 1: "medium", 2: "high"}


def _min_conf(a, b):
    """Conservative minimum of two confidence labels."""
    return _ORD_CONF[min(_CONF_ORD[a], _CONF_ORD[b])]


def _component_confidence(candidates):
    """Derive a confidence label from the included method candidates.

    Weight-share-weighted average of the component confidences, rounded (half-up)
    to the nearest label, then *dragged down* when a low-confidence component
    carries meaningful weight — so a fair value leaning on a fragile input cannot
    read "high". Covers single-method cases (one included candidate just returns
    its own confidence) and blends alike. Returns None when nothing is included.
    """
    active = [c for c in candidates
              if c.get("included") and (c.get("weight") or 0.0) > 0
              and c.get("confidence") in _CONF_ORD]
    total_w = sum(c["weight"] for c in active)
    if not active or total_w <= 0:
        return None
    weighted = sum(_CONF_ORD[c["confidence"]] * c["weight"] for c in active) / total_w
    label = _ORD_CONF[int(weighted + 0.5)]  # round half up (ordinals are non-negative)
    # Drag guard: a low-confidence component with a meaningful weight share caps
    # the blend — ≥0.30 share → no better than "medium"; ≥0.60 share → "low".
    for c in active:
        if c["confidence"] == "low":
            share = c["weight"] / total_w
            if share >= 0.60 and _CONF_ORD[label] > 0:
                label = "low"
            elif share >= 0.30 and _CONF_ORD[label] > 1:
                label = "medium"
    return label


def _select_fair_value(dcf_block, implied, price, cfg):
    """Choose the canonical fair-value anchor from DCF and comps candidates."""
    candidates = []

    intrinsic_base = (dcf_block or {}).get("scenarios", {}).get("base", {}).get("value_per_share")
    dcf_ok = _positive(intrinsic_base)
    dcf_reason = None
    dcf_conf = "high"
    dcf_weight = 0.65
    if dcf_ok and dcf_block:
        spread = (dcf_block.get("wacc") or 0.0) - (dcf_block.get("terminal_growth") or 0.0)
        # Fragile only when terminal value *dominates* (terminal-dependent). A low
        # TV fraction — common for the two-stage fade where the explicit phase
        # carries most of the value — is robust, not fragile, so it is NOT downgraded.
        base_tvf = (dcf_block.get("scenarios", {}).get("base", {}) or {}).get("tv_fraction")
        fragile_tvf = cfg.get("fragile_tv_fraction", dcf_mod.TV_FRACTION_MAX)
        # Non-convergence guard: when the explicit-phase initial growth sits far
        # above WACC, early-year cashflows compound faster than they discount, so
        # the explicit-phase PV explodes (a post-recovery revenue CAGR against a
        # low-beta WACC can imply an equity value many multiples of market cap).
        # The spread guard below only compares WACC to *terminal* growth and misses
        # this, so exclude the DCF outright. Fragile DCF must not anchor fair value.
        init_g = dcf_block.get("initial_growth")
        max_excess = cfg.get("dcf_max_growth_wacc_excess")
        wacc_val = dcf_block.get("wacc") or 0.0
        if (init_g is not None and max_excess is not None
                and init_g - wacc_val > max_excess):
            dcf_conf, dcf_weight = "low", 0.0
            dcf_reason = ("excluded: initial growth %.1f%% exceeds WACC %.1f%% by "
                          ">%.0fpp (non-convergent explicit phase)"
                          % (init_g * 100, wacc_val * 100, max_excess * 100))
        elif spread < cfg["dcf_min_wacc_spread"]:
            dcf_conf, dcf_weight = "low", 0.0
            dcf_reason = "excluded: WACC too close to terminal growth"
        elif base_tvf is not None and base_tvf > fragile_tvf:
            dcf_conf, dcf_weight = "medium", 0.35
            dcf_reason = "downgraded: terminal value dominates enterprise value"
        # Low-confidence growth selection lowers the DCF's blend weight so the
        # fair value leans on comps. Only ever lowers (never raises) the weight.
        gconf = dcf_block.get("growth_confidence")
        weight_cap = {"high": 0.65, "medium": 0.5, "low": 0.35}.get(gconf)
        if weight_cap is not None and dcf_weight > weight_cap:
            dcf_weight = weight_cap
            if gconf == "low":
                dcf_conf = "low"
            elif gconf == "medium" and dcf_conf == "high":
                dcf_conf = "medium"
            note = f"growth confidence {gconf}"
            dcf_reason = f"{dcf_reason}; {note}" if dcf_reason else note
    else:
        dcf_reason = "not available"
    candidates.append(_candidate("dcf", intrinsic_base, dcf_weight, dcf_conf,
                                 included=dcf_ok and dcf_weight > 0, reason=dcf_reason))

    earnings_value = (implied or {}).get("blended")
    candidates.append(_candidate("comps_earnings", earnings_value, 0.35, "medium",
                                 included=_positive(earnings_value),
                                 reason=None if _positive(earnings_value) else "not available"))

    revenue_value = ((implied or {}).get("by_ev_revenue_growth_adjusted")
                     or (implied or {}).get("by_ev_revenue"))
    rev_weight = 0.0 if any(c.get("included") for c in candidates) else 1.0
    candidates.append(_candidate("comps_revenue", revenue_value, rev_weight, "low",
                                 included=_positive(revenue_value) and rev_weight > 0,
                                 reason=None if _positive(revenue_value) else "not available"))

    fair_value = _weighted_value(candidates)
    method = "none"
    if fair_value is not None:
        active = [c["method"] for c in candidates if c.get("included")]
        method = active[0] if len(active) == 1 else "blended"

    mos = round((fair_value - price) / price, 4) if fair_value is not None and price else None
    return _round(fair_value), mos, method, candidates


def _write_summary(out_dir, summary):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "valuation_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False, default=str)


def _not_applicable(out_dir, ticker, reason, lang):
    msg = (f"{ticker} 不适用估值模型：{reason}。" if lang == "ch"
           else f"Valuation model not applicable for {ticker}: {reason}.")
    _write_summary(out_dir, {
        "ticker": ticker,
        "applicable": False,
        "confidence": "n/a",
        "reason": reason,
        "summary": msg,
    })
    print(f"valuation: not applicable for {ticker} ({reason})")


def main():
    if len(sys.argv) < 3:
        print("usage: run_valuation.py <workspace> <date>", file=sys.stderr)
        sys.exit(2)

    workspace, date = sys.argv[1], sys.argv[2]
    ticker = os.path.basename(os.path.normpath(workspace))
    cfg, lang = _cfg(workspace)
    out_dir = os.path.join(workspace, "valuation", date)

    if not cfg.get("enabled", True):
        print("valuation: disabled in config")
        return

    # Try several possible filenames: {ticker}.json, then any *.json in the dir
    # (company desk may append exchange suffix, e.g. 0941.HK.json)
    fund_dir = os.path.join(workspace, "raw", date, "fundamentals")
    fund_path = os.path.join(fund_dir, f"{ticker}.json")
    if not os.path.exists(fund_path):
        candidates = [f for f in sorted(glob.glob(os.path.join(fund_dir, "*.json")))
                      if os.path.basename(f).startswith(ticker)]
        fund_path = candidates[0] if candidates else fund_path
    fund = _adapt_fundamentals(_load_json(fund_path))
    if not fund or not fund.get("metrics"):
        _not_applicable(out_dir, ticker, "fundamentals unavailable", lang)
        return

    metrics = fund["metrics"]
    quote_type = (metrics.get("quote_type") or "").upper()
    if quote_type in _NON_EQUITY:
        _not_applicable(out_dir, ticker, f"instrument type {quote_type}", lang)
        return

    # current price: prefer fundamentals, fall back to quant latest close
    price = metrics.get("current_price")
    if not price:
        q = _load_json(os.path.join(workspace, "quant", date, "quant_summary.json")) or {}
        price = q.get("latest_close")

    # sector/industry (for the financials/brokers DCF-growth caveat); graceful if absent
    profile = _load_json(os.path.join(workspace, "profile", "company_profile.json")) or {}
    sector = profile.get("sector") or profile.get("sector_profile")
    industry = profile.get("industry")

    # peers
    peer_files = sorted(glob.glob(
        os.path.join(workspace, "raw", date, "fundamentals", "peers", "*.json")))
    peers = [p for p in (_adapt_fundamentals(_load_json(f)) for f in peer_files)
             if p and p.get("metrics")]

    inputs_missing = []

    # ── comps ──────────────────────────────────────────────────────────────
    comps_table, implied = None, None
    if peers:
        comps_table = comps_mod.build_comps(ticker, metrics,
                                            [{"ticker": p["ticker"], "metrics": p["metrics"]}
                                             for p in peers])
        implied = comps_mod.implied_value_per_share(metrics, comps_table["benchmarks"])
    else:
        inputs_missing.append("peer_fundamentals")

    # ── DCF ──────────────────────────────────────────────────────────────────
    dcf_block, sensitivity = None, None
    income = fund.get("income_statement") or []
    cash_flow = fund.get("cash_flow") or []
    shares = metrics.get("shares_outstanding")
    if not income or not cash_flow:
        inputs_missing.append("financial_statements")
    if not shares:
        inputs_missing.append("shares_outstanding")

    tax_rate = dcf_mod.effective_tax_rate(income)
    base = dcf_mod.base_fcff(income, cash_flow, tax_rate)
    if base is None or base <= 0:
        inputs_missing.append("positive_free_cash_flow")

    if base and base > 0 and shares:
        beta = metrics.get("beta") or 1.0
        net_debt = (metrics.get("total_debt") or 0.0) - (metrics.get("total_cash") or 0.0)
        wacc_rate = dcf_mod.wacc(
            risk_free=cfg["risk_free_rate"], beta=beta,
            equity_risk_premium=cfg["equity_risk_premium"],
            equity_value=metrics.get("market_cap") or 0.0,
            debt_value=metrics.get("total_debt") or 0.0,
            cost_of_debt=cfg["cost_of_debt"], tax_rate=tax_rate,
        )
        g_term = cfg["default_terminal_growth"]
        # keep WACC strictly above terminal growth
        if wacc_rate <= g_term:
            wacc_rate = g_term + 0.03

        # Two-stage fade DCF: the high-growth phase (capped at max_initial_growth,
        # well above the legacy 0.20 constant-growth cap) fades linearly to terminal
        # growth over fade_years, so fast growers are not understated by a single
        # constant rate. Scenarios move the *initial* growth lever; bear is floored
        # at terminal so the fade stays monotone and bear <= base <= bull holds.
        # Deterministic, fundamentals-driven growth selection (3yr revenue CAGR
        # preferred, with divergence / profitability / sector / maturity guards).
        gsel = dcf_mod.select_growth(income, metrics, cfg, sector=sector, industry=industry)
        initial_growth = gsel["initial_growth"]
        delta = cfg["scenario_growth_delta"]
        fade_years = cfg["fade_years"]

        scenarios = {}
        for name, ig in (("bear", max(initial_growth - delta, g_term)),
                         ("base", initial_growth),
                         ("bull", initial_growth + delta)):
            res = dcf_mod.intrinsic_value_two_stage(
                base, ig, fade_years, wacc_rate, g_term, net_debt, shares)
            scenarios[name] = {
                "growth": _round(ig, 4),
                "value_per_share": _round(res["value_per_share"]),
                "tv_fraction": _round(res["tv_fraction"], 3),
            }

        base_vps = scenarios["base"]["value_per_share"]
        tv_frac = scenarios["base"]["tv_fraction"]
        base_schedule = dcf_mod.growth_schedule(initial_growth, g_term, fade_years)
        dcf_block = {
            "model": "fcff_two_stage",
            "wacc": _round(wacc_rate, 4),
            "terminal_growth": _round(g_term, 4),
            "tax_rate": _round(tax_rate, 4),
            "beta": _round(beta, 3),
            "base_fcff": _round(base),
            "net_debt": _round(net_debt),
            "initial_growth": _round(initial_growth, 4),
            "growth_source": gsel["source"],
            "growth_confidence": gsel["confidence"],
            "growth_reason": gsel["reason"],
            "growth_components": {k: _round(v, 4) for k, v in gsel["components"].items()},
            "fade_years": fade_years,
            "projection_years": fade_years,
            "growth_schedule": [_round(g, 4) for g in base_schedule],
            "scenarios": scenarios,
            "tv_fraction_in_band": (tv_frac is not None
                                    and dcf_mod.TV_FRACTION_MIN <= tv_frac <= dcf_mod.TV_FRACTION_MAX),
        }

        grid = dcf_mod.sensitivity_grid(
            base, initial_growth, fade_years, net_debt, shares,
            wacc_center=wacc_rate, g_center=g_term, n=cfg["sensitivity_dims"],
            model="fcff_two_stage", initial_growth=initial_growth, fade_years=fade_years)
        grid["center_value"] = base_vps  # equals scenarios.base — QC asserts this
        sensitivity = grid

    intrinsic_base = (dcf_block or {}).get("scenarios", {}).get("base", {}).get("value_per_share")
    fair_value, margin_of_safety, valuation_method, method_candidates = _select_fair_value(
        dcf_block, implied, price, cfg)
    fair_value_range = None
    if dcf_block and valuation_method in {"dcf", "blended"}:
        fair_value_range = {
            "low": (dcf_block or {}).get("scenarios", {}).get("bear", {}).get("value_per_share"),
            "base": fair_value,
            "high": (dcf_block or {}).get("scenarios", {}).get("bull", {}).get("value_per_share"),
        }
    elif implied:
        vals = [implied.get(k) for k in (
            "by_pe", "by_ev_ebitda", "by_ev_revenue", "by_ev_revenue_growth_adjusted")]
        vals = [v for v in vals if _positive(v)]
        if vals and fair_value:
            fair_value_range = {"low": _round(min(vals)), "base": fair_value, "high": _round(max(vals))}

    # ── verdict + confidence ─────────────────────────────────────────────────
    if margin_of_safety is None:
        verdict = "unknown"
    elif margin_of_safety >= 0.15:
        verdict = "cheap"
    elif margin_of_safety <= -0.15:
        verdict = "expensive"
    else:
        verdict = "fair"

    critical_missing = {"financial_statements", "positive_free_cash_flow", "shares_outstanding"}
    if valuation_method == "none":
        heuristic = "low"
    elif valuation_method == "comps_revenue":
        heuristic = "low"
    elif valuation_method == "blended":
        heuristic = "medium" if inputs_missing or len(peers) < 3 else "high"
    elif not (inputs_missing) and len(peers) >= 3:
        heuristic = "high"
    elif critical_missing & set(inputs_missing) or len(peers) < 2:
        heuristic = "low"
    else:
        heuristic = "medium"

    # For methods backed by scored candidates, never claim more confidence than
    # the included components support — a low-confidence DCF (single-method or in
    # a blend) cannot read "high". `none`/`comps_revenue` keep their hard floor.
    confidence = heuristic
    if valuation_method in ("dcf", "comps_earnings", "blended"):
        comp_conf = _component_confidence(method_candidates)
        if comp_conf is not None:
            confidence = _min_conf(heuristic, comp_conf)

    # ── summary text ─────────────────────────────────────────────────────────
    if lang == "ch":
        mos_txt = (f"{margin_of_safety*100:.0f}%" if margin_of_safety is not None else "不可计算")
        fv_txt = fair_value if fair_value else "不可计算"
        summary_text = (f"{ticker} 现价 {price}，公允价值 {fv_txt}"
                        f"（方法：{valuation_method}），"
                        f"安全边际 {mos_txt}，估值判断：{verdict}（置信度 {confidence}）。")
    else:
        mos_txt = (f"{margin_of_safety*100:.0f}%" if margin_of_safety is not None else "n/a")
        fv_txt = fair_value if fair_value else "n/a"
        summary_text = (f"{ticker} at {price}: fair value {fv_txt} "
                        f"({valuation_method}), margin of safety {mos_txt}, "
                        f"verdict {verdict} (confidence {confidence}).")

    summary = {
        "ticker": ticker,
        "applicable": True,
        "confidence": confidence,
        "current_price": _round(price) if price else None,
        "fair_value": fair_value,
        "fair_value_range": fair_value_range,
        "valuation_method": valuation_method,
        "method_candidates": method_candidates,
        "intrinsic_value_base": intrinsic_base,
        "intrinsic_range": {
            "bear": (dcf_block or {}).get("scenarios", {}).get("bear", {}).get("value_per_share"),
            "base": intrinsic_base,
            "bull": (dcf_block or {}).get("scenarios", {}).get("bull", {}).get("value_per_share"),
        } if dcf_block else None,
        "margin_of_safety": margin_of_safety,
        "verdict": verdict,
        "dcf": dcf_block,
        "comps": comps_table["benchmarks"] if comps_table else None,
        "comps_implied_value": implied,
        "inputs_missing": inputs_missing,
        "summary": summary_text,
        "metadata": {"date": date, "source": "yfinance", "peers_used": len(peers)},
    }
    _write_summary(out_dir, summary)

    # ── CSV artifacts ────────────────────────────────────────────────────────
    if comps_table:
        with open(os.path.join(out_dir, "comps.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            cols = ["ticker"] + comps_mod.COMPS_MULTIPLES + comps_mod.COMPS_CONTEXT
            w.writerow(cols)
            for row in comps_table["rows"]:
                w.writerow([row.get(c) for c in cols])

    if sensitivity:
        with open(os.path.join(out_dir, "dcf_sensitivity.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["wacc\\g"] + [f"{g:.4f}" for g in sensitivity["g_axis"]])
            for wv, row in zip(sensitivity["wacc_axis"], sensitivity["grid"]):
                w.writerow([f"{wv:.4f}"] + [(_round(c) if c is not None else "") for c in row])

    print(f"valuation: {ticker} verdict={verdict} mos={margin_of_safety} "
          f"confidence={confidence} peers={len(peers)}")


if __name__ == "__main__":
    main()

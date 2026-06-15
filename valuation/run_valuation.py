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
    "use_live_risk_free": True,      # live 10Y from shared macro layer when available
    "risk_free_rate": 0.042,         # fallback only (provenance: risk_free_source)
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
    # per-market CAPM inputs, routed by profile.market_profile. Ke = risk_free
    # + beta*ERP(mature) + country_risk_premium. US uses the live 10Y
    # (risk_free_rate None → live path) and CRP 0. Non-US markets use a STATIC,
    # currency-matched risk-free plus a beta-INDEPENDENT country_risk_premium — the
    # lever that gives a low-beta defensive name a sensible discount rate (a bare
    # risk-free swap to a lower local rate would cut WACC and inflate the DCF; a
    # beta*ERP bump alone is dampened by low beta). No reliable free live non-US
    # source (same v1 precedent as ERP/cost_of_debt). All values tunable.
    # equity_risk_premium/cost_of_debt None → fall back to the global cfg values.
    "market_capm": {
        "US": {"risk_free_rate": None,  "country_risk_premium": 0.0,   "cost_of_debt": 0.05},
        "CN": {"risk_free_rate": 0.022, "country_risk_premium": 0.035, "cost_of_debt": 0.05},
        "HK": {"risk_free_rate": 0.035, "country_risk_premium": 0.015, "cost_of_debt": 0.05},
        "JP": {"risk_free_rate": 0.010, "country_risk_premium": 0.015, "cost_of_debt": 0.03},
        "UK": {"risk_free_rate": 0.040, "country_risk_premium": 0.010, "cost_of_debt": 0.05},
        "EU": {"risk_free_rate": 0.025, "country_risk_premium": 0.015, "cost_of_debt": 0.04},
    },
    # optional DCF-vs-comps sanity net: when the DCF base intrinsic value exceeds
    # cap × comps blended, cap the DCF weight and downgrade its confidence. None = off.
    "dcf_comps_divergence_cap": None,
    "dcf_comps_divergence_weight": 0.35,
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


def _market_and_currency(profile):
    """(market_profile, currency) from a company profile, normalizing resolver
    field aliases (market_profile_actual/config) and the RMB→CNY currency alias.
    Defaults to US/USD when absent (backward compat for old profiles)."""
    mp = (profile.get("market_profile")
          or profile.get("market_profile_actual")
          or profile.get("market_profile_config")
          or "US").upper()
    cur = (profile.get("currency") or "USD").upper()
    if cur == "RMB":
        cur = "CNY"
    return mp, cur


# ── live risk-free rate (shared macro layer) ──────────────────────────────────

_RF_BAND = (0.001, 0.10)  # sanity band for a live 10Y read (decimal)


def _rf_source_label(src):
    """macro_sources provenance → summary label: fred→DGS10, yfinance:^TNX→^TNX."""
    if not src or src == "missing":
        return None
    if src.startswith("yfinance:"):
        return src.split(":", 1)[1]
    return "DGS10" if src == "fred" else src


def _resolve_risk_free(workspace, date, cfg, market_profile="US"):
    """CAPM risk-free = live 10Y from the shared macro layer when available.

    Non-US markets first try a STATIC per-market risk-free from
    cfg["market_capm"][market_profile] (the live US 10Y is the wrong currency for
    a CNY/HKD/etc. asset). When that is set (inside _RF_BAND) it wins, with
    source "config_market:{MARKET}". Otherwise (US, or a market with no static
    rate) the resolution order is: macro_regime.json rates.us10y → raw DGS10.csv
    (regime script failed but the collector ran) → config fallback. A live value
    is accepted only inside _RF_BAND. Returns {"rate", "source", "as_of", "note"}
    where source is "config_market:{MARKET}" | "DGS10" | "^TNX" | "shared_csv" |
    "config_fallback".

    The macro layer is ADVISORY: a miss is recorded in the returned "note"
    (surfaced as macro_inputs.risk_free_note), never in the engine's
    inputs_missing — the config fallback is a perfectly valid rate and must not
    cap the valuation confidence heuristic.
    """
    fallback = {"rate": cfg["risk_free_rate"], "source": "config_fallback",
                "as_of": None, "note": None}
    # Per-market static risk-free (non-US): currency-matched to the cash flows.
    static_rf = ((cfg.get("market_capm") or {}).get(market_profile) or {}).get("risk_free_rate")
    if static_rf is not None and _RF_BAND[0] <= static_rf <= _RF_BAND[1]:
        return {"rate": static_rf, "source": f"config_market:{market_profile}",
                "as_of": None, "note": None}
    if not cfg.get("use_live_risk_free", True):
        return fallback

    shared = os.path.join(os.path.dirname(os.path.abspath(os.path.normpath(workspace))),
                          "shared", "market_context", date)
    rate, source, as_of = None, None, None

    regime = _load_json(os.path.join(shared, "indicators", "macro_regime.json"))
    if regime:
        blk = (regime.get("rates") or {}).get("us10y") or {}
        if blk.get("value") is not None and blk.get("data_quality") != "missing":
            rate = blk["value"]  # already decimal in the regime artifact
            source = _rf_source_label(blk.get("source"))
            as_of = blk.get("as_of")

    if rate is None:
        try:
            lines = open(os.path.join(shared, "raw", "macro", "DGS10.csv")).read().strip().splitlines()[1:]
            as_of, raw = lines[-1].split(",", 1)
            rate = round(float(raw) / 100.0, 6)  # CSVs store FRED percent units
            srcs = _load_json(os.path.join(shared, "raw", "macro", "macro_sources.json")) or {}
            # No provenance record → don't claim FRED; label the path honestly.
            source = _rf_source_label(((srcs.get("series") or {}).get("DGS10") or {}).get("source")) \
                or "shared_csv"
        except (OSError, ValueError, IndexError):
            rate = None

    if rate is None:
        return {**fallback, "note": "live_risk_free unavailable"}
    if not (_RF_BAND[0] <= rate <= _RF_BAND[1]):
        return {**fallback, "note": f"live_risk_free out_of_band ({rate})"}
    return {"rate": rate, "source": source or "shared_csv", "as_of": as_of, "note": None}


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


# ── degenerate-input sanitization ─────────────────────────────────────────────
# Some yfinance fundamentals carry frame-breaking artifacts that, left alone,
# distort comps. The two we guard here both show up on net-cash-rich /
# insurance-float conglomerates (e.g. Berkshire):
#   • price_to_book reported as a near-zero stub (~0.001) — not a real 0.001x
#     multiple; it would rank the name as the "cheapest on book" in the peer set.
#   • a NEGATIVE enterprise_value (and the EV/Revenue, EV/EBITDA multiples derived
#     from it) because total_cash ≫ total_debt + the EV framing. EV-based comps
#     paths then add a huge cash pile back to equity and inflate fair value.
# We null out these inputs on the COMPANY metrics (peers untouched) and return a
# list of human-readable notes for `inputs_missing`. Earnings-based (P/E) and a
# correctly-bounded P/B remain the meaningful frames for such names.
_PB_FLOOR = 0.01          # below this a price_to_book is treated as a yfinance stub
_NET_CASH_EV_RATIO = 0.30  # net-cash ≥ 30% of market cap ⇒ EV frame is degenerate


def _sanitize_company_metrics(metrics: dict) -> list[str]:
    """Mutate `metrics` in place, neutralizing degenerate comps inputs.

    Returns a list of data-quality notes describing what was excluded/sanitized.
    """
    notes: list[str] = []
    if not isinstance(metrics, dict):
        return notes

    # 1) Garbage price_to_book stub (≤0 or implausibly small).
    pb = metrics.get("price_to_book")
    if isinstance(pb, (int, float)) and pb <= _PB_FLOOR:
        metrics["price_to_book"] = None
        notes.append(
            f"price_to_book ({pb:g}) excluded: degenerate yfinance stub "
            f"(< {_PB_FLOOR}x); recompute book-value frame from equity if needed")

    # 2) Net-cash-dominated / negative enterprise value ⇒ EV multiples meaningless.
    ev = metrics.get("enterprise_value")
    mcap = metrics.get("market_cap")
    cash = metrics.get("total_cash")
    debt = metrics.get("total_debt")
    net_debt = None
    if cash is not None or debt is not None:
        net_debt = (debt or 0.0) - (cash or 0.0)
    ev_negative = isinstance(ev, (int, float)) and ev <= 0
    net_cash_heavy = (net_debt is not None and net_debt < 0
                      and isinstance(mcap, (int, float)) and mcap > 0
                      and (-net_debt) >= _NET_CASH_EV_RATIO * mcap)
    if ev_negative or net_cash_heavy:
        why = []
        if ev_negative:
            why.append(f"enterprise_value ({ev:,.0f}) is non-positive")
        if net_cash_heavy:
            why.append(f"net cash ({-net_debt:,.0f}) is "
                       f"{(-net_debt)/mcap*100:.0f}% of market cap")
        reason = "; ".join(why)
        for k in ("enterprise_value", "ev_to_revenue", "ev_to_ebitda"):
            if metrics.get(k) is not None:
                metrics[k] = None
        # flag so the comps engine skips EV→equity implied-value paths entirely
        metrics["_ev_frame_degenerate"] = True
        notes.append(
            f"EV-based multiples (enterprise_value, ev_to_revenue, ev_to_ebitda) "
            f"excluded: {reason}. EV frame distorted for a cash-rich/insurance-float "
            f"holding company; earnings (P/E) and book-value anchors used instead")
    return notes


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
        # Optional DCF-vs-comps sanity net (config-gated; off by default). A DCF
        # sitting far above the comps blended value is a divergence signal (e.g. a
        # low-beta WACC inflating a CNY name); cap its weight and downgrade its
        # confidence so it can't anchor the fair value. Only ever lowers weight.
        cap = cfg.get("dcf_comps_divergence_cap")
        comps_anchor = (implied or {}).get("blended")
        if (cap and dcf_weight > 0 and _positive(intrinsic_base)
                and _positive(comps_anchor) and intrinsic_base > cap * comps_anchor):
            wcap = cfg.get("dcf_comps_divergence_weight", 0.35)
            if dcf_weight > wcap:
                dcf_weight = wcap
            dcf_conf = _min_conf(dcf_conf, "low")
            note = ("downgraded: DCF base %.0f exceeds comps blended %.0f by >%.1fx"
                    % (intrinsic_base, comps_anchor, cap))
            dcf_reason = f"{dcf_reason}; {note}" if dcf_reason else note
    else:
        dcf_reason = "not available"
    candidates.append(_candidate("dcf", intrinsic_base, dcf_weight, dcf_conf,
                                 included=dcf_ok and dcf_weight > 0, reason=dcf_reason))

    earnings_value = (implied or {}).get("blended")
    # The blended earnings anchor averages the P/E and EV/EBITDA paths. When only
    # ONE of those survives (e.g. EV multiples excluded as degenerate for a
    # cash-rich conglomerate), the anchor rests on a single peer multiple and is
    # inherently fragile — downgrade its confidence to "low" so it can't read
    # "medium"/"high" and so the summary self-degrades rather than emitting a
    # spuriously precise fair value.
    earnings_paths = sum(1 for k in ("by_pe", "by_ev_ebitda")
                         if _positive((implied or {}).get(k)))
    earnings_conf = "medium" if earnings_paths >= 2 else "low"
    earnings_reason = None
    if not _positive(earnings_value):
        earnings_reason = "not available"
    elif earnings_paths < 2:
        earnings_reason = ("single-multiple anchor (only one of P/E / EV-EBITDA "
                           "available) — fragile")
    candidates.append(_candidate("comps_earnings", earnings_value, 0.35, earnings_conf,
                                 included=_positive(earnings_value),
                                 reason=earnings_reason))

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


def _scenario_growths(initial_growth: float, delta: float, g_term: float) -> dict:
    """Bear/base/bull initial-growth levers, bear <= base <= bull guaranteed.

    Bear is floored at terminal growth but never above base — a sub-terminal
    base (e.g. after the weak-margin haircut) would otherwise invert the range.
    """
    return {
        "bear": min(max(initial_growth - delta, g_term), initial_growth),
        "base": initial_growth,
        "bull": initial_growth + delta,
    }


def _build_fair_value_range(fair_value, method_candidates, dcf_block):
    """Coherent fair-value band (low <= base <= high by construction). The band is
    the dispersion of the INCLUDED method candidates (the same ones feeding the
    blended base) unioned with the DCF bull/bear scenarios. Because base is a
    weighted average of the included candidate values it always lies within
    [min,max] of that pool; the min/max clamps absorb 2dp rounding. Distinct from
    intrinsic_range, which stays a DCF-scenario-only band."""
    if fair_value is None:
        return None
    pool = [c["value"] for c in (method_candidates or [])
            if c.get("included") and _positive(c.get("value"))]
    if dcf_block:
        for nm in ("bear", "bull"):
            v = (dcf_block.get("scenarios", {}).get(nm, {}) or {}).get("value_per_share")
            if _positive(v):
                pool.append(v)
    if not pool:
        return None
    return {
        "low": _round(min(min(pool), fair_value)),
        "base": fair_value,
        "high": _round(max(max(pool), fair_value)),
    }


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
    # market/currency drive market-aware CAPM inputs (per-market risk-free + ERP)
    market_profile, currency = _market_and_currency(profile)

    # peers
    peer_files = sorted(glob.glob(
        os.path.join(workspace, "raw", date, "fundamentals", "peers", "*.json")))
    peers = [p for p in (_adapt_fundamentals(_load_json(f)) for f in peer_files)
             if p and p.get("metrics")]

    inputs_missing = []

    # live 10Y from the shared macro layer (config fallback when unavailable;
    # advisory — a fallback never enters inputs_missing / the confidence heuristic)
    rf = _resolve_risk_free(workspace, date, cfg, market_profile)

    # ── degenerate-input sanitization ─────────────────────────────────────────
    # Neutralize frame-breaking yfinance artifacts (e.g. near-zero price_to_book
    # stub, negative/net-cash-dominated enterprise value) before they reach comps.
    inputs_missing.extend(_sanitize_company_metrics(metrics))

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
        # market-aware CAPM: mature ERP (global, override per market), a flat
        # country risk premium, and cost of debt (fall back to global cfg values)
        _mc = (cfg.get("market_capm") or {}).get(market_profile) or {}
        erp = _mc.get("equity_risk_premium")
        if erp is None:
            erp = cfg["equity_risk_premium"]
        cod = _mc.get("cost_of_debt")
        if cod is None:
            cod = cfg["cost_of_debt"]
        crp = _mc.get("country_risk_premium") or 0.0
        wacc_rate = dcf_mod.wacc(
            risk_free=rf["rate"], beta=beta,
            equity_risk_premium=erp,
            equity_value=metrics.get("market_cap") or 0.0,
            debt_value=metrics.get("total_debt") or 0.0,
            cost_of_debt=cod, tax_rate=tax_rate,
            country_risk_premium=crp,
        )
        g_term = cfg["default_terminal_growth"]
        # keep WACC strictly above terminal growth
        if wacc_rate <= g_term:
            wacc_rate = g_term + 0.03

        # Two-stage fade DCF: the high-growth phase (capped at max_initial_growth,
        # well above the legacy 0.20 constant-growth cap) fades linearly to terminal
        # growth over fade_years, so fast growers are not understated by a single
        # constant rate. Scenarios move the *initial* growth lever; bear is floored
        # at terminal but never above base (a sub-terminal base — e.g. after the
        # weak-margin haircut — would otherwise invert it), so bear <= base <= bull
        # holds. growth_schedule fades up or down, so a sub-terminal bear is valid.
        # Deterministic, fundamentals-driven growth selection (3yr revenue CAGR
        # preferred, with divergence / profitability / sector / maturity guards).
        gsel = dcf_mod.select_growth(income, metrics, cfg, sector=sector, industry=industry)
        initial_growth = gsel["initial_growth"]
        delta = cfg["scenario_growth_delta"]
        fade_years = cfg["fade_years"]

        scenarios = {}
        for name, ig in _scenario_growths(initial_growth, delta, g_term).items():
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
            "risk_free_rate": _round(rf["rate"], 4),
            "risk_free_source": rf["source"],
            "equity_risk_premium": _round(erp, 4),
            "country_risk_premium": _round(crp, 4),
            "cost_of_debt": _round(cod, 4),
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
    dcf_cand = next((c for c in method_candidates if c.get("method") == "dcf"), None)
    if (dcf_block is not None and dcf_cand is not None and not dcf_cand.get("included")
            and "non-convergent" in (dcf_cand.get("reason") or "")):
        # The sensitivity grid was built from the same non-convergent growth, so
        # flag the whole DCF block as illustrative-only for downstream readers.
        dcf_block["excluded_from_fair_value"] = True
    # fair_value_range: coherent by construction (low <= base <= high), distinct
    # from the DCF-scenario-only intrinsic_range below.
    fair_value_range = _build_fair_value_range(fair_value, method_candidates, dcf_block)

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
        "meta": {"market_profile": market_profile, "currency": currency},
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
        "macro_inputs": {
            "risk_free_rate": _round(rf["rate"], 4),
            "risk_free_source": rf["source"],
            "risk_free_as_of": rf["as_of"],
            "risk_free_note": rf["note"],
        },
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

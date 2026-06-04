#!/usr/bin/env python3
"""Valuation engine runner — Stage 5b (valuation).

Reads fundamentals collected by the company desk, runs a scenario DCF
(bull/base/bear) and a peer comps analysis, and writes structured artifacts
for downstream analysts, the decision maker, and the report writer.

Usage:
    .venv/bin/python3 valuation/run_valuation.py <workspace> <date>

Outputs (under {workspace}/valuation/{date}/):
    valuation_summary.json   — intrinsic range, margin of safety, comps, flags
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
    "max_base_growth": 0.20,
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
    # Support two schemas:
    #   schema A (company-desk v1): raw["valuation_ratios"] = {camelCase: value}
    #   schema B (direct yfinance): raw["info"] = {camelCase: value}
    ratios = raw.get("valuation_ratios") or raw.get("info") or {}
    metrics = {}
    for src, dst in _RATIO_MAP.items():
        v = ratios.get(src)
        if v is not None and not (isinstance(v, float) and v != v):  # skip NaN
            metrics[dst] = v
    # quote_type: prefer metadata, then info/valuation_ratios field
    qt = (raw.get("metadata") or {}).get("quote_type") or ratios.get("quoteType")
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

    Handles three input schemas:
      • already-normalized: has `metrics` key → pass through
      • company-desk v1: has `valuation_ratios` + `financials` keys
      • direct yfinance: has `info` + `income_statement` + `cash_flow` keys
    """
    if not isinstance(raw, dict):
        return raw
    if raw.get("metrics"):
        return raw
    # Accept either schema; fall through if neither recognized key set is present.
    has_ratios_schema = "valuation_ratios" in raw or "financials" in raw
    has_info_schema = "info" in raw or "income_statement" in raw or "cash_flow" in raw
    if not has_ratios_schema and not has_info_schema:
        return raw
    income, cash_flow = _norm_statements(raw)
    return {
        "ticker": raw.get("ticker"),
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

        rev_growth = metrics.get("revenue_growth")
        base_growth = cfg["base_growth_fallback"]
        if isinstance(rev_growth, (int, float)) and rev_growth > 0:
            base_growth = min(rev_growth, cfg["max_base_growth"])
        delta = cfg["scenario_growth_delta"]
        years = cfg["projection_years"]

        scenarios = {}
        for name, g in (("bear", max(base_growth - delta, 0.0)),
                        ("base", base_growth),
                        ("bull", base_growth + delta)):
            res = dcf_mod.intrinsic_value_per_share(
                base, g, years, wacc_rate, g_term, net_debt, shares)
            scenarios[name] = {
                "growth": _round(g, 4),
                "value_per_share": _round(res["value_per_share"]),
                "tv_fraction": _round(res["tv_fraction"], 3),
            }

        base_vps = scenarios["base"]["value_per_share"]
        tv_frac = scenarios["base"]["tv_fraction"]
        dcf_block = {
            "wacc": _round(wacc_rate, 4),
            "terminal_growth": _round(g_term, 4),
            "tax_rate": _round(tax_rate, 4),
            "beta": _round(beta, 3),
            "base_fcff": _round(base),
            "net_debt": _round(net_debt),
            "projection_years": years,
            "scenarios": scenarios,
            "tv_fraction_in_band": (tv_frac is not None
                                    and dcf_mod.TV_FRACTION_MIN <= tv_frac <= dcf_mod.TV_FRACTION_MAX),
        }

        grid = dcf_mod.sensitivity_grid(
            base, base_growth, years, net_debt, shares,
            wacc_center=wacc_rate, g_center=g_term, n=cfg["sensitivity_dims"])
        grid["center_value"] = base_vps  # equals scenarios.base — QC asserts this
        sensitivity = grid

    # ── margin of safety (base intrinsic vs price) ───────────────────────────
    margin_of_safety = None
    intrinsic_base = (dcf_block or {}).get("scenarios", {}).get("base", {}).get("value_per_share")
    if intrinsic_base and price:
        margin_of_safety = round((intrinsic_base - price) / price, 4)

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
    if not (inputs_missing) and len(peers) >= 3:
        confidence = "high"
    elif critical_missing & set(inputs_missing) or len(peers) < 2:
        confidence = "low"
    else:
        confidence = "medium"

    # ── summary text ─────────────────────────────────────────────────────────
    if lang == "ch":
        mos_txt = (f"{margin_of_safety*100:.0f}%" if margin_of_safety is not None else "不可计算")
        summary_text = (f"{ticker} 现价 {price}，基准内在价值 "
                        f"{intrinsic_base if intrinsic_base else '不可计算'}，"
                        f"安全边际 {mos_txt}，估值判断：{verdict}（置信度 {confidence}）。")
    else:
        mos_txt = (f"{margin_of_safety*100:.0f}%" if margin_of_safety is not None else "n/a")
        summary_text = (f"{ticker} at {price}: base-case intrinsic value "
                        f"{intrinsic_base if intrinsic_base else 'n/a'}, margin of safety "
                        f"{mos_txt}, verdict {verdict} (confidence {confidence}).")

    summary = {
        "ticker": ticker,
        "applicable": True,
        "confidence": confidence,
        "current_price": _round(price) if price else None,
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

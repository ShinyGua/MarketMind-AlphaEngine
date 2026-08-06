#!/usr/bin/env python3
"""Fundamentals from SEC EDGAR XBRL — the Yahoo-free valuation input collector.

Replaces the yfinance ``get_fundamentals`` path for US filers. Emits the exact
normalized shape the valuation engine already consumes (see the adapter comment
in ``valuation/run_valuation.py``)::

    {"ticker", "metrics": {snake_case}, "income_statement": [...],
     "cash_flow": [...], "metadata": {...}}

Statement rows are annual (10-K / FY), newest first.

Derived rather than reported (EDGAR carries filings, not market data):
  market_cap        = current_price x shares_outstanding
  enterprise_value  = market_cap + total_debt - total_cash
  ebitda            = ebit + dep_amort
  beta              = 250d OLS slope of daily returns vs SPY (both NASDAQ API)
  trailing_pe, price_to_book, ev_to_revenue, ev_to_ebitda, margins, growth, roe

``forward_pe`` needs sell-side estimates and has no free keyless source, so it
is omitted and reported in ``metadata.inputs_missing`` — the engine already
degrades on missing metrics rather than aborting.

Usage:
    fetch_fundamentals_edgar.py MU --out raw/2026-08-03/fundamentals/MU.json
    fetch_fundamentals_edgar.py --workspace workspaces/MU --date 2026-08-03
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# NOTE: www.sec.gov (which hosts the convenient company_tickers.json ticker->CIK
# map) is not reachable from every network this runs on, so CIK resolution goes
# through efts.sec.gov full-text search and is verified against data.sec.gov.
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
CIK_CACHE = Path(__file__).resolve().parent.parent / "workspaces" / "shared" / "cik_cache.json"

# SEC requires a descriptive UA with contact info.
UA = "MarketMind-AlphaEngine research (xiyuwang.usyd@gmail.com)"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

REQUEST_DELAY_S = 0.15  # SEC fair-access: <10 req/s

# First tag that the filer actually reports wins.
TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "ebit": ["OperatingIncomeLoss"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"],
    "tax_provision": ["IncomeTaxExpenseBenefit"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "dep_amort": ["DepreciationDepletionAndAmortization",
                  "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization"],
}
BALANCE_TAGS = {
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "short_term_investments": ["ShortTermInvestments", "OtherShortTermInvestments",
                               "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "gross_profit": ["GrossProfit"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "st_debt": ["LongTermDebtCurrent", "DebtCurrent"],
    # Total debt should include capitalised leases (the prior yfinance input did),
    # otherwise enterprise value is understated.
    "lt_lease": ["FinanceLeaseLiabilityNoncurrent", "OperatingLeaseLiabilityNoncurrent"],
    "st_lease": ["FinanceLeaseLiabilityCurrent", "OperatingLeaseLiabilityCurrent"],
}


def _get(url: str, params: dict | None = None, timeout: int = 30, retries: int = 3):
    import requests

    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if resp.status_code >= 400:
                raise RuntimeError(f"http_{resp.status_code}")
            return resp.json()
        except Exception:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    return None


def _load_cik_cache() -> dict:
    try:
        return json.loads(CIK_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_cik_cache(cache: dict) -> None:
    try:
        CIK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CIK_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass  # cache is an optimisation, never a hard dependency


def resolve_cik(ticker: str) -> str | None:
    """ticker -> zero-padded CIK, via EDGAR full-text search + submissions check."""
    t = ticker.upper()
    cache = _load_cik_cache()
    if t in cache:
        return cache[t]

    try:
        hits = (_get(SEARCH_URL, {"q": "", "forms": "10-K", "entityName": t})
                or {}).get("hits", {}).get("hits", [])
    except Exception:  # noqa: BLE001
        return None

    candidates: list[str] = []
    for h in hits:
        for c in (h.get("_source", {}).get("ciks") or []):
            if c not in candidates:
                candidates.append(c)

    # Full-text search ranks by relevance, not by ticker identity — confirm the
    # candidate actually trades under this symbol before trusting it.
    for cik in candidates[:6]:
        try:
            sub = _get(SUBMISSIONS_URL.format(cik=cik))
        except Exception:  # noqa: BLE001
            continue
        if t in [str(x).upper() for x in (sub or {}).get("tickers", [])]:
            cache[t] = cik
            _save_cik_cache(cache)
            return cik
        time.sleep(REQUEST_DELAY_S)
    return None


def _usd_entries(facts: dict, name: str) -> list[dict] | None:
    node = facts.get("facts", {}).get("us-gaap", {}).get(name)
    if not node:
        return None
    units = node.get("units", {})
    return units.get("USD") or next((v for k, v in units.items() if k.startswith("USD")), None)


def _candidate_series(facts: dict, names: list[str]) -> list[list[dict]]:
    """Candidate tags ordered by data freshness, newest coverage first.

    Filers migrate tags: NVIDIA reported
    ``RevenueFromContractWithCustomerExcludingAssessedTax`` through FY2022 and
    ``Revenues`` after. Taking the first tag that merely *exists* would return
    a figure years out of date, so rank by each tag's most recent period end and
    let the preference order in TAGS break ties only.
    """
    ranked = []
    for i, name in enumerate(names):
        series = _usd_entries(facts, name)
        if not series:
            continue
        latest = max((e.get("end") or "" for e in series), default="")
        ranked.append((latest, -i, series))
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [r[2] for r in ranked]


def _annual(facts: dict, names: list[str], instant: bool = False) -> dict[str, float]:
    """{fiscal_year: value} from the freshest reported tag, annual periods only."""
    for series in _candidate_series(facts, names):
        out: dict[str, tuple[str, float]] = {}
        for e in series:
            if e.get("form") not in ("10-K", "10-K/A"):
                continue
            end = e.get("end")
            val = e.get("val")
            if end is None or val is None:
                continue
            if not instant:
                start = e.get("start")
                if not start:
                    continue
                try:
                    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
                except ValueError:
                    continue
                if not (330 <= days <= 400):  # annual duration only, never a quarter
                    continue
            fy = str(e.get("fy") or end[:4])
            # Later filings restate; keep the entry with the newest 'end' per year.
            if fy not in out or end > out[fy][0]:
                out[fy] = (end, float(val))
        if out:
            return {k: v[1] for k, v in out.items()}
    return {}


def _latest(facts: dict, names: list[str], instant: bool = True) -> float | None:
    """Most recent balance-sheet (instant) value from ANY form.

    Deliberately not restricted to 10-K: a 10-Q balance sheet is up to three
    quarters fresher, and stale cash/debt/equity would distort enterprise value
    and price-to-book.
    """
    for series in _candidate_series(facts, names):
        best_end, best_val = "", None
        for e in series:
            if e.get("start") and instant:
                continue  # a duration entry is not a balance
            end, val = e.get("end"), e.get("val")
            if end and val is not None and end > best_end:
                best_end, best_val = end, float(val)
        if best_val is not None:
            return best_val
    return None


def _period_days(start: str, end: str) -> int | None:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
    except (ValueError, TypeError):
        return None


def _ttm_cumulative(entries: list[dict], asof: str | None = None) -> float | None:
    """TTM for statements filed year-to-date rather than as discrete quarters.

    Cash-flow lines in a 10-Q are cumulative from the fiscal year start (90d,
    181d, 272d...), so summing "quarters" would double-count. The standard
    reconstruction is::

        TTM = latest_YTD + prior_full_year - prior_year_YTD_of_equal_length

    Returns None unless all three legs line up, so a partial match degrades to
    the caller's fiscal-year fallback instead of inventing a number.
    """
    durations = []
    for e in entries:
        start, end, val = e.get("start"), e.get("end"), e.get("val")
        if not start or not end or val is None:
            continue
        if asof and end > asof:
            continue
        days = _period_days(start, end)
        if days is None or days < 60:
            continue
        durations.append((start, end, days, float(val)))
    if not durations:
        return None

    annuals = [d for d in durations if 330 <= d[2] <= 400]
    partials = [d for d in durations if d[2] < 330]
    if not partials or not annuals:
        return max(annuals, key=lambda d: d[1])[3] if annuals else None

    latest = max(partials, key=lambda d: d[1])
    # The prior full year is the annual ending just before this YTD window opens.
    prior_annual = None
    for a in sorted(annuals, key=lambda d: d[1], reverse=True):
        if a[1] < latest[0]:
            prior_annual = a
            break
    if prior_annual is None:
        return None
    # Same-length YTD window one year earlier, anchored on that year's start.
    prior_ytd = next((p for p in partials
                      if p[0] == prior_annual[0] and abs(p[2] - latest[2]) <= 10), None)
    if prior_ytd is None:
        return None
    return latest[3] + prior_annual[3] - prior_ytd[3]


def _ttm(facts: dict, names: list[str], asof: str | None = None) -> float | None:
    """Trailing-twelve-month total from quarterly XBRL facts.

    yfinance reported TTM metrics; the annual 10-K figure can be up to a year
    stale, which for a fast-ramping filer misstates every multiple. So rebuild
    TTM from the four most recent contiguous quarters.

    Q4 is normally never filed as a 10-Q (it is only inside the 10-K), so a
    missing quarter is derived as ``annual - sum(the year's other quarters)``.
    """
    for series in _candidate_series(facts, names):
        quarters: dict[tuple[str, str], tuple[str, float]] = {}
        annuals: dict[tuple[str, str], tuple[str, float]] = {}
        for e in series:
            start, end, val = e.get("start"), e.get("end"), e.get("val")
            if not start or not end or val is None:
                continue
            days = _period_days(start, end)
            if days is None:
                continue
            key = (start, end)
            filed = e.get("filed") or ""
            if 80 <= days <= 100:
                # Restatements repeat a period; keep the latest-filed value.
                if key not in quarters or filed >= quarters[key][0]:
                    quarters[key] = (filed, float(val))
            elif 330 <= days <= 400:
                if key not in annuals or filed >= annuals[key][0]:
                    annuals[key] = (filed, float(val))

        # Derive each year's unfiled quarter from the annual minus the filed ones.
        for (astart, aend), (_f, aval) in annuals.items():
            inside = [(s, e, v[1]) for (s, e), v in quarters.items() if s >= astart and e <= aend]
            if len(inside) != 3:
                continue
            inside.sort()
            covered = sum(v for _s, _e, v in inside)
            # The gap is whichever edge the filed quarters leave open.
            if inside[0][0] > astart:
                gap = (astart, inside[0][0])
            elif inside[-1][1] < aend:
                gap = (inside[-1][1], aend)
            else:
                continue
            quarters.setdefault(gap, ("derived", aval - covered))

        if quarters:
            rows = sorted(((s, e, v[1]) for (s, e), v in quarters.items()), key=lambda r: r[1])
            if asof:
                rows = [r for r in rows if r[1] <= asof]
            if len(rows) >= 4:
                window = rows[-4:]
                # Only trust a genuinely contiguous year (no gap, no overlap).
                span = _period_days(window[0][0], window[-1][1])
                if span is not None and 330 <= span <= 400:
                    return sum(v for _s, _e, v in window)

        # No usable discrete quarters — the filer reports this line cumulatively
        # (the cash-flow statement always does).
        cumulative = _ttm_cumulative(series, asof=asof)
        if cumulative is not None:
            return cumulative
    return None


def _shares_outstanding(facts: dict) -> float | None:
    dei = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    if not dei:
        return None
    best_end, best_val = "", None
    for entries in dei.get("units", {}).values():
        for e in entries:
            end = e.get("end") or ""
            if e.get("val") is not None and end > best_end:
                best_end, best_val = end, float(e["val"])
    return best_val


def _price_series(symbol: str, end: str | None, months: int = 14):
    from fetch_prices_nasdaq import fetch_daily
    rows, _ = fetch_daily(symbol, months=months, end=end)
    return rows


# 5 years of daily bars, resampled to month-ends for the CAPM beta fit.
_BETA_MONTHS = 62


def _beta(sym_rows: list[dict], bench_rows: list[dict]) -> float | None:
    """CAPM beta: OLS slope of ~5y MONTHLY returns vs the benchmark.

    Monthly-over-5-years is the CAPM convention (and what the previous yfinance
    input used). A 250d *daily* beta on a name in a violent sector run reads far
    higher, and since beta drives WACC it would quietly depress every DCF value.
    Falls back to daily only when there is too little history for a monthly fit.
    """
    def month_end_closes(rows):
        by_month: dict[str, tuple[str, float]] = {}
        for r in rows:
            d = r["Date"]
            key = d[:7]
            if key not in by_month or d > by_month[key][0]:
                by_month[key] = (d, r["Close"])
        return {k: v[1] for k, v in by_month.items()}

    def rets_from(closes: dict) -> dict:
        out, prev = {}, None
        for k in sorted(closes):
            c = closes[k]
            if prev and prev > 0:
                out[k] = c / prev - 1.0
            prev = c
        return out

    a = rets_from(month_end_closes(sym_rows))
    b = rets_from(month_end_closes(bench_rows))
    common = sorted(set(a) & set(b))[-60:]  # 5 years of monthly observations

    if len(common) < 24:  # too short for a monthly fit — fall back to daily
        def daily(rows):
            out, prev = {}, None
            for r in rows:
                c = r["Close"]
                if prev and prev > 0:
                    out[r["Date"]] = c / prev - 1.0
                prev = c
            return out
        a, b = daily(sym_rows), daily(bench_rows)
        common = sorted(set(a) & set(b))[-250:]
    if len(common) < 24:
        return None
    xs = [b[d] for d in common]
    ys = [a[d] for d in common]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(cov / var, 4)


def _safe_div(a, b):
    try:
        if a is None or not b:
            return None
        return round(a / b, 6)
    except (TypeError, ZeroDivisionError):
        return None


def build(ticker: str, end: str | None = None, with_beta: bool = True) -> dict:
    cik = resolve_cik(ticker)
    if not cik:
        raise RuntimeError(f"{ticker}: no CIK on SEC (non-US filer?)")
    time.sleep(REQUEST_DELAY_S)
    facts = _get(FACTS_URL.format(cik=cik))

    flows = {k: _annual(facts, names) for k, names in TAGS.items()}
    years = sorted({y for s in flows.values() for y in s}, reverse=True)[:5]

    income_statement, cash_flow = [], []
    for y in years:
        income_statement.append({
            "period": y,
            "revenue": flows["revenue"].get(y),
            "ebit": flows["ebit"].get(y),
            "pretax_income": flows["pretax_income"].get(y),
            "tax_provision": flows["tax_provision"].get(y),
            "net_income": flows["net_income"].get(y),
        })
        capex = flows["capex"].get(y)
        cash_flow.append({
            "period": y,
            "operating_cash_flow": flows["operating_cash_flow"].get(y),
            # EDGAR reports capex as a positive outflow; the engine expects the
            # yfinance sign convention (negative).
            "capex": -abs(capex) if capex is not None else None,
            "dep_amort": flows["dep_amort"].get(y),
        })

    bal = {k: _latest(facts, names) for k, names in BALANCE_TAGS.items()}
    total_cash = (bal.get("cash") or 0) + (bal.get("short_term_investments") or 0) or bal.get("cash")
    total_debt = sum(bal.get(k) or 0 for k in ("lt_debt", "st_debt", "lt_lease", "st_lease")) \
        or bal.get("lt_debt")

    shares = _shares_outstanding(facts)
    inputs_missing = []

    price_rows = _price_series(ticker, end)
    current_price = price_rows[-1]["Close"] if price_rows else None
    if current_price is None:
        inputs_missing.append("current_price")

    beta = None
    if with_beta and price_rows:
        try:
            beta = _beta(_price_series(ticker, end, months=_BETA_MONTHS),
                         _price_series("SPY", end, months=_BETA_MONTHS))
        except Exception:  # noqa: BLE001
            beta = None
    if beta is None:
        inputs_missing.append("beta")

    latest_is = income_statement[0] if income_statement else {}
    prev_is = income_statement[1] if len(income_statement) > 1 else {}
    latest_cf = cash_flow[0] if cash_flow else {}

    # Headline multiples must be TTM (what yfinance supplied); a 10-K figure can
    # be almost a year stale, which badly misstates every multiple for a filer
    # whose run-rate is moving fast. Fall back to the last full fiscal year only
    # when the quarterly chain is incomplete, and say so in metadata.
    basis_stale: list[str] = []

    def pick(key: str, tags: list[str], fallback):
        val = _ttm(facts, tags, asof=end)
        if val is None:
            if fallback is not None:
                basis_stale.append(key)
            return fallback
        return val

    revenue = pick("revenue", TAGS["revenue"], latest_is.get("revenue"))
    net_income = pick("net_income", TAGS["net_income"], latest_is.get("net_income"))
    ebit = pick("ebit", TAGS["ebit"], latest_is.get("ebit"))
    dep = pick("dep_amort", TAGS["dep_amort"], latest_cf.get("dep_amort"))
    ocf = pick("operating_cash_flow", TAGS["operating_cash_flow"],
               latest_cf.get("operating_cash_flow"))
    gross_profit = pick("gross_profit", BALANCE_TAGS["gross_profit"], None)
    _capex_ttm = _ttm(facts, TAGS["capex"], asof=end)
    capex = -abs(_capex_ttm) if _capex_ttm is not None else latest_cf.get("capex")

    ebitda = (ebit + dep) if (ebit is not None and dep is not None) else None
    fcf = (ocf + capex) if (ocf is not None and capex is not None) else None

    # Growth compares TTM against the TTM window one year earlier, so both legs
    # sit on the same basis (mixing TTM against a fiscal year would inflate it).
    prior_asof = None
    if end:
        try:
            prior_asof = end[:10].replace(end[:4], str(int(end[:4]) - 1), 1)
        except ValueError:
            prior_asof = None
    prev_revenue = _ttm(facts, TAGS["revenue"], asof=prior_asof) if prior_asof else None
    prev_net_income = _ttm(facts, TAGS["net_income"], asof=prior_asof) if prior_asof else None
    if prev_revenue is None:
        prev_revenue = prev_is.get("revenue")
    if prev_net_income is None:
        prev_net_income = prev_is.get("net_income")

    market_cap = (current_price * shares) if (current_price and shares) else None
    ev = (market_cap + (total_debt or 0) - (total_cash or 0)) if market_cap else None
    equity = bal.get("equity")

    for key, val in (("shares_outstanding", shares), ("market_cap", market_cap),
                     ("total_revenue", revenue), ("ebitda", ebitda)):
        if val is None:
            inputs_missing.append(key)
    inputs_missing.append("forward_pe")  # no free keyless source for estimates

    metrics = {
        "quote_type": "EQUITY",
        "currency": "USD",
        "market_cap": market_cap,
        "enterprise_value": ev,
        "trailing_pe": _safe_div(market_cap, net_income),
        "price_to_book": _safe_div(market_cap, equity),
        "ev_to_revenue": _safe_div(ev, revenue),
        "ev_to_ebitda": _safe_div(ev, ebitda),
        "profit_margins": _safe_div(net_income, revenue),
        "operating_margins": _safe_div(ebit, revenue),
        "gross_margins": _safe_div(gross_profit, revenue),
        "revenue_growth": _safe_div(revenue - prev_revenue, prev_revenue)
            if revenue and prev_revenue else None,
        "earnings_growth": _safe_div(net_income - prev_net_income, abs(prev_net_income))
            if net_income is not None and prev_net_income else None,
        "return_on_equity": _safe_div(net_income, equity),
        "free_cashflow": fcf,
        "operating_cashflow": ocf,
        "total_cash": total_cash,
        "total_debt": total_debt,
        "total_revenue": revenue,
        "ebitda": ebitda,
        "shares_outstanding": shares,
        "beta": beta,
        "current_price": current_price,
    }
    metrics = {k: v for k, v in metrics.items() if v is not None}

    # Base year for the DCF. The latest 10-K can be nearly a year stale, so the
    # engine prefers this window (valuation/dcf.py::base_fcff) and falls back to
    # income_statement[0] / cash_flow[0] when it is absent.
    ttm_block = {
        "period": "TTM",
        "revenue": revenue,
        "ebit": ebit,
        "net_income": net_income,
        "dep_amort": dep,
        "capex": capex,
        "operating_cash_flow": ocf,
    }
    if any(v is None for v in (ebit, capex)) and any(v is None for v in (ocf, capex)):
        ttm_block = None  # neither FCFF path is computable — let the engine fall back

    return {
        "ticker": ticker.upper(),
        "metrics": metrics,
        "ttm": ttm_block,
        "income_statement": income_statement,
        "cash_flow": cash_flow,
        "metadata": {
            "source": "sec_edgar_xbrl",
            "cik": cik,
            "price_source": "api.nasdaq.com",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "inputs_missing": sorted(set(inputs_missing)),
            "metrics_basis": "ttm" if not basis_stale else "mixed",
            "fiscal_year_fallback": sorted(set(basis_stale)),
            "note": ("Annual statements from 10-K XBRL facts; headline metrics are TTM "
                     "(last 4 quarters, unfiled Q4 derived from the annual). Market "
                     "metrics derived from NASDAQ close x EDGAR shares. "
                     "Yahoo/yfinance not used."),
        },
    }


def _peers(workspace: Path) -> list[str]:
    try:
        data = json.loads((workspace / "profile" / "peer_set.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    peers = data.get("peers", data) if isinstance(data, dict) else data
    return [str(p.get("ticker") if isinstance(p, dict) else p) for p in (peers or [])
            if (p.get("ticker") if isinstance(p, dict) else p)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Fundamentals from SEC EDGAR (Yahoo-free)")
    ap.add_argument("ticker", nargs="?")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--end", help="as-of date YYYY-MM-DD for the price leg")
    ap.add_argument("--workspace", type=Path)
    ap.add_argument("--date")
    args = ap.parse_args()

    if args.workspace:
        if not args.date:
            ap.error("--workspace requires --date")
        profile = {}
        try:
            profile = json.loads((args.workspace / "profile" / "company_profile.json")
                                 .read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        ticker = profile.get("ticker") or args.workspace.name
        base = args.workspace / "raw" / args.date / "fundamentals"
        ok = 0
        for sym, out in [(ticker, base / f"{ticker}.json")] + \
                        [(p, base / "peers" / f"{p}.json") for p in _peers(args.workspace)]:
            try:
                doc = build(sym, end=args.date, with_beta=(sym == ticker))
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                m = doc["metrics"]
                print(f"  ok   {sym:<8} mcap={m.get('market_cap')} rev={m.get('total_revenue')} "
                      f"missing={len(doc['metadata']['inputs_missing'])}")
                ok += 1
            except Exception as exc:  # noqa: BLE001 — never abort the desk
                print(f"  FAIL {sym:<8} {exc}")
            time.sleep(REQUEST_DELAY_S)
        print(f"fundamentals: {ok} written")
        return 0 if ok else 1

    if not args.ticker:
        ap.error("provide a ticker or --workspace")
    doc = build(args.ticker, end=args.end)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(json.dumps(doc["metrics"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

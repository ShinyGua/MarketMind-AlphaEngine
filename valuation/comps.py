"""Peer comparable-company analysis primitives.

Pure functions — no I/O. Follows the financial-services comps-analysis
discipline: benchmark against peer quartiles (25th / median / 75th), not
averages, and rank the company's percentile within the peer set.
"""

from __future__ import annotations

# Multiples we benchmark. Lower percentile == cheaper on that multiple.
COMPS_MULTIPLES = ["ev_to_revenue", "ev_to_ebitda", "trailing_pe", "forward_pe", "price_to_book"]
# Descriptive metrics shown alongside the multiples for context.
COMPS_CONTEXT = ["revenue_growth", "profit_margins", "operating_margins"]


def _clean(values: list) -> list[float]:
    """Drop None and non-positive values (negative multiples are meaningless)."""
    out = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolation percentile (q in [0,1]); expects a sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def quartiles(values: list) -> dict:
    """{p25, median, p75, n} over cleaned values (None if nothing usable)."""
    vals = sorted(_clean(values))
    return {
        "p25": _percentile(vals, 0.25),
        "median": _percentile(vals, 0.50),
        "p75": _percentile(vals, 0.75),
        "n": len(vals),
    }


def percentile_rank(value, peer_values: list) -> float | None:
    """Fraction of peers a positive `value` is >= (0..1). None if not computable."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    peers = _clean(peer_values)
    if not peers:
        return None
    below = sum(1 for p in peers if v >= p)
    return below / len(peers)


def build_comps(ticker: str, company: dict, peers: list[dict]) -> dict:
    """Comps table: per-name multiples, peer quartiles, and company percentile rank.

    `company` and each peer are the `metrics` dicts from get_fundamentals.
    Returns {rows, benchmarks, company} where benchmarks[multiple] has quartiles
    and the company's percentile_rank for that multiple.
    """
    fields = COMPS_MULTIPLES + COMPS_CONTEXT

    def row_for(name, metrics):
        r = {"ticker": name}
        for f in fields:
            r[f] = metrics.get(f)
        return r

    rows = [row_for(ticker, company)] + [
        row_for(p.get("ticker", "?"), p.get("metrics", p)) for p in peers
    ]

    peer_metric_dicts = [p.get("metrics", p) for p in peers]
    benchmarks = {}
    for m in COMPS_MULTIPLES:
        peer_vals = [pm.get(m) for pm in peer_metric_dicts]
        benchmarks[m] = {
            **quartiles(peer_vals),
            "company": company.get(m),
            "company_percentile": percentile_rank(company.get(m), peer_vals),
        }

    return {"rows": rows, "benchmarks": benchmarks}


def implied_value_per_share(company: dict, benchmarks: dict) -> dict:
    """Implied price from applying the peer *median* multiple to company metrics.

    Uses forward P/E and EV/EBITDA where the underlying company inputs exist.
    Returns {by_pe, by_ev_ebitda, blended} (per share; None where not computable).
    """
    out = {"by_pe": None, "by_ev_ebitda": None, "blended": None}

    # P/E path: median peer P/E × company EPS. EPS ≈ price / company P/E.
    price = company.get("current_price")
    fwd_pe = company.get("forward_pe") or company.get("trailing_pe")
    peer_pe_median = (benchmarks.get("forward_pe") or {}).get("median") \
        or (benchmarks.get("trailing_pe") or {}).get("median")
    if price and fwd_pe and peer_pe_median and fwd_pe > 0:
        eps = price / fwd_pe
        out["by_pe"] = eps * peer_pe_median

    # EV/EBITDA path: median peer EV/EBITDA × company EBITDA → EV → equity → /share.
    ebitda = company.get("ebitda")
    shares = company.get("shares_outstanding")
    net_debt = None
    if company.get("total_debt") is not None or company.get("total_cash") is not None:
        net_debt = (company.get("total_debt") or 0.0) - (company.get("total_cash") or 0.0)
    peer_ev_ebitda_median = (benchmarks.get("ev_to_ebitda") or {}).get("median")
    if ebitda and shares and peer_ev_ebitda_median and net_debt is not None:
        implied_ev = peer_ev_ebitda_median * ebitda
        out["by_ev_ebitda"] = (implied_ev - net_debt) / shares

    vals = [v for v in (out["by_pe"], out["by_ev_ebitda"]) if v is not None and v > 0]
    if vals:
        out["blended"] = sum(vals) / len(vals)
    return out

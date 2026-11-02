"""EDGAR extraction on synthetic companyfacts, shaped after the filings that broke
the HOOD run (2026-10-29): SCHW's proxy-statement net income, SOFI's bank revenue
tag, HOOD's per-class cover counts, IBKR's non-controlling interest."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_fundamentals_edgar as E  # noqa: E402

Q = [("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"), ("2025-07-01", "2025-09-30"),
     ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30")]


def _flow(quarters: list[float], annual_2025: float, form_extra: list[dict] = ()) -> list[dict]:
    rows = [{"start": s, "end": e, "val": v, "form": "10-Q", "filed": e[:8] + "28"}
            for (s, e), v in zip(Q, quarters)]
    rows.append({"start": "2025-01-01", "end": "2025-12-31", "val": annual_2025,
                 "form": "10-K", "filed": "2026-02-20"})
    return rows + list(form_extra)


def _facts(us_gaap: dict, dei: dict | None = None) -> dict:
    return {"facts": {"us-gaap": {k: {"units": v} for k, v in us_gaap.items()},
                      "dei": {k: {"units": v} for k, v in (dei or {}).items()}}}


def test_proxy_statement_value_does_not_override_the_10k():
    # SCHW: the DEF 14A, filed after the 10-K, tags FY2025 net income in the wrong scale
    proxy = {"start": "2025-01-01", "end": "2025-12-31", "val": 8_852_000,
             "form": "DEF 14A", "filed": "2026-03-27"}
    ni = _flow([1909e6, 2126e6, 2358e6, 2479e6, 2800e6], 8852e6, [proxy])
    facts = _facts({"NetIncomeLoss": {"USD": ni}})
    q4 = 8852e6 - (1909e6 + 2126e6 + 2358e6)
    assert E._ttm(facts, E.TAGS["net_income"], asof="2026-10-29") == 2358e6 + q4 + 2479e6 + 2800e6


def test_bank_net_revenue_beats_a_revenues_sub_line():
    # SOFI: "Revenues" is a ~$150M/quarter sub-line; the total is net of interest expense
    sub = _flow([140e6, 151e6, 156e6, 129e6, 153e6], 600e6)
    total = _flow([770e6, 858e6, 961e6, 1000e6, 1090e6], 3600e6)
    facts = _facts({"Revenues": {"USD": sub}, "RevenuesNetOfInterestExpense": {"USD": total}})
    assert E._ttm(facts, E.TAGS["revenue"], asof="2026-10-29") > 3e9


def test_net_income_skips_consolidated_profit_when_attributable_is_tagged():
    # IBKR: NetIncomeLoss last tagged in 2013; ProfitLoss includes the NCI share
    old = [{"start": "2013-01-01", "end": "2013-03-31", "val": 6.6e6, "form": "10-Q"}]
    common = _flow([250e6, 270e6, 290e6, 280e6, 310e6], 1100e6)
    consolidated = _flow([1000e6, 1100e6, 1186e6, 1171e6, 1338e6], 4400e6)
    facts = _facts({"NetIncomeLoss": {"USD": old},
                    "NetIncomeLossAvailableToCommonStockholdersBasic": {"USD": common},
                    "ProfitLoss": {"USD": consolidated}})
    ttm = E._ttm(facts, E.TAGS["net_income"], asof="2026-10-29")
    assert ttm < 1.5e9


def _weighted(val: float, end: str = "2026-09-30", start: str = "2026-07-01") -> dict:
    return {"shares": [{"start": start, "end": end, "val": val, "form": "10-Q"},
                       {"start": "2026-01-01", "end": end, "val": val * 0.99, "form": "10-Q"}]}


def test_multi_class_filer_falls_back_to_weighted_average_shares():
    # HOOD: no undivided cover count in companyfacts
    facts = _facts({"WeightedAverageNumberOfSharesOutstandingBasic": _weighted(898e6)})
    shares, basis = E._shares_outstanding(facts)
    assert shares == 898e6
    assert basis.startswith("us-gaap:WeightedAverageNumberOfSharesOutstandingBasic")


def test_cover_count_wins_when_current_and_loses_when_stale():
    cover = {"shares": [{"end": "2026-10-21", "val": 1729e6, "form": "10-Q"}]}
    facts = _facts({"WeightedAverageNumberOfSharesOutstandingBasic": _weighted(1739e6)},
                   dei={"EntityCommonStockSharesOutstanding": cover})
    assert E._shares_outstanding(facts) == (1729e6, "dei:EntityCommonStockSharesOutstanding")
    stale = {"shares": [{"end": "2015-04-30", "val": 42.2e6, "form": "10-Q"}]}  # IBKR
    facts = _facts({"WeightedAverageNumberOfSharesOutstandingBasic": _weighted(447.9e6)},
                   dei={"EntityCommonStockSharesOutstanding": stale})
    assert E._shares_outstanding(facts)[0] == 447.9e6


def test_no_share_data_is_none():
    assert E._shares_outstanding(_facts({})) == (None, None)


# ── TTM windows (formal-verification track "data", F-D1-1..4, 8, 10, 11) ──

def _cash_flow(year: int, q: list[float], k_filed: str | None, q_filed: tuple = ()) -> list[dict]:
    """A cash-flow line as 10-Qs and a 10-K tag it: year-to-date only (3/6/9 months)
    plus the fiscal year. q_filed overrides the 10-Q filing dates."""
    ends = [f"{year}-03-31", f"{year}-06-30", f"{year}-09-30"]
    filed = list(q_filed) or [f"{year}-05-10", f"{year}-08-05", f"{year}-11-05"]
    rows = [{"start": f"{year}-01-01", "end": e, "val": sum(q[:i + 1]), "form": "10-Q",
             "filed": f} for i, (e, f) in enumerate(zip(ends, filed)) if i < len(q)]
    if k_filed:
        rows.append({"start": f"{year}-01-01", "end": f"{year}-12-31", "val": sum(q),
                     "form": "10-K", "filed": k_filed})
    return rows


def _ocf(entries: list[dict]) -> dict:
    return _facts({"NetCashProvidedByUsedInOperatingActivities": {"USD": entries}})


def test_cash_flow_ttm_right_after_10k_is_the_fiscal_year():
    # F-D1-1, SCHW 2026-03-15: the TTM to Q3 read -$753M against a filed FY of +$9,311M
    rows = _cash_flow(2024, [100, 200, 300, 400], "2025-02-25") + \
        _cash_flow(2025, [-400, -300, -200, 2100], "2026-02-25")
    facts = _ocf(rows)
    assert E._ttm(facts, E.TAGS["operating_cash_flow"], asof="2026-03-15") == 1200
    # the next 10-Q reopens the year-to-date reconstruction: 3M'26 + FY'25 - 3M'25
    facts = _ocf(rows + _cash_flow(2026, [50], None))
    assert E._ttm(facts, E.TAGS["operating_cash_flow"], asof="2026-05-15") == 50 + 1200 + 400


def _income(year: int, q: list[float], *, missing: tuple = (), tag_q4: bool = False,
            k_filed: str | None = None) -> list[dict]:
    """An income line: 3-month columns, YTD columns in Q2/Q3, the fiscal year, and
    optionally a discrete Q4 tagged in the 10-K."""
    bounds = [(f"{year}-01-01", f"{year}-03-31"), (f"{year}-04-01", f"{year}-06-30"),
              (f"{year}-07-01", f"{year}-09-30"), (f"{year}-10-01", f"{year}-12-31")]
    filed = [f"{year}-05-10", f"{year}-08-05", f"{year}-11-05"]
    rows = []
    for i in range(3):
        if i in missing:
            continue
        s, e = bounds[i]
        rows.append({"start": s, "end": e, "val": q[i], "form": "10-Q", "filed": filed[i]})
        if i:
            rows.append({"start": f"{year}-01-01", "end": e, "val": sum(q[:i + 1]),
                         "form": "10-Q", "filed": filed[i]})
    if k_filed:
        rows.append({"start": f"{year}-01-01", "end": f"{year}-12-31", "val": sum(q),
                     "form": "10-K", "filed": k_filed})
        if tag_q4:
            rows.append({"start": bounds[3][0], "end": bounds[3][1], "val": q[3],
                         "form": "10-K", "filed": k_filed})
    return rows


def test_missing_q3_10q_does_not_leave_the_window_at_q2():
    # F-D1-1, discrete path: four contiguous quarters still exist one quarter back
    rows = _income(2024, [1, 2, 3, 4], k_filed="2025-02-25") + \
        _income(2025, [10, 20, 30, 40], missing=(2,), k_filed="2026-02-25")
    facts = _facts({"Revenues": {"USD": rows}})
    assert E._ttm(facts, E.TAGS["revenue"], asof="2026-03-15") == 100     # FY2025, not 3+4+10+20


def test_a_discrete_quarter_is_never_year_to_date():
    # F-D1-2: Q2 10-Q missing, the 10-K tags a discrete Q4; the cumulative path once
    # took Q4'25 as the latest YTD: Q4'25 + FY'24 - Q1'24 = 170 instead of FY'25 = 260
    rows = _income(2024, [10, 20, 30, 40], tag_q4=True, k_filed="2025-02-25") + \
        _income(2025, [50, 60, 70, 80], missing=(1,), tag_q4=True, k_filed="2026-02-25")
    facts = _facts({"Revenues": {"USD": rows}})
    assert E._ttm(facts, E.TAGS["revenue"], asof="2026-03-01") == 260


def test_cumulative_path_does_not_depend_on_entry_order():
    # F-D1-2: Q1'26 10-Q missing; the Q2'26 10-Q has the 3-month Q2 and the 6M YTD,
    # same end. Listing the 3-month column first used to make it the "YTD".
    rows = _income(2025, [100, 110, 120, 130], k_filed="2026-02-25") + \
        _income(2026, [500, 210, 0, 0], missing=(0, 2))
    want = 120 + 130 + 500 + 210
    facts = _facts({"Revenues": {"USD": rows}})
    assert E._ttm(facts, E.TAGS["revenue"], asof="2026-08-20") == want
    q2 = [r for r in rows if r["start"] == "2026-04-01"]
    facts = _facts({"Revenues": {"USD": q2 + [r for r in rows if r not in q2]}})
    assert E._ttm(facts, E.TAGS["revenue"], asof="2026-08-20") == want


def test_a_missing_10k_is_not_bridged_by_an_older_year():
    # F-D1-3: without the FY2025 10-K, 6M'26 + FY'24 - 6M'24 skips all of 2025
    rows = _cash_flow(2024, [100, 110, 120, 130], "2025-02-25") + \
        _cash_flow(2025, [200, 210, 220, 230], None) + _cash_flow(2026, [300, 310], None)
    assert E._ttm(_ocf(rows), E.TAGS["operating_cash_flow"], asof="2026-08-20") is None


def test_cumulative_path_takes_the_latest_filed_value():
    # F-D1-4: a 10-K/A restates FY2025; the 2026 10-Q recasts the 6M'25 comparative
    # (SOFI's 6M'22 capex went from $50.03M to $47.02M this way)
    rows = _cash_flow(2025, [-400, -300, -200, 2100], "2026-02-25") + \
        _cash_flow(2026, [50, 60], None) + [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 1100, "form": "10-K/A",
             "filed": "2026-04-01"},
            {"start": "2025-01-01", "end": "2025-06-30", "val": -650, "form": "10-Q",
             "filed": "2026-08-05"}]
    assert E._ttm(_ocf(rows), E.TAGS["operating_cash_flow"], asof="2026-08-20") == 110 + 1100 + 650


def test_ttm_uses_only_facts_filed_by_asof():
    # F-D1-10: HOOD's Q3 10-Q was filed 2026-10-28; a run dated 10-27 must not see it
    rows = _income(2025, [100, 110, 120, 130], k_filed="2026-02-25") + \
        _income(2026, [140, 150, 160, 0])
    facts = _facts({"Revenues": {"USD": rows}})
    known = [r for r in rows if r["filed"] <= "2026-10-27"]
    for r in rows:
        if r["end"] == "2026-09-30":
            r["filed"] = "2026-10-28"
    assert E._ttm(facts, E.TAGS["revenue"], asof="2026-10-27") == \
        E._ttm(_facts({"Revenues": {"USD": known}}), E.TAGS["revenue"], asof="2026-10-27") == \
        120 + 130 + 140 + 150


def test_growth_leg_reads_last_year_as_known_on_the_run_date():
    # the prior-year TTM keeps the run date's filing cutoff (known_by), so a later
    # 10-Q/A restating Q2'25 counts; asof alone would also cut the filing date
    rows = _income(2024, [100, 110, 120, 130], k_filed="2025-02-25") + \
        _income(2025, [140, 150, 160, 170], k_filed="2026-02-25") + \
        [{"start": "2025-04-01", "end": "2025-06-30", "val": 155, "form": "10-Q/A",
          "filed": "2026-03-10"}]
    facts = _facts({"Revenues": {"USD": rows}})
    assert E._ttm(facts, E.TAGS["revenue"], asof="2025-08-20") == 120 + 130 + 140 + 150
    assert E._ttm(facts, E.TAGS["revenue"], asof="2025-08-20", known_by="2026-08-20") == \
        120 + 130 + 140 + 155


def test_build_sees_only_what_was_filed_by_the_run_date(monkeypatch):
    # F-D1-10 for every field: shares and balances filed after the run date too
    revenue = _income(2025, [100, 110, 120, 130], k_filed="2026-02-25") + \
        _income(2026, [140, 150, 160, 0])
    for r in revenue:
        if r["end"] == "2026-09-30":
            r["filed"] = "2026-10-28"
    cover = {"shares": [{"end": "2026-07-25", "val": 900e6, "form": "10-Q", "filed": "2026-08-05"},
                        {"end": "2026-10-21", "val": 950e6, "form": "10-Q", "filed": "2026-10-28"}]}
    cash = [{"end": "2026-06-30", "val": 5e9, "form": "10-Q", "filed": "2026-08-05"},
            {"end": "2026-09-30", "val": 7e9, "form": "10-Q", "filed": "2026-10-28"}]
    facts = _facts({"Revenues": {"USD": revenue},
                    "CashAndCashEquivalentsAtCarryingValue": {"USD": cash}},
                   dei={"EntityCommonStockSharesOutstanding": cover})
    monkeypatch.setattr(E, "resolve_cik", lambda t: "0000000001")
    monkeypatch.setattr(E, "_get", lambda url, *a, **k: facts)
    monkeypatch.setattr(E, "_price_series", lambda *a, **k: [{"Date": "2026-10-27", "Close": 10.0}])
    monkeypatch.setattr(E.time, "sleep", lambda s: None)
    m = E.build("TEST", end="2026-10-27", with_beta=False)["metrics"]
    assert m["total_revenue"] == 120 + 130 + 140 + 150
    assert m["shares_outstanding"] == 900e6
    assert m["total_cash"] == 5e9


def test_net_income_is_what_common_holders_earn():
    # F-D1-8, SCHW: NetIncomeLoss is before preferred dividends ($10,096M vs $9,722M)
    parent = _flow([1909e6, 2126e6, 2358e6, 2479e6, 2800e6], 8852e6)
    common = _flow([1800e6, 2000e6, 2250e6, 2380e6, 2700e6], 8417e6)
    facts = _facts({"NetIncomeLoss": {"USD": parent},
                    "NetIncomeLossAvailableToCommonStockholdersBasic": {"USD": common}})
    q4 = 8417e6 - (1800e6 + 2000e6 + 2250e6)
    assert E._ttm(facts, E.TAGS["net_income"], asof="2026-10-29") == 2250e6 + q4 + 2380e6 + 2700e6
    # a to-common tag the filer stopped using never outranks a current NetIncomeLoss
    stale = [r for r in common if r["end"] <= "2025-09-30"]
    facts = _facts({"NetIncomeLoss": {"USD": parent},
                    "NetIncomeLossAvailableToCommonStockholdersBasic": {"USD": stale}})
    q4 = 8852e6 - (1909e6 + 2126e6 + 2358e6)
    assert E._ttm(facts, E.TAGS["net_income"], asof="2026-10-29") == 2358e6 + q4 + 2479e6 + 2800e6


def test_a_retired_tag_is_not_a_ttm_fallback(monkeypatch):
    # F-D1-11: the current tag has two quarters (just migrated), the old one's last
    # complete window ended over a year earlier; that window is no TTM
    old = _income(2023, [100, 110, 120, 130], k_filed="2024-02-25")
    new = [{"start": "2025-01-01", "end": "2025-03-31", "val": 999, "form": "10-Q", "filed": "2025-05-10"},
           {"start": "2025-04-01", "end": "2025-06-30", "val": 999, "form": "10-Q", "filed": "2025-08-05"}]
    facts = _facts({"Revenues": {"USD": old}, "RevenuesNetOfInterestExpense": {"USD": new}})
    assert E._ttm(facts, E.TAGS["revenue"], asof="2025-08-20") is None
    # build() then falls back to the fiscal year and says so
    monkeypatch.setattr(E, "resolve_cik", lambda t: "0000000001")
    monkeypatch.setattr(E, "_get", lambda url, *a, **k: facts)
    monkeypatch.setattr(E, "_price_series", lambda *a, **k: [])
    monkeypatch.setattr(E.time, "sleep", lambda s: None)
    doc = E.build("TEST", end="2025-08-20", with_beta=False)
    assert doc["metrics"]["total_revenue"] == 460
    assert "revenue" in doc["metadata"]["fiscal_year_fallback"]


# ── growth base: the window a year before the current one (F-D1-12) ──

def _build(monkeypatch, facts: dict, end: str) -> dict:
    monkeypatch.setattr(E, "resolve_cik", lambda t: "0000000001")
    monkeypatch.setattr(E, "_get", lambda url, *a, **k: facts)
    monkeypatch.setattr(E, "_price_series", lambda *a, **k: [])
    monkeypatch.setattr(E.time, "sleep", lambda s: None)
    return E.build("TEST", end=end, with_beta=False)


def test_growth_compares_windows_a_year_apart(monkeypatch):
    # SCHW 2026-10-30: the Q3'26 10-Q is not out, so the TTM ends Q2'26, while the
    # Q3'25 10-Q is on file; "run date - 1 year" took the TTM to Q3'25 as the base
    rows = _income(2024, [100, 110, 120, 130], k_filed="2025-02-25") + \
        _income(2025, [140, 150, 160, 170], k_filed="2026-02-25") + \
        _income(2026, [180, 190, 200, 0])                       # Q3'26 10-Q filed 2026-11-05
    m = _build(monkeypatch, _facts({"Revenues": {"USD": rows}}), "2026-10-30")["metrics"]
    assert m["total_revenue"] == 160 + 170 + 180 + 190            # TTM to Q2'26
    assert m["revenue_growth"] == round(700 / (120 + 130 + 140 + 150) - 1, 6)   # vs TTM to Q2'25


def _weeks52(fy_start: str, q: list[float], k_filed: str) -> list[dict]:
    """A 52-week fiscal year of four 13-week quarters, 10-Qs 40 days after each."""
    s0 = datetime.fromisoformat(fy_start)
    rows = []
    for i in range(3):
        s, e = s0 + timedelta(days=91 * i), s0 + timedelta(days=91 * i + 90)
        rows.append({"start": s.date().isoformat(), "end": e.date().isoformat(), "val": q[i],
                     "form": "10-Q", "filed": (e + timedelta(days=40)).date().isoformat()})
    rows.append({"start": fy_start, "end": (s0 + timedelta(days=363)).date().isoformat(),
                 "val": sum(q), "form": "10-K", "filed": k_filed})
    return rows


def test_growth_base_allows_a_52_week_shift(monkeypatch):
    # Q3 FY2026 ends 2026-06-27, Q3 FY2025 on 2025-06-28: a day past the anniversary
    rows = _weeks52("2023-10-01", [10, 20, 30, 40], "2024-11-22") + \
        _weeks52("2024-09-29", [50, 60, 70, 80], "2025-11-21") + \
        _weeks52("2025-09-28", [90, 100, 110, 120], "2026-11-20")   # 10-K after the run
    m = _build(monkeypatch, _facts({"Revenues": {"USD": rows}}), "2026-11-10")["metrics"]
    assert m["total_revenue"] == 80 + 90 + 100 + 110
    assert m["revenue_growth"] == round(380 / (40 + 50 + 60 + 70) - 1, 6)    # not vs FY2025

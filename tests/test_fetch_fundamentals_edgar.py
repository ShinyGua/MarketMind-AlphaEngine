"""EDGAR extraction on synthetic companyfacts, shaped after the filings that broke
the HOOD run (2026-10-29): SCHW's proxy-statement net income, SOFI's bank revenue
tag, HOOD's per-class cover counts, IBKR's non-controlling interest."""
import sys
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

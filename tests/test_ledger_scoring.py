"""Outcome scoring on synthetic bars: reference timing, horizons, suspensions,
limit locks, stops, hedges, trims, costs — and the runner/report/backfill."""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ledger import backfill, report, score, store  # noqa: E402

FEES = score.load_fees()


def bars(dates, closes, opens=None, highs=None, lows=None):
    out = []
    for i, (d, c) in enumerate(zip(dates, closes)):
        o = opens[i] if opens else c
        out.append({"Date": d, "Open": o, "High": highs[i] if highs else max(o, c) * 1.01,
                    "Low": lows[i] if lows else min(o, c) * 0.99, "Close": c, "Volume": 1e6})
    return out


DATES = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-06-01", periods=150)]
N = len(DATES)


def body(market="CN", issued="2026-10-22T03:21:00+00:00", p=0.4, flat=("HOLD", "none"),
         long=("SELL", "none"), symbol="300308.SZ"):
    return {"decision_id": "X", "ticker": "300308", "symbol": symbol, "market": market,
            "issued_at": issued, "forecast": {"p_outperform": p, "excess_quantiles": {"p10": -0.1, "p50": 0, "p90": 0.3}},
            "actions": {"if_flat": {"label": flat[0], "overlay": flat[1]},
                        "if_long": {"label": long[0], "overlay": long[1]}}}


FLAT_BENCH = bars(DATES, [100.0] * N)


def test_mid_session_issue_references_that_days_close():
    # 03:21Z = 11:21 Beijing on 10-22: the 10-22 close is still ahead -> reference 10-22.
    i = score.reference_index(DATES, score._issued(body()), "CN")
    assert DATES[i] == "2026-10-22"


def test_after_close_issue_references_next_session():
    i = score.reference_index(DATES, score._issued(body(issued="2026-10-22T08:00:00+00:00")), "CN")
    assert DATES[i] == "2026-10-23"


def test_forecast_excess_brier_and_horizon():
    stock = bars(DATES, [100.0 + i for i in range(N)])
    f = score.score_forecast(body(p=0.4), stock, FLAT_BENCH, 20)
    i0 = DATES.index("2026-10-22")
    assert f["status"] == "scored" and f["endpoint_date"] == DATES[i0 + 20]
    assert f["excess_return"] == pytest.approx((100 + i0 + 20) / (100 + i0) - 1, abs=1e-6)
    assert f["outperformed"] is True
    assert f["brier"] == pytest.approx((0.4 - 1) ** 2)
    assert f["inside_p10_p90"] is True


def test_immature_and_suspended_are_never_filled():
    stock = bars(DATES, [100.0] * N)
    assert score.score_forecast(body(), stock, FLAT_BENCH, 60)["status"] == "immature"
    i0 = DATES.index("2026-10-22")
    gap = [r for r in stock if r["Date"] != DATES[i0 + 5]]       # suspended on the endpoint
    f = score.score_forecast(body(), gap, FLAT_BENCH, 5)
    assert f["status"] == "unscorable" and "suspended" in f["reason"]


def test_flat_hold_stays_in_cash():
    stock = bars(DATES, [100.0 + i for i in range(N)])
    t = score.simulate_track(body(flat=("HOLD", "none")), "if_flat", stock, FLAT_BENCH, 5, FEES)
    assert t["net_return"] == 0.0 and t["exposure_after"] == 0.0 and t["vs_do_nothing"] == 0.0


def test_trim_from_flat_never_opens_a_position():
    stock = bars(DATES, [100.0] * N)
    t = score.simulate_track(body(flat=("HOLD", "trim")), "if_flat", stock, FLAT_BENCH, 5, FEES)
    assert t["exposure_after"] == 0.0 and t["costs"] == 0.0


def test_buy_fills_next_open_after_issue_and_pays_costs():
    closes = [100.0] * N
    stock = bars(DATES, closes)
    t = score.simulate_track(body(flat=("BUY", "none")), "if_flat", stock, FLAT_BENCH, 5, FEES)
    assert t["filled_on"] == "2026-10-23"       # 11:21 issue: today's open already passed
    buy_cost = FEES["CN"]["assumptions"]["commission"] + FEES["CN"]["assumptions"]["slippage"] \
        + sum(i["rate"] for i in FEES["CN"]["statutory"] if i["side"] in ("buy", "both"))
    assert t["costs"] == pytest.approx(buy_cost, abs=1e-6)
    assert t["net_return"] == pytest.approx(-buy_cost, abs=1e-6)
    assert "transfer_fee" in t["unverified_fee_items"]


def test_cn_limit_locked_open_defers_the_buy():
    closes = [100.0] * N
    opens = list(closes)
    k = DATES.index("2026-10-23")
    opens[k] = 120.0                            # ChiNext +20% limit-up open
    closes[k] = 120.0
    stock = bars(DATES, closes, opens)
    t = score.simulate_track(body(flat=("BUY", "none")), "if_flat", stock, FLAT_BENCH, 5, FEES)
    assert t["filled_on"] == "2026-10-26"
    assert any(f.startswith("no_fill_limit_locked") for f in t["flags"])


def test_long_sell_exits_and_beats_a_falling_stock():
    closes = [100.0] * 120 + [100.0 - 2 * i for i in range(30)]
    stock = bars(DATES, closes)
    t = score.simulate_track(body(long=("SELL", "none"), issued=DATES[119] + "T08:00:00+00:00"),
                             "if_long", stock, FLAT_BENCH, 5, FEES)
    assert t["exposure_after"] == 0.0
    assert t["vs_do_nothing"] > 0                # selling beat holding a falling stock


def test_stop_triggers_on_close_and_exits_next_open():
    closes = [100.0] * 110 + [100.0 - 2 * i for i in range(1, N - 109)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    stock = bars(DATES, closes, highs=highs, lows=lows)
    issued = DATES[109] + "T08:00:00+00:00"
    t = score.simulate_track(body(long=("HOLD", "stop"), issued=issued), "if_long", stock, FLAT_BENCH, 20, FEES)
    assert t["stop_level"] is not None and t["stopped_out_on"] is not None
    assert t["vs_do_nothing"] > 0                # the stop cut the slide


def test_cn_hedge_maps_to_partial_trim():
    import math
    stock = bars(DATES, [100.0 * (1 + 0.01 * math.sin(i)) for i in range(N)])
    bench = bars(DATES, [100.0 * (1 + 0.01 * math.sin(i)) for i in range(N)])  # beta 1
    t = score.simulate_track(body(long=("HOLD", "hedge")), "if_long", stock, bench, 5, FEES)
    assert t["exposure_after"] == pytest.approx(0.5, abs=0.02)
    assert "hedge_mapped_to_trim" in t["flags"]


def test_us_hedge_shorts_spy_by_beta():
    import math
    stock = bars(DATES, [100.0 * (1 + 0.02 * math.sin(i)) for i in range(N)])
    spy = bars(DATES, [400.0 * (1 + 0.01 * math.sin(i)) for i in range(N)])    # beta ~2 -> capped 1
    t = score.simulate_track(body(market="US", long=("HOLD", "hedge"), symbol="MU",
                                  issued="2026-10-22T21:00:00+00:00"),
                             "if_long", stock, FLAT_BENCH, 5, FEES, hedge=spy)
    assert t["hedge_ratio"] == 1.0 and t["exposure_after"] == 1.0


def _sealed(root, decision_id, ticker, issued, p, market="CN", symbol="300308.SZ"):
    store.append(root, "decision", {**body(market=market, issued=issued, p=p, symbol=symbol),
                                    "decision_id": decision_id, "ticker": ticker})


def test_runner_appends_once_per_matured_horizon_and_report_reads_it(tmp_path):
    root = tmp_path / "MMD"
    store.append(root, "protocol", {"protocol": {"review_points": [
        {"matured_primary_forecasts": 2, "min_tickers": 2, "allowed_claims": "descriptive only"}],
        "default_verdict": "inconclusive"}})
    _sealed(root, "A", "300308", "2026-09-10T08:00:00+00:00", 0.7)
    _sealed(root, "B", "600519", "2026-09-11T08:00:00+00:00", 0.3, symbol="600519.SS")
    up = bars(DATES, [100.0 + i for i in range(N)])

    def fetch(symbol, since):
        return (FLAT_BENCH if symbol == "sh000300" else up), {"symbol": symbol}

    added = score.score_all(root, fetch=fetch)
    assert {(r["body"]["decision_id"], r["body"]["horizon_sessions"]) for r in added} == \
        {("A", 5), ("A", 20), ("A", 60), ("B", 5), ("B", 20), ("B", 60)}
    assert score.score_all(root, fetch=fetch) == []                 # idempotent
    assert store.verify(root)["ok"]
    assert list((root / "prices" / "stock").rglob("*.csv"))         # bars archived
    rep = report.build(root)
    h20 = rep["horizons"][20]
    assert h20["scored"] == 2 and h20["excess_return"]["tickers"] == 2
    assert h20["brier"] == pytest.approx(((0.7 - 1) ** 2 + (0.3 - 1) ** 2) / 2)
    assert rep["review"]["verdict"] == "descriptive only"
    assert "MarketMind outcome report" in report.render(rep)


def test_report_without_review_point_is_inconclusive(tmp_path):
    root = tmp_path / "MMD"
    store.append(root, "protocol", {"protocol": {"review_points": [
        {"matured_primary_forecasts": 30, "min_tickers": 10, "allowed_claims": "descriptive only"}],
        "default_verdict": "inconclusive"}})
    assert report.build(root)["review"]["verdict"] == "inconclusive"


def test_backfill_uses_run_date_close_and_marks_unverified(tmp_path):
    ws = tmp_path / "300308"
    (ws / "profile").mkdir(parents=True)
    (ws / "profile" / "company_profile.json").write_text('{"market_profile": "CN"}')
    stock = bars(DATES, [100.0 + i for i in range(N)])
    rows = backfill.rows_for({"decision": "HOLD", "risk_overlay": "hedge"}, ws, "2026-10-22",
                             stock, FLAT_BENCH)
    r20 = next(r for r in rows if r["horizon_sessions"] == 20)
    assert r20["reference_date"] == "2026-10-22" and r20["provenance"] == "unverified"
    assert r20["status"] == "scored" and r20["excess_return"] > 0

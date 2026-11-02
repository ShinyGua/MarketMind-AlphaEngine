"""Tests for mcp/shared/freshness.py — cadence-aware freshness of CN chip blocks."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from shared import freshness as f  # noqa: E402

SH = ZoneInfo("Asia/Shanghai")


def at(y, m, d, hh=20, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=SH)


def test_golden_week_is_not_lag():
    # 09-30 is the last session before the National Day closure; on 10-08
    # (first session back, after the close) it is one completed session old.
    assert f.session_lag("2026-09-30", "2026-10-08") == 1
    v = f.assess("main_force", "2026-09-30", "2026-10-08", at(2026, 10, 8))
    assert v["fresh"] is True and v["lag_sessions"] == 1


def test_spring_festival_is_not_lag():
    assert f.session_lag("2026-02-13", "2026-02-24") == 1


def test_daily_block_two_sessions_is_the_limit():
    assert f.assess("turnover", "2026-10-26", "2026-10-28", at(2026, 10, 28))["fresh"] is True
    v = f.assess("turnover", "2026-10-23", "2026-10-28", at(2026, 10, 28))
    assert v["fresh"] is False and "3 sessions" in v["reason"]


def test_lunch_break_and_open_session_use_previous_close():
    # 12:30 Beijing on 10-29: the 10-29 session is not complete; reference is 10-28.
    assert f.last_completed_session("2026-10-29", at(2026, 10, 29, 12, 30)) == "2026-10-28"
    assert f.last_completed_session("2026-10-29", at(2026, 10, 29, 15, 29)) == "2026-10-28"
    assert f.last_completed_session("2026-10-29", at(2026, 10, 29, 15, 30)) == "2026-10-29"


def test_mid_session_run_does_not_penalise_yesterday():
    # Data as of 10-27 seen at 10:00 on 10-29: one completed session (10-28) later.
    v = f.assess("main_force", "2026-10-27", "2026-10-29", at(2026, 10, 29, 10, 0))
    assert v["lag_sessions"] == 1 and v["fresh"] is True


def test_margin_allows_t_minus_one_publication():
    v = f.assess("margin", "2026-10-23", "2026-10-28", at(2026, 10, 28))
    assert v["lag_sessions"] == 3 and v["fresh"] is True
    assert f.assess("margin", "20261022", "2026-10-28", at(2026, 10, 28))["fresh"] is False


def test_northbound_2024_series_is_stale():
    v = f.assess("northbound", "2024-08-16", "2026-08-25", at(2026, 8, 25))
    assert v["fresh"] is False and v["lag_sessions"] > 400


def test_replay_of_past_run_measures_against_that_run():
    # Judged as of the run date, not today: an audit months later must not
    # mark the run's own fresh data stale.
    v = f.assess("main_force", "2026-08-24", "2026-08-25", at(2026, 10, 29))
    assert v["fresh"] is True and v["lag_sessions"] == 1


def test_holder_count_deadlines():
    assert f.required_quarter_end("2026-04-29") == "2025-09-30"
    assert f.required_quarter_end("2026-04-30") == "2026-03-31"   # annual + Q1 both due
    assert f.required_quarter_end("2026-08-30") == "2026-03-31"
    assert f.required_quarter_end("2026-08-31") == "2026-06-30"
    assert f.required_quarter_end("2026-10-31") == "2026-09-30"
    assert f.required_quarter_end("2027-01-15") == "2026-09-30"
    # 000333's August 2026 runs used a 2026-02-28 count although 03-31 was due.
    assert f.assess("holder_count", "2026-02-28", "2026-08-06")["fresh"] is False
    assert f.assess("holder_count", "2026-06-30", "2026-10-22")["fresh"] is True


def test_lhb_event_window():
    assert f.assess("lhb", "2026-10-09", "2026-10-28", at(2026, 10, 28))["fresh"] is True
    assert f.assess("lhb", "2026-08-03", "2026-10-28", at(2026, 10, 28))["fresh"] is False
    assert f.assess("lhb", None, "2026-10-28")["fresh"] is True  # no appearance: nothing stale


def test_missing_or_future_dates_are_not_fresh():
    assert f.assess("turnover", None, "2026-10-28")["reason"] == "no observation date"
    assert f.assess("turnover", "2026-10-30", "2026-10-28")["fresh"] is False


def test_unlock_schedule_is_windowed_by_the_card_builder():
    assert f.assess("restricted_release", None, "2026-10-28")["fresh"] is True


def test_normalize_date_formats():
    assert f.normalize_date("20261021") == "2026-10-21"
    assert f.normalize_date("2026-10-21 00:00:00") == "2026-10-21"
    assert f.normalize_date("not a date") is None

"""Cadence-aware freshness for CN chip blocks.

A chip observation may support a current claim only while it is fresh for its
own cadence. Re-fetching does not refresh an observation: what counts is the
date the data describes (``as_of``), measured against the last *completed*
Shanghai session — a session still trading (the 11:30-13:00 lunch break
included) is not yet an observation.

| cadence            | blocks                       | fresh when                                   |
|--------------------|------------------------------|----------------------------------------------|
| daily (≤2 sessions)| turnover, main_force, northbound | as_of within 2 completed sessions         |
| daily (≤3 sessions)| margin                       | published T-1, so one extra session          |
| quarterly_report   | holder_count                 | as_of ≥ latest quarter-end whose statutory   |
|                    |                              | disclosure deadline has passed               |
| event (≤20)        | lhb                          | latest appearance within 20 sessions         |
| scheduled          | restricted_release           | a forward schedule; each event is windowed   |
|                    |                              | by the card builder (run date .. +60d)       |

Northbound per-stock daily holdings stopped being published in 2024-08, so
that block comes out stale under the generic daily rule — no special case,
and it recovers on its own if the source ever resumes.

Sessions are counted on the card's own market (``market`` = the workspace's
``market_profile``): a US name's price-structure card is judged on the NYSE
calendar and clock, not Shanghai's. Unknown markets keep the Shanghai rule.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")
CN_SESSION_DONE = time(15, 30)  # a run-date bar counts only after this

# market_profile -> (exchange calendar, local zone, local time after which that
# day's session is an observation) — the same cutoffs as scripts/price_router.py
MARKETS = {
    "CN": ("XSHG", CN_TZ, CN_SESSION_DONE),
    "HK": ("XHKG", ZoneInfo("Asia/Hong_Kong"), time(16, 30)),
    "US": ("XNYS", ZoneInfo("America/New_York"), time(16, 30)),
}


def _market(market: str | None) -> tuple:
    return MARKETS.get(str(market or "CN").upper(), MARKETS["CN"])

CADENCE = {
    "turnover": {"kind": "daily", "max_lag_sessions": 2},
    "main_force": {"kind": "daily", "max_lag_sessions": 2},
    "northbound": {"kind": "daily", "max_lag_sessions": 2},
    "margin": {"kind": "daily", "max_lag_sessions": 3},
    "holder_count": {"kind": "quarterly_report"},
    "lhb": {"kind": "event", "max_lag_sessions": 20},
    "restricted_release": {"kind": "scheduled"},
    # A chip card whose fields name no known block: cadence unknown (desks tag
    # short interest or Form 4 data "chips"), so it gets the event window —
    # still far too short for a months-old observation.
    "unknown": {"kind": "event", "max_lag_sessions": 20},
}

# Quarter-end -> statutory disclosure deadline (month, day) and year offset.
# Annual reports (12-31) and Q1 reports (03-31) are both due 04-30.
_DEADLINES = (
    ((12, 31), (4, 30), 1),
    ((3, 31), (4, 30), 0),
    ((6, 30), (8, 31), 0),
    ((9, 30), (10, 31), 0),
)


@lru_cache(maxsize=4)
def _calendar(code: str = "XSHG"):
    try:
        import exchange_calendars as xcals

        return xcals.get_calendar(code)
    except Exception:  # noqa: BLE001 - degrade to weekdays
        return None


def calendar_end(market: str = "CN") -> str | None:
    """Last session the installed calendar knows; holidays after it are unknown."""
    cal = _calendar(_market(market)[0])
    return cal.last_session.date().isoformat() if cal is not None else None


def sessions_between(start: str, end: str, market: str = "CN") -> list[str]:
    """The market's sessions in [start, end] as ISO dates (weekdays if no calendar)."""
    if end < start:
        return []
    lo, hi = _date.fromisoformat(start), _date.fromisoformat(end)
    out: list[str] = []
    cal = _calendar(_market(market)[0])
    if cal is not None:
        first, last = cal.first_session.date(), cal.last_session.date()
        a, b = max(lo, first), min(hi, last)
        if a <= b:
            out = [s.strftime("%Y-%m-%d") for s in cal.sessions_in_range(a.isoformat(), b.isoformat())]
        lo = max(lo, last + timedelta(days=1))  # weekdays only beyond the calendar
    d = lo
    while d <= hi:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def last_completed_session(run_date: str, now: datetime | None = None,
                           market: str = "CN") -> str:
    """The newest session that had finished trading, no later than run_date."""
    _code, tz, done = _market(market)
    local = (now or datetime.now(tz)).astimezone(tz)
    cap = min(_date.fromisoformat(run_date), local.date())
    if cap == local.date() and local.time() < done:
        cap -= timedelta(days=1)
    recent = sessions_between((cap - timedelta(days=30)).isoformat(), cap.isoformat(), market)
    return recent[-1] if recent else cap.isoformat()


def session_lag(as_of: str, reference: str, market: str = "CN") -> int:
    """Completed sessions after ``as_of`` up to and including ``reference``."""
    if as_of >= reference:
        return 0
    following = (_date.fromisoformat(as_of) + timedelta(days=1)).isoformat()
    return len(sessions_between(following, reference, market))


def required_quarter_end(run_date: str) -> str:
    """Latest quarter-end whose CN statutory disclosure deadline has passed."""
    d = _date.fromisoformat(run_date)
    best = None
    for year in (d.year - 2, d.year - 1, d.year):
        for (qm, qd), (dm, dd), offset in _DEADLINES:
            quarter_end = _date(year, qm, qd)
            deadline = _date(year + offset, dm, dd)
            if deadline <= d and (best is None or quarter_end > best):
                best = quarter_end
    return best.isoformat()


def normalize_date(value) -> str | None:
    """'20261021' / '2026-10-21' / '2026-10-21 00:00:00' -> '2026-10-21'."""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) >= 8 and s[:8].isdigit():
        s = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    s = s[:10]
    try:
        return _date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def assess(block: str, as_of, run_date: str, now: datetime | None = None,
           market: str = "CN") -> dict:
    """Freshness verdict for one block: {cadence, as_of, lag_sessions, fresh, reason}."""
    rule = CADENCE.get(block, {"kind": "daily", "max_lag_sessions": 2})
    kind = rule["kind"]
    as_of = normalize_date(as_of)
    out = {"cadence": kind, "as_of": as_of, "lag_sessions": None, "fresh": True, "reason": None}
    if kind == "scheduled":
        return out
    if as_of is None:
        if kind == "event":  # no appearance in the window: nothing to be stale
            return out
        return {**out, "fresh": False, "reason": "no observation date"}
    if as_of > run_date:
        return {**out, "fresh": False, "reason": f"as_of {as_of} is after the run date"}
    if kind == "quarterly_report":
        need = required_quarter_end(run_date)
        if as_of < need:
            return {**out, "fresh": False,
                    "reason": f"as_of {as_of} predates the {need} period already due for disclosure"}
        return out
    ref = last_completed_session(run_date, now, market)
    lag = session_lag(as_of, ref, market)
    out["lag_sessions"] = lag
    if lag > rule["max_lag_sessions"]:
        out["fresh"] = False
        out["reason"] = (f"as_of {as_of} is {lag} sessions before {ref} "
                         f"(limit {rule['max_lag_sessions']})")
        end = calendar_end(market)
        if end and ref > end:  # weekdays stand in for sessions: holidays count
            out["reason"] += f"; holidays after {end} unknown — upgrade exchange_calendars"
    return out

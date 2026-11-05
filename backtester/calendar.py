"""NYSE trading sessions, via exchange_calendars."""

from __future__ import annotations

from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

CALENDAR_START = pd.Timestamp("1962-01-02")


@lru_cache(maxsize=1)
def nyse():
    return xcals.get_calendar("XNYS", start=CALENDAR_START)


def sessions(start, end) -> pd.DatetimeIndex:
    """NYSE sessions between start and end inclusive, as tz-naive midnight timestamps."""
    cal = nyse()
    start = max(pd.Timestamp(start).normalize(), cal.first_session)
    end = min(pd.Timestamp(end).normalize(), cal.last_session)
    if start > end:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(cal.sessions_in_range(start, end), freq=None)


def previous_session(date) -> pd.Timestamp:
    """The NYSE session before ``date`` (which need not itself be a session).

    On or before the calendar's first session (1962-01-02, which a stock's history can reach),
    there is none on record, so the weekday before is returned.
    """
    cal = nyse()
    date = pd.Timestamp(date).normalize()
    if date <= cal.first_session:
        return date - pd.offsets.BDay(1)
    if cal.is_session(date):
        return cal.previous_session(date)
    return cal.date_to_session(date, direction="previous")

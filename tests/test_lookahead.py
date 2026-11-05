"""Lookahead canary: on session t a strategy sees bars only through the close of t-1."""

import pandas as pd
import pytest
from helpers import make_strategy, run, ts

from backtester import LookaheadError
from backtester.testing import make_bars, nyse_sessions

START, END = "2024-01-02", "2024-01-31"


def numbered_bars():
    """Each session's OHLC equals its position in the month, so values identify the bar."""
    df = make_bars(START, END)
    for col in ("open", "high", "low", "close"):
        df[col] = range(1, len(df) + 1)
    return df.astype(float)


def test_reading_todays_bar_raises():
    def on_bar(self, ctx):
        if ctx.today == ts("2024-01-10"):
            ctx.bar("AAA", ctx.today)

    with pytest.raises(LookaheadError):
        run(make_strategy(on_bar), {"AAA": numbered_bars()}, START, END)


def test_reading_a_future_bar_raises():
    def on_bar(self, ctx):
        if ctx.today == ts("2024-01-10"):
            ctx.bar("AAA", ts("2024-01-17"))

    with pytest.raises(LookaheadError):
        run(make_strategy(on_bar), {"AAA": numbered_bars()}, START, END)


def test_reading_a_future_non_session_date_raises():
    def on_bar(self, ctx):
        if ctx.today == ts("2024-01-12"):  # Friday; the 13th is a Saturday
            ctx.bar("AAA", ts("2024-01-13"))

    with pytest.raises(LookaheadError):
        run(make_strategy(on_bar), {"AAA": numbered_bars()}, START, END)


def test_lookahead_in_on_start_raises():
    def on_start(self, ctx):
        ctx.bar("AAA", ctx.today)

    strat = make_strategy(lambda self, ctx: None, on_start=on_start)
    with pytest.raises(LookaheadError):
        run(strat, {"AAA": numbered_bars()}, START, END)


def test_previous_session_bar_is_visible():
    seen = {}

    def on_bar(self, ctx):
        if ctx.today == ts("2024-01-10"):
            seen["bar"] = ctx.bar("AAA", ts("2024-01-09"))

    run(make_strategy(on_bar), {"AAA": numbered_bars()}, START, END)
    sessions = list(nyse_sessions(START, END))
    assert seen["bar"].close == sessions.index(ts("2024-01-09")) + 1


def test_history_and_price_end_at_previous_close():
    sessions = list(nyse_sessions(START, END))
    checked = []

    def on_bar(self, ctx):
        i = sessions.index(ctx.today)
        hist = ctx.history("close", 5)
        if i == 0:
            assert hist.empty
            return
        assert hist.index[-1] == sessions[i - 1]
        assert hist.index.max() < ctx.today
        assert hist["AAA"].iloc[-1] == i  # close of session i-1 is i
        assert len(hist) == min(5, i)
        assert ctx.price("AAA") == i
        checked.append(ctx.today)

    run(make_strategy(on_bar), {"AAA": numbered_bars()}, START, END)
    assert len(checked) == len(sessions) - 1


def test_history_is_a_copy():
    def on_bar(self, ctx):
        hist = ctx.history("close", 3)
        if hist.empty:
            return
        expected = hist.copy()
        hist.iloc[:, :] = -1.0
        pd.testing.assert_frame_equal(ctx.history("close", 3), expected)

    run(make_strategy(on_bar), {"AAA": numbered_bars()}, START, END)

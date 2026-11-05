"""exit_after counts trading sessions, skipping weekends and holidays."""

import pytest
from helpers import make_strategy, run, ts

from backtester.testing import make_bars


def held_trade(entry_day, exit_after, at="open", start=None, end=None):
    start = start or entry_day[:8] + "01"
    frames = {"AAA": make_bars(start, end, price=100.0)}

    def on_bar(self, ctx):
        if ctx.today == ts(entry_day):
            ctx.order("AAA", shares=10, at=at, exit_after=exit_after)

    res = run(make_strategy(on_bar), frames, start, end)
    closed = res.trades[~res.trades["is_open"]]
    assert len(closed) == 1, res.trades
    return closed.iloc[0], res


@pytest.mark.parametrize(
    "entry, n, expected_exit",
    [
        ("2024-07-03", 3, "2024-07-09"),  # skips Jul 4 holiday and the weekend
        ("2024-11-26", 2, "2024-11-29"),  # skips Thanksgiving
        ("2024-03-28", 1, "2024-04-01"),  # skips Good Friday and the weekend
    ],
)
def test_exit_after_skips_non_sessions(entry, n, expected_exit):
    trade, res = held_trade(entry, n, end="2024-12-31")
    assert trade["entry_date"] == ts(entry)
    assert trade["exit_date"] == ts(expected_exit)
    assert trade["sessions_held"] == n
    assert trade["exit_reason"] == "exit_after"
    exit_fill = res.fills[res.fills["qty"] < 0].iloc[0]
    assert exit_fill["at"] == "open"


def test_exit_after_from_close_entry_exits_at_open_n_sessions_later():
    trade, _ = held_trade("2024-07-03", 3, at="close", end="2024-12-31")
    assert trade["exit_date"] == ts("2024-07-09")

"""Stops and targets: intraday fills, gap-through fills at the open, stop wins ties."""

import pytest
from helpers import make_strategy, run, ts

from backtester.testing import make_bars

START, END = "2024-03-01", "2024-03-15"
ENTRY, NEXT = "2024-03-04", "2024-03-05"
STOP, TARGET = 95.0, 110.0


def run_case(next_bar, entry_bar=None):
    overrides = {ENTRY: entry_bar or {"open": 100, "high": 101, "low": 99, "close": 100}}
    if next_bar:
        overrides[NEXT] = next_bar
    frames = {"AAA": make_bars(START, END, price=100.0, overrides=overrides)}

    def on_bar(self, ctx):
        if ctx.today == ts(ENTRY):
            ctx.order("AAA", shares=100, at="open", stop=STOP, target=TARGET)

    res = run(make_strategy(on_bar), frames, START, END)
    closed = res.trades[~res.trades["is_open"]]
    assert len(closed) == 1, res.trades
    return closed.iloc[0]


@pytest.mark.parametrize(
    "bar, exit_price, reason",
    [
        ({"open": 100, "high": 105, "low": 94, "close": 96}, STOP, "stop"),
        ({"open": 100, "high": 112, "low": 98, "close": 111}, TARGET, "target"),
        ({"open": 90, "high": 92, "low": 88, "close": 91}, 90.0, "stop"),  # gapped through stop
        ({"open": 115, "high": 118, "low": 113, "close": 116}, 115.0, "target"),  # gapped through target
        ({"open": 100, "high": 111, "low": 94, "close": 100}, STOP, "stop"),  # both touched
    ],
    ids=["stop", "target", "gap-stop", "gap-target", "both-touched"],
)
def test_exit_rules(bar, exit_price, reason):
    trade = run_case(bar)
    assert trade["exit_date"] == ts(NEXT)
    assert trade["exit_price"] == exit_price
    assert trade["exit_reason"] == reason


def test_stop_checked_on_entry_day():
    trade = run_case(None, entry_bar={"open": 100, "high": 101, "low": 94, "close": 96})
    assert trade["exit_date"] == ts(ENTRY)
    assert trade["exit_price"] == STOP
    assert trade["exit_reason"] == "stop"

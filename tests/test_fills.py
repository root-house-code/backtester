"""Fill timing: orders placed on session t fill at t's open or t's close."""

import pytest
from helpers import make_strategy, run, ts

from backtester.testing import make_bars

START, END = "2024-03-01", "2024-03-15"
DAY = "2024-03-05"


def frames():
    return {
        "AAA": make_bars(
            START, END, price=100.0,
            overrides={DAY: {"open": 101, "high": 106, "low": 99, "close": 105}},
        )
    }


def order_on(day, **kwargs):
    def on_bar(self, ctx):
        if ctx.today == ts(day):
            ctx.order("AAA", **kwargs)

    return make_strategy(on_bar)


def only_fill(result):
    assert len(result.fills) == 1, result.fills
    return result.fills.iloc[0]


def test_open_order_fills_at_todays_open():
    fill = only_fill(run(order_on(DAY, shares=10, at="open"), frames(), START, END))
    assert fill["date"] == ts(DAY)
    assert fill["price"] == 101.0
    assert fill["qty"] == 10


def test_close_order_fills_at_todays_close():
    fill = only_fill(run(order_on(DAY, shares=10, at="close"), frames(), START, END))
    assert fill["date"] == ts(DAY)
    assert fill["price"] == 105.0


def test_pct_order_sized_at_fill_price_whole_shares():
    fill = only_fill(run(order_on(DAY, pct=0.5, at="open"), frames(), START, END))
    assert fill["qty"] == 495  # floor(50_000 / 101)


def test_value_order_fractional_shares():
    res = run(order_on(DAY, value=1010, at="open"), frames(), START, END, fractional_shares=True)
    assert only_fill(res)["qty"] == pytest.approx(10.0)


def test_slippage_moves_buy_fill_up():
    res = run(order_on(DAY, shares=10, at="open"), frames(), START, END, slippage_bps=10)
    assert only_fill(res)["price"] == pytest.approx(101 * 1.001)


def test_commission_charged():
    res = run(
        order_on(DAY, shares=10, at="open"), frames(), START, END,
        commission_per_share=0.01, commission_min=1.0,
    )
    fill = only_fill(res)
    assert fill["commission"] == 1.0
    assert res.equity.loc[ts(DAY), "cash"] == pytest.approx(100_000 - 1010 - 1)


def test_cash_shortfall_resizes_by_default():
    res = run(order_on(DAY, shares=2000, at="open"), frames(), START, END)
    assert only_fill(res)["qty"] == 990  # floor(100_000 / 101)
    assert res.orders.iloc[0]["status"] == "resized"


def test_cash_shortfall_reject_mode():
    res = run(order_on(DAY, shares=2000, at="open"), frames(), START, END, cash_shortfall="reject")
    assert res.fills.empty
    assert res.orders.iloc[0]["status"] == "rejected"


def test_order_for_ticker_without_data_is_skipped_and_counted():
    def on_bar(self, ctx):
        if ctx.today == ts(DAY):
            ctx.order("ZZZ", shares=10)

    res = run(make_strategy(on_bar, universe=["AAA", "ZZZ"]), frames(), START, END)
    assert res.fills.empty
    assert res.orders.iloc[0]["status"] == "skipped"
    assert res.metrics["data_quality"]["signals_skipped_no_data"] == 1
    assert res.metrics["data_quality"]["skip_rate"] == 1.0

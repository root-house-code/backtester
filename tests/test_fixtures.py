"""Fixture-testing helpers: make_fixture and run_on_fixture."""

import math

import pandas as pd
import pytest
from helpers import make_strategy

from backtester.errors import ConfigError
from backtester.testing import make_fixture, run_on_fixture


def test_sessions_follow_the_nyse_calendar():
    fx = make_fixture(prices={"AAA": [10.0] * 15}, start="2024-01-10")
    assert len(fx.sessions) == 15
    assert fx.sessions[0] == pd.Timestamp("2024-01-10")
    assert pd.Timestamp("2024-01-15") not in fx.sessions  # MLK Day
    bar = fx.frames["AAA"].iloc[0]
    assert bar["open"] == bar["high"] == bar["low"] == bar["close"] == 10.0


def test_none_or_nan_means_no_bar():
    fx = make_fixture(prices={"AAA": [10.0, None, math.nan, 11.0]})
    assert list(fx.frames["AAA"]["close"]) == [10.0, 11.0]
    assert list(fx.frames["AAA"].index) == [fx.sessions[0], fx.sessions[3]]


def test_dataframe_prices():
    df = pd.DataFrame({"open": [10, 11], "high": [12, 12], "low": [9, 10], "close": [11, 11.5]})
    fx = make_fixture(prices={"aaa": df})
    assert fx.tickers == ["AAA"]
    assert fx.frames["AAA"].iloc[1]["high"] == 12.0
    assert (fx.frames["AAA"]["volume"] > 0).all()


@pytest.mark.parametrize("kwargs,match", [
    ({"prices": {"A": [1.0] * 3, "B": [1.0] * 4}}, "same number of sessions"),
    ({"prices": {"A": [1.0] * 3}, "start": "2024-01-06"}, "not an NYSE session"),
    ({"prices": {"A": [1.0] * 3}, "events": [{}]}, "dict of feed name"),
    ({"prices": {"A": pd.DataFrame({"close": [1.0]})}}, "missing columns"),
])
def test_bad_fixtures_fail_loudly(kwargs, match):
    with pytest.raises(ConfigError, match=match):
        make_fixture(**kwargs)


def test_orders_and_rejects_carry_session_numbers():
    def on_bar(self, ctx):
        k = len(ctx.history("close", 100))  # sessions seen so far
        if k == 3:
            ctx.order("AAA", shares=10, tag="day3")
            ctx.reject("BBB", "not interesting")

    fx = make_fixture(prices={"AAA": [10.0] * 6, "BBB": [20.0] * 6})
    result = run_on_fixture(make_strategy(on_bar, universe=["AAA", "BBB"]), fx)
    order = result.orders.iloc[0]
    assert (order["session"], order["ticker"], order["status"], order["tag"]) == (3, "AAA", "filled", "day3")
    assert order["date"] == fx.sessions[3]
    assert result.rejects[["session", "ticker", "reason"]].values.tolist() == [[3, "BBB", "not interesting"]]
    assert result.fills["session"].tolist() == [3]
    assert result.trades["session"].tolist() == [3]
    assert len(result.equity) == 6


def test_start_end_params_universe_and_settings():
    def on_bar(self, ctx):
        if not ctx.positions:
            ctx.order(ctx.universe[0], value=self.p.amount)

    from backtester import Param
    strategy = make_strategy(on_bar, universe=["AAA"], params={"amount": Param(float, default=100.0, min=0)})
    fx = make_fixture(prices={"AAA": [10.0] * 10, "BBB": [5.0] * 10})
    result = run_on_fixture(strategy, fx, params={"amount": 50.0}, start=4, end=7, universe=["BBB"],
                            initial_capital=1_000.0, fractional_shares=True)
    assert result.orders[["session", "ticker"]].values.tolist() == [[4, "BBB"]]
    assert result.fills["qty"].iloc[0] == pytest.approx(10.0)  # 50 / 5
    assert result.equity["session"].tolist() == [4, 5, 6, 7]
    assert result.equity["equity"].iloc[0] == pytest.approx(1_000.0)

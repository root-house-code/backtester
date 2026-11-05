from my_strategy import MyStrategy

from backtester.testing import make_fixture, run_on_fixture


def test_buys_at_the_open_after_a_breakout():
    # 30 flat sessions, then session 30 closes above the prior 20-session high.
    fx = make_fixture(prices={"AAA": [100.0] * 30 + [105.0] * 6, "BBB": [50.0] * 36})
    result = run_on_fixture(MyStrategy, fx)
    assert result.orders["ticker"].tolist() == ["AAA"]
    assert result.orders["session"].tolist() == [31]  # decided before session 31's open
    assert result.orders["status"].tolist() == ["filled"]


def test_exits_after_the_hold_period():
    fx = make_fixture(prices={"AAA": [100.0] * 30 + [105.0] * 20})
    result = run_on_fixture(MyStrategy, fx, params={"hold": 5})
    trade = result.trades.iloc[0]
    assert trade["exit_reason"] == "exit_after"
    assert trade["sessions_held"] == 5

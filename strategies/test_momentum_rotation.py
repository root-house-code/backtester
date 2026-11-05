from momentum_rotation import MomentumRotation

from backtester.testing import make_fixture, run_on_fixture

N = 60  # sessions: 2024-01-02 through late March


def leader_then_laggard():
    """A rises through session 25 and then falls; B is flat until session 25 and then rises."""
    a = [100 * 1.01 ** min(k, 25) * 0.99 ** max(k - 25, 0) for k in range(N)]
    b = [100 * 1.01 ** max(k - 25, 0) for k in range(N)]
    return make_fixture(prices={"A": a, "B": b, "FLAT": [100.0] * N})


def test_rebalances_only_on_the_first_session_of_each_month():
    fx = leader_then_laggard()
    result = run_on_fixture(MomentumRotation, fx, params={"lookback": 20, "top_n": 1}, start=1)
    s = fx.sessions
    first_of_month = {k for k in range(1, N) if s[k].month != s[k - 1].month}
    assert set(result.orders["session"]) <= first_of_month


def test_holds_the_leader_then_rotates_when_leadership_changes():
    fx = leader_then_laggard()
    result = run_on_fixture(MomentumRotation, fx, params={"lookback": 20, "top_n": 1}, start=1)
    trades = result.trades
    # February: A had the best trailing return, so it is bought.
    a = trades[trades["ticker"] == "A"].iloc[0]
    assert a["entry_date"].month == 2
    # March: B now leads, so A is sold in the rebalance and B is held to the end.
    assert a["exit_reason"] == "rebalance"
    b = trades[trades["ticker"] == "B"].iloc[0]
    assert b["entry_date"] == a["exit_date"]
    assert bool(b["is_open"])
    assert "FLAT" not in set(trades["ticker"])

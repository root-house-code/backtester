"""Accounting invariants, checked by replaying the fill log independently of the ledger."""

import pandas as pd
import pytest
from helpers import STRATEGIES, make_strategy, rotation_frames, run, ts

from backtester.testing import make_bars

COSTS = dict(commission_per_share=0.005, commission_min=1.0, slippage_bps=5)


def replay_equity(result, frames, capital):
    """Rebuild daily cash and equity from fills and raw closes."""
    closes = pd.DataFrame({t: df["close"] for t, df in frames.items()})
    closes = closes.reindex(result.equity.index.union(closes.index)).ffill()
    fills = result.fills.groupby("date")
    cash, holdings, rows = capital, {}, {}
    for date in result.equity.index:
        if date in fills.groups:
            for _, f in fills.get_group(date).iterrows():
                cash -= f["qty"] * f["price"] + f["commission"]
                holdings[f["ticker"]] = holdings.get(f["ticker"], 0.0) + f["qty"]
        value = sum(q * closes.loc[date, t] for t, q in holdings.items() if q)
        rows[date] = (cash, cash + value)
    return pd.DataFrame(rows, index=["cash", "equity"]).T


@pytest.mark.parametrize("costs", [{}, COSTS], ids=["zero-cost", "with-costs"])
def test_cash_plus_positions_equals_equity_every_session(costs):
    frames = rotation_frames()
    res = run(STRATEGIES / "momentum_rotation.py", frames, "2015-01-01", "2019-12-31",
              benchmark="SPY", **costs)
    assert len(res.fills) > 20
    eq = res.equity
    assert (eq["cash"] + eq["positions_value"] - eq["equity"]).abs().max() < 1e-6
    replay = replay_equity(res, frames, 100_000)
    assert (replay["cash"] - eq["cash"]).abs().max() < 1e-6
    assert (replay["equity"] - eq["equity"]).abs().max() < 1e-6


@pytest.mark.parametrize("costs", [{}, COSTS], ids=["zero-cost", "with-costs"])
def test_trade_pnl_equals_equity_change_minus_costs(costs):
    res = run(STRATEGIES / "momentum_rotation.py", rotation_frames(), "2015-01-01", "2019-12-31",
              benchmark="SPY", **costs)
    total_pnl = res.trades["pnl"].sum()
    total_costs = res.fills["commission"].sum()
    equity_change = res.equity["equity"].iloc[-1] - 100_000
    assert total_pnl - total_costs == pytest.approx(equity_change, abs=1e-6)
    assert res.trades["pnl_net"].sum() == pytest.approx(equity_change, abs=1e-6)
    if costs:
        assert total_costs > 0


def test_ticker_that_stops_trading_closes_at_last_close_and_is_flagged():
    frames = {
        "AAA": make_bars("2024-03-01", "2024-03-29", price=100.0),
        "BBB": make_bars("2024-03-01", "2024-03-12", price=50.0,
                         overrides={"2024-03-12": {"close": 40, "low": 40}}),
    }

    def on_bar(self, ctx):
        if ctx.today == ts("2024-03-05"):
            ctx.order("BBB", shares=100)

    res = run(make_strategy(on_bar, universe=["AAA", "BBB"]), frames, "2024-03-01", "2024-03-29")
    trade = res.trades.iloc[0]
    assert not trade["is_open"]
    assert trade["exit_price"] == 40.0
    assert trade["exit_reason"] == "stopped_trading"
    assert "stopped_trading" in trade["flags"]
    assert res.equity["equity"].iloc[-1] == pytest.approx(100_000 - 100 * 10)

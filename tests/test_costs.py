"""Cost sanity: a zero-cost run never underperforms the same run with costs."""

import pytest
from helpers import STRATEGIES, rotation_frames, run

COST_LEVELS = [
    dict(commission_per_share=0.005, commission_min=1.0),
    dict(slippage_bps=5),
    dict(commission_per_share=0.01, commission_min=1.0, slippage_bps=20),
]


@pytest.mark.parametrize("strategy", ["momentum_rotation.py", "buy_and_hold.py"])
@pytest.mark.parametrize("costs", COST_LEVELS, ids=["commission", "slippage", "both"])
def test_zero_cost_never_underperforms(strategy, costs):
    frames = rotation_frames()
    args = (STRATEGIES / strategy, frames, "2015-01-01", "2019-12-31")
    free = run(*args, benchmark="SPY")
    costly = run(*args, benchmark="SPY", **costs)
    assert free.equity["equity"].iloc[-1] >= costly.equity["equity"].iloc[-1]
    assert free.metrics["returns"]["total_return"] >= costly.metrics["returns"]["total_return"]

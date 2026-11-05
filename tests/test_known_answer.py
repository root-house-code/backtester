"""Known answer on real data: buy-and-hold SPY matches SPY's adjusted return.

Needs network access (yfinance). Run with: pytest -m slow
"""

import pandas as pd
import pytest
from helpers import ROOT, STRATEGIES

from backtester.data.yfinance_provider import YFinanceProvider
from backtester.run import run_backtest

START, END = "2015-01-01", "2024-12-31"


@pytest.mark.slow
def test_buy_and_hold_spy_matches_adjusted_return(tmp_path):
    yf = pytest.importorskip("yfinance")

    provider = YFinanceProvider(cache_dir=ROOT / "data" / "cache")
    res = run_backtest(
        STRATEGIES / "buy_and_hold.py",
        {"start": START, "end": END, "universe": ["SPY"], "fractional_shares": True},
        provider=provider,
    )

    # Independent reference: Yahoo's own adjusted prices, fetched directly.
    ref = yf.Ticker("SPY").history(start=START, end="2025-01-01", auto_adjust=True)
    ref.index = pd.DatetimeIndex(ref.index).tz_localize(None).normalize()
    # Same convention as the engine: buy at the first session's open, value at the last close.
    expected = ref["Close"].iloc[-1] / ref["Open"].iloc[0] - 1

    actual = res.metrics["returns"]["total_return"]
    assert res.equity.index[0] == ref.index[0]
    assert res.equity.index[-1] == ref.index[-1]
    assert abs(actual - expected) < 1e-4, (actual, expected)

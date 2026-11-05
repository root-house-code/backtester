"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from backtester import Strategy
from backtester.run import run_backtest
from backtester.testing import InMemoryProvider, random_walk_bars

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = ROOT / "strategies"

ROTATION_TICKERS = ["SPY", "QQQ", "IWM", "EFA", "TLT", "GLD"]


def make_strategy(on_bar, universe=("AAA",), name="test", **attrs):
    """Build a Strategy subclass whose on_bar is ``on_bar(self, ctx)``."""
    body = dict(
        api_version=1,
        name=name,
        description="test strategy",
        requires=["prices"],
        universe=universe if isinstance(universe, str) else list(universe),
        params={},
        on_bar=lambda self, ctx: on_bar(self, ctx),
    )
    body.update(attrs)
    return type("TestStrategy", (Strategy,), body)


def run(strategy, frames: dict, start, end, **overrides):
    """Run a strategy against in-memory frames. Benchmark defaults to the first frame."""
    config = {"start": start, "end": end, "benchmark": next(iter(frames))}
    config.update(overrides)
    return run_backtest(strategy, config, provider=InMemoryProvider(frames))


def rotation_frames(start="2013-01-01", end="2019-12-31") -> dict:
    """Deterministic synthetic bars for the momentum rotation tickers."""
    frames = {}
    for i, t in enumerate(ROTATION_TICKERS):
        frames[t] = random_walk_bars(
            start, end, seed=100 + i, start_price=50 + 20 * i,
            drift=0.0002 + 0.0001 * i, daily_vol=0.008 + 0.002 * i,
        )
    return frames


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s)

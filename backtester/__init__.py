"""Deterministic daily-bar backtesting engine."""

__version__ = "0.1.0"

from .context import Context, Position  # noqa: E402
from .data.panel import Bar  # noqa: E402
from .errors import (  # noqa: E402
    BacktesterError,
    ConfigError,
    DataError,
    LookaheadError,
    OrderError,
    StrategyError,
)
from .orders import Fill  # noqa: E402
from .strategy import Param, Strategy  # noqa: E402

__all__ = [
    "BacktesterError",
    "Bar",
    "ConfigError",
    "Context",
    "DataError",
    "Fill",
    "LookaheadError",
    "OrderError",
    "Param",
    "Position",
    "Strategy",
    "StrategyError",
]

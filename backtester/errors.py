"""Exception types raised by the engine."""


class BacktesterError(Exception):
    """Base class for all engine errors."""


class LookaheadError(BacktesterError):
    """Raised when a strategy tries to read data that was not yet knowable."""


class StrategyError(BacktesterError):
    """Raised when a strategy file breaks the strategy contract."""


class OrderError(BacktesterError, ValueError):
    """Raised when a strategy submits a malformed order."""


class ConfigError(BacktesterError):
    """Raised when a run config is invalid."""


class DataError(BacktesterError):
    """Raised when market data is missing or malformed."""

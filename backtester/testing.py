"""Synthetic markets for testing strategies.

Strategy authors use ``make_fixture`` and ``run_on_fixture`` to check a
strategy's logic exactly, without real data::

    from backtester.testing import make_fixture, run_on_fixture

    fx = make_fixture(prices={"AAA": [100] * 30 + [105] * 10})
    result = run_on_fixture(MyStrategy, fx)
    assert result.orders["session"].tolist() == [31]

``make_bars`` and ``random_walk_bars`` build bars for the engine's own tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .calendar import sessions as nyse_sessions
from .data.memory import InMemoryProvider
from .errors import ConfigError
from .feeds import StaticFeed

__all__ = ["Fixture", "FixtureResult", "InMemoryProvider", "make_bars", "make_fixture",
           "nyse_sessions", "random_walk_bars", "run_on_fixture"]


def make_bars(
    start,
    end,
    price: float = 100.0,
    volume: float = 1_000_000.0,
    overrides: dict | None = None,
) -> pd.DataFrame:
    """Flat bars (open = high = low = close = ``price``) on every NYSE session.

    ``overrides`` maps a date to a dict of column values for that session,
    e.g. ``{"2024-03-05": {"open": 101, "high": 106, "low": 100, "close": 105}}``.
    """
    idx = nyse_sessions(start, end)
    df = pd.DataFrame(
        {"open": price, "high": price, "low": price, "close": price, "volume": volume},
        index=idx,
        dtype=float,
    )
    for date, values in (overrides or {}).items():
        date = pd.Timestamp(date)
        if date not in df.index:
            raise ValueError(f"{date.date()} is not an NYSE session")
        for col, val in values.items():
            df.loc[date, col] = float(val)
    return df


def random_walk_bars(
    start,
    end,
    seed: int,
    start_price: float = 100.0,
    daily_vol: float = 0.012,
    drift: float = 0.0004,
    volume: float = 2_000_000.0,
) -> pd.DataFrame:
    """Deterministic geometric random-walk bars with overnight gaps and intraday range."""
    idx = nyse_sessions(start, end)
    rng = np.random.default_rng(seed)
    n = len(idx)
    gap = rng.normal(0.0, daily_vol * 0.3, n)
    intraday = rng.normal(drift, daily_vol, n)
    close = np.empty(n)
    open_ = np.empty(n)
    prev = start_price
    for i in range(n):
        open_[i] = prev * np.exp(gap[i])
        close[i] = open_[i] * np.exp(intraday[i])
        prev = close[i]
    wick_hi = np.abs(rng.normal(0.0, daily_vol * 0.5, n))
    wick_lo = np.abs(rng.normal(0.0, daily_vol * 0.5, n))
    high = np.maximum(open_, close) * (1 + wick_hi)
    low = np.minimum(open_, close) * (1 - wick_lo)
    vol = volume * np.exp(rng.normal(0.0, 0.3, n))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


# --- fixture testing ----------------------------------------------------------

@dataclass
class Fixture:
    """A tiny synthetic market built by ``make_fixture``.

    Attributes:
        sessions: The fixture's trading dates. ``sessions[k]`` is session ``k``.
        frames: Daily bars per ticker (open, high, low, close, volume).
        feeds: Event and reference feeds, by name.
    """

    sessions: pd.DatetimeIndex
    frames: dict
    feeds: dict = field(default_factory=dict)

    @property
    def tickers(self) -> list[str]:
        return list(self.frames)


@dataclass
class FixtureResult:
    """What a strategy did on a fixture, returned by ``run_on_fixture``.

    Every frame starts with a ``session`` column: the fixture session number
    (0-based) of the row's date, so a test can check "a buy on session 31".

    Attributes:
        orders: Orders the strategy placed: ``session``, ``date`` (the session
            the order was placed for), ``ticker``, ``kind`` (``order``,
            ``target_weights`` or ``close``), ``shares``, ``pct``, ``value``,
            ``weight``, ``at``, ``stop``, ``target``, ``exit_after``, ``tag``,
            ``status`` (``filled``, ``resized``, ``rejected``, ``skipped``,
            ``no_change`` or ``cancelled``), ``filled_qty``, ``fill_price``,
            ``message``.
        rejects: Candidates skipped with ``ctx.reject``: ``session``, ``date``,
            ``ticker``, ``reason``, ``tag``.
        fills: Every fill, including engine exits (stops, targets, ``exit_after``):
            ``session``, ``date``, ``ticker``, ``qty`` (negative = sold),
            ``price``, ``commission``, ``at``, ``reason``, ``tag``.
        trades: Trade records (``session`` is the entry session):
            ``entry_date``, ``entry_price``, ``exit_date``, ``exit_price``,
            ``shares``, ``pnl``, ``return``, ``sessions_held``, ``exit_reason``,
            ``tag``, ``is_open``.
        equity: Daily ``equity``, ``cash``, ``positions_value`` and ``exposure``.
        signals: The full signal audit: every feed event delivered to the
            strategy (``decision`` "event", with ``feed``, ``event_id`` and an
            ``outcome``: the order status, "rejected" or "no action"), every
            order and every reject.
        log: Run log lines, including ``ctx.log`` output.
    """

    orders: pd.DataFrame
    rejects: pd.DataFrame
    fills: pd.DataFrame
    trades: pd.DataFrame
    equity: pd.DataFrame
    signals: pd.DataFrame
    log: list


def make_fixture(prices: dict, events: dict | None = None, refs: dict | None = None,
                 start="2024-01-02", volume: float = 1_000_000.0) -> Fixture:
    """Build a tiny synthetic market for testing a strategy's logic.

    Args:
        prices: Ticker -> prices. Either a list of closes (each bar is flat:
            open = high = low = close), or a DataFrame with ``open``, ``high``,
            ``low``, ``close`` and optionally ``volume`` columns, one row per
            session. A close of ``None`` or NaN means the ticker has no bar that
            session. Every ticker needs the same number of sessions.
        events: Event feeds: feed name -> records, each record a dict (or a
            DataFrame row) with ``ticker``, ``available_at`` and any value
            columns, e.g. ``{"filings": [{"ticker": "AAA", "available_at":
            "2024-01-05 17:00", "value": 2.5}]}``. Times without an offset are
            US/Eastern; a date alone means 18:00 (after the close).
        refs: Reference feeds, in the same form, e.g. ``{"shares_outstanding":
            [{"ticker": "AAA", "available_at": "2024-01-02", "shares_outstanding": 1e6}]}``.
            Supplying ``shares_outstanding`` makes ``market_cap`` work too.
        start: Date of session 0, an NYSE session. Later sessions follow the
            NYSE calendar.
        volume: Daily volume for bars built from a list of closes.

    Returns:
        A ``Fixture``. ``fixture.sessions[k]`` is the date of session ``k``.
    """
    if not prices:
        raise ConfigError("make_fixture needs prices for at least one ticker")
    lengths = {str(t): len(v) for t, v in prices.items()}
    if len(set(lengths.values())) != 1:
        raise ConfigError(f"every ticker needs the same number of sessions; got {lengths}")
    n = next(iter(lengths.values()))
    first = pd.Timestamp(start)
    sessions = nyse_sessions(first, first + pd.Timedelta(days=n * 2 + 10))[:n]
    if len(sessions) == 0 or sessions[0] != first:
        raise ConfigError(f"start {first.date()} is not an NYSE session")
    frames = {}
    for ticker, data in prices.items():
        if isinstance(data, pd.DataFrame):
            df = data.reset_index(drop=True).astype(float)
            missing = {"open", "high", "low", "close"} - set(df.columns)
            if missing:
                raise ConfigError(f"{ticker}: price frame is missing columns {sorted(missing)}")
            if "volume" not in df:
                df["volume"] = volume
        else:
            closes = pd.Series([np.nan if c is None else float(c) for c in data], dtype=float)
            df = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                               "volume": float(volume)})
        df.index = sessions
        frames[str(ticker).upper()] = df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    feeds = {}
    for kind, given in (("event", events), ("reference", refs)):
        if given is not None and not isinstance(given, dict):
            raise ConfigError(f"{'events' if kind == 'event' else 'refs'} must be a dict of feed name -> records")
        for name, records in (given or {}).items():
            if name in feeds:
                raise ConfigError(f"feed {name!r} is given twice")
            feeds[name] = StaticFeed(name, kind, records)
    return Fixture(sessions, frames, feeds)


def run_on_fixture(strategy, fixture: Fixture, params: dict | None = None, start=None, end=None,
                   universe: list | None = None, **settings) -> FixtureResult:
    """Run a strategy on a fixture and return what it did.

    Args:
        strategy: A Strategy subclass, or the path to a strategy file.
        fixture: From ``make_fixture``.
        params: Parameter overrides, e.g. ``{"lookback": 20}``.
        start: First session to trade, as a session number or a date (default
            0). Earlier sessions are visible to the strategy as history.
        end: Last session to trade, as a session number or a date (default: the
            last session).
        universe: Tickers in scope (default: every fixture ticker, or the
            strategy's own ``from_feed`` universe if it has one).
        **settings: Any other run setting, e.g. ``initial_capital=10_000``,
            ``slippage_bps=5`` or ``fractional_shares=True``.

    Returns:
        A ``FixtureResult``.
    """
    from .config import universe_feed
    from .feeds import FeedRegistry
    from .run import run_backtest
    from .strategy import load_strategy_file

    cls = load_strategy_file(strategy) if isinstance(strategy, (str, Path)) else strategy

    def date_of(x, default: int) -> pd.Timestamp:
        if x is None:
            return fixture.sessions[default]
        if isinstance(x, (int, np.integer)):
            return fixture.sessions[int(x)]
        return pd.Timestamp(x)

    overrides = {
        "start": date_of(start, 0).date().isoformat(),
        "end": date_of(end, -1).date().isoformat(),
        "benchmark": fixture.tickers[0],
        **settings,
    }
    if universe is not None:
        overrides["universe"] = list(universe)
    elif universe_feed(cls.universe) is None:
        overrides["universe"] = fixture.tickers
    if params:
        overrides["params"] = dict(params)
    feeds = FeedRegistry(feeds_dir=None, plugins_dir=None, cache_dir=Path("data") / "cache",
                         refresh=False, extra=fixture.feeds)
    result = run_backtest(strategy, overrides, provider=InMemoryProvider(fixture.frames), feeds=feeds)
    number = {d: k for k, d in enumerate(fixture.sessions)}

    def with_session(df: pd.DataFrame, col: str) -> pd.DataFrame:
        df = df.reset_index(drop=True)
        df.insert(0, "session", pd.array([number.get(pd.Timestamp(d)) for d in df[col]], dtype="Int64"))
        return df

    orders = result.orders[result.orders["source"] == "strategy"].rename(columns={"created": "date"})
    signals = result.signals
    rejects = signals[signals["decision"] == "reject"][["date", "ticker", "reason", "tag"]]
    return FixtureResult(
        orders=with_session(orders, "date"),
        rejects=with_session(rejects, "date"),
        fills=with_session(result.fills, "date"),
        trades=with_session(result.trades, "entry_date"),
        equity=with_session(result.equity.reset_index(), "date"),
        signals=with_session(signals, "date"),
        log=list(result.log),
    )

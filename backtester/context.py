"""The point-in-time context passed to strategy hooks as ``ctx``.

Everything a strategy knows about the market comes through ``ctx``. On session
t it exposes only what was knowable before t's open: bars through the close of
t-1. Any attempt to read later data raises ``LookaheadError``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, NamedTuple

import numpy as np
import pandas as pd

from . import calendar
from .data.panel import Bar
from .errors import OrderError, StrategyError

if TYPE_CHECKING:
    from .engine import Engine

PERIODS = ("week", "month", "quarter", "year")


class Position(NamedTuple):
    """A current holding.

    Attributes:
        ticker: Ticker held.
        qty: Shares held (positive = long). 0 if not held.
        avg_price: Average entry price of the shares still held.
        market_value: ``qty`` x the last completed close.
    """

    ticker: str
    qty: float
    avg_price: float
    market_value: float


def _period_key(date: pd.Timestamp, period: str):
    if period == "week":
        iso = date.isocalendar()
        return (iso.year, iso.week)
    if period == "month":
        return (date.year, date.month)
    if period == "quarter":
        return (date.year, (date.month - 1) // 3)
    return date.year


class Context:
    """Point-in-time view of the market and the account, passed to every hook."""

    def __init__(self, engine: Engine, seed: int):
        self._engine = engine
        #: Dict that persists across sessions. Store any strategy state here.
        self.state: dict = {}
        #: Seeded ``numpy.random.Generator``. The only allowed source of randomness.
        self.rng: np.random.Generator = np.random.default_rng(seed)

    # --- clock and universe -------------------------------------------
    @property
    def today(self) -> pd.Timestamp:
        """Current session date. Decisions are made before this session's open."""
        return self._engine.view.today

    @property
    def universe(self) -> list[str]:
        """Tickers in scope this session, in config order. For a ``from_feed``
        universe: every ticker the feed has mentioned in records visible so far,
        in order of first mention."""
        return list(self._engine.universe_tickers)

    @property
    def benchmark(self) -> str:
        """The run's benchmark ticker (``SPY`` unless the run sets another).

        Its prices are always loaded, so a strategy may trade it even when it
        is not in ``ctx.universe`` (for example, to keep idle cash in the
        index it is judged against) and read it with
        ``ctx.history(field, n, [ctx.benchmark])``."""
        return self._engine.cfg["benchmark"]

    def is_first_session_of(self, period: str) -> bool:
        """True if today is the first trading session of its ``"week"``,
        ``"month"``, ``"quarter"`` or ``"year"``.

        Compares against the previous NYSE session, so a run starting mid-month
        does not count its first session as the first of the month.
        """
        if period not in PERIODS:
            raise ValueError(f"period must be one of {PERIODS}, got {period!r}")
        prev = calendar.previous_session(self.today)
        return _period_key(self.today, period) != _period_key(prev, period)

    # --- market data ---------------------------------------------------
    def history(self, field: str, n: int, tickers: list[str] | None = None) -> pd.DataFrame:
        """Last ``n`` completed bars of one field as a DataFrame.

        Args:
            field: ``"open"``, ``"high"``, ``"low"``, ``"close"``, ``"volume"``
                or ``"dollar_volume"``. Prices are split- and dividend-adjusted.
            n: Number of sessions. Fewer rows come back if history is shorter.
            tickers: Columns to return; defaults to ``ctx.universe``.

        Returns:
            Rows are sessions (oldest first, the last row is the previous
            session), columns are tickers, NaN where a ticker had no bar. The
            frame is a copy; changing it does not affect later calls.
        """
        tickers = self.universe if tickers is None else [t.upper() for t in tickers]
        sids = [self._engine.master.to_id(t) for t in tickers]
        return self._engine.view.window(field, n, sids, tickers)

    def price(self, ticker: str) -> float:
        """Last completed close for ``ticker`` (the previous session's, or the
        most recent before it if the ticker did not trade). NaN if none."""
        return self._engine.view.last_close(self._engine.master.to_id(ticker))

    def bar(self, ticker: str, date) -> Bar | None:
        """The completed bar for ``ticker`` on ``date``, or None if it had no bar.

        Raises ``LookaheadError`` if ``date`` is today or later.
        """
        return self._engine.view.bar(self._engine.master.to_id(ticker), date)

    # --- feeds ---------------------------------------------------------
    def events(self, name: str) -> list:
        """Records from event feed ``name`` that became visible since the last session.

        A record is visible on session t if its ``available_at`` is before
        09:30 ET on t, so acting on it ``at="open"`` trades at t's open. Each
        record is returned on exactly one session. The feed must be in
        ``requires``.

        Returns:
            A list of ``Event`` objects in ``available_at`` order (empty if
            none). Use ``e.id`` as the order ``tag`` (or in ``ctx.reject``) so
            the signal audit can show what happened to each event.
        """
        view = self._engine.feeds.get(name)
        if view is None or view.kind != "event":
            event_feeds = sorted(n for n, v in self._engine.feeds.items() if v.kind == "event")
            raise StrategyError(f"{name!r} is not an event feed in requires (event feeds: {event_feeds})")
        return list(self._engine.current_events.get(name, []))

    def ref(self, ticker: str, field: str, default=float("nan")):
        """Latest value of a reference field for ``ticker``, as known before today's open.

        ``field`` is a column of a reference feed in ``requires``, e.g.
        ``"market_cap"``, ``"shares_outstanding"`` or ``"sector"``; write
        ``"feed.column"`` if two feeds share a column name. ``market_cap`` is
        the last raw (unadjusted) close times the latest shares outstanding.

        Returns:
            The value, or ``default`` (NaN) if nothing is known yet.
        """
        engine = self._engine
        if field not in engine.ref_fields:
            raise StrategyError(f"unknown reference field {field!r} (available: {sorted(engine.ref_fields)}); "
                                "add its feed to requires")
        feed, column = engine.ref_fields[field]
        ticker = engine.master.to_ticker(engine.master.to_id(ticker))
        if feed == "market_cap":
            shares = engine.feeds["shares_outstanding"].asof(ticker, "shares_outstanding", engine.i, None)
            raw_close = engine.view.last_raw_close(engine.master.to_id(ticker))
            if shares is None or math.isnan(raw_close):
                return default
            return float(shares) * raw_close
        return engine.feeds[feed].asof(ticker, column, engine.i, default)

    # --- account -------------------------------------------------------
    def position(self, ticker: str) -> Position:
        """Current holding in ``ticker`` (``qty`` is 0 if none)."""
        sid = self._engine.master.to_id(ticker)
        return self._position(sid)

    @property
    def positions(self) -> dict[str, Position]:
        """All current holdings, keyed by ticker."""
        out = {}
        for sid in self._engine.ledger.held():
            pos = self._position(sid)
            out[pos.ticker] = pos
        return out

    def _position(self, sid) -> Position:
        ledger = self._engine.ledger
        qty = ledger.qty(sid)
        ticker = self._engine.master.to_ticker(sid)
        if qty <= 0:
            return Position(ticker, 0.0, float("nan"), 0.0)
        last = self._engine.view.last_close(sid)
        return Position(ticker, qty, ledger.avg_price(sid), qty * last)

    @property
    def cash(self) -> float:
        """Cash available."""
        return self._engine.ledger.cash

    @property
    def equity(self) -> float:
        """Cash plus holdings valued at the last completed close."""
        total = self.cash
        for sid in self._engine.ledger.held():
            last = self._engine.view.last_close(sid)
            if not math.isnan(last):
                total += self._engine.ledger.qty(sid) * last
        return total

    # --- orders --------------------------------------------------------
    def order(
        self,
        ticker: str,
        shares: float | None = None,
        pct: float | None = None,
        value: float | None = None,
        at: str = "open",
        stop: float | None = None,
        target: float | None = None,
        exit_after: int | None = None,
        tag: str | None = None,
    ) -> int:
        """Queue an order. Give exactly one of ``shares``, ``pct`` or ``value``.

        Args:
            ticker: Ticker to trade.
            shares: Fixed share count. Negative sells.
            pct: Fraction of equity, e.g. ``0.05`` for 5%. Negative sells.
                Shares are computed at fill time from the fill price.
            value: Dollar amount. Negative sells. Shares computed at fill time.
            at: ``"open"`` fills at today's open; ``"close"`` at today's close.
            stop: Exit if the price trades at or below this level (adjusted terms).
            target: Exit if the price trades at or above this level.
            exit_after: Exit at the open this many trading sessions after entry.
            tag: Free text kept on the order, fill and trade (e.g. an event id).

        Returns:
            The order id.

        Long-only in V1: a sell larger than the position is clipped to it.
        ``stop``, ``target`` and ``exit_after`` apply to buys and create a
        separate trade record.
        """
        sizes = [s for s in (shares, pct, value) if s is not None]
        if len(sizes) != 1:
            raise OrderError("give exactly one of shares, pct or value")
        size = sizes[0]
        if isinstance(size, bool) or not isinstance(size, (int, float, np.integer, np.floating)):
            raise OrderError(f"order size must be a number, got {size!r}")
        size = float(size)
        if not math.isfinite(size) or size == 0:
            raise OrderError(f"order size must be a non-zero finite number, got {size!r}")
        if shares is not None and not self._engine.cfg["fractional_shares"] and not size.is_integer():
            raise OrderError(f"shares must be whole (fractional_shares is off), got {size!r}")
        if pct is not None and abs(size) > 1:
            raise OrderError(f"pct is a fraction of equity in [-1, 1], got {size!r}")
        self._check_at(at)
        if size < 0 and (stop is not None or target is not None or exit_after is not None):
            raise OrderError("stop, target and exit_after apply to buys only")
        for name, level in (("stop", stop), ("target", target)):
            if level is not None and not (isinstance(level, (int, float)) and level > 0):
                raise OrderError(f"{name} must be a positive price, got {level!r}")
        if stop is not None and target is not None and stop >= target:
            raise OrderError(f"stop ({stop}) must be below target ({target})")
        if exit_after is not None:
            if isinstance(exit_after, bool) or not isinstance(exit_after, (int, np.integer)) or exit_after < 1:
                raise OrderError(f"exit_after must be a positive whole number of sessions, got {exit_after!r}")
            exit_after = int(exit_after)
        return self._engine.queue_order(
            ticker, kind="order", at=at,
            shares=size if shares is not None else None,
            pct=size if pct is not None else None,
            value=size if value is not None else None,
            stop=None if stop is None else float(stop),
            target=None if target is None else float(target),
            exit_after=exit_after, tag=None if tag is None else str(tag),
        )

    def target_weights(self, weights: dict[str, float], at: str = "open") -> list[int]:
        """Rebalance so each ticker is ``weight`` x equity; close holdings not listed.

        Weights must be >= 0 and sum to at most 1. Share counts are computed at
        fill time from the fill price. Returns the order ids.
        """
        self._check_at(at)
        if not isinstance(weights, dict):
            raise OrderError("weights must be a dict of ticker -> weight")
        clean = {}
        for t, w in weights.items():
            if isinstance(w, bool) or not isinstance(w, (int, float, np.integer, np.floating)) or not math.isfinite(w) or w < 0:
                raise OrderError(f"weight for {t} must be a finite number >= 0, got {w!r}")
            clean[str(t).upper()] = float(w)
        if sum(clean.values()) > 1 + 1e-9:
            raise OrderError(f"weights sum to {sum(clean.values()):.6f}; the maximum is 1 (no leverage)")
        ids = []
        for t, w in clean.items():
            ids.append(self._engine.queue_order(t, kind="target_weights", at=at, weight=w))
        for sid in self._engine.ledger.held():
            t = self._engine.master.to_ticker(sid)
            if t not in clean:
                ids.append(self._engine.queue_order(t, kind="target_weights", at=at, weight=0.0))
        return ids

    def close(self, ticker: str, at: str = "open", tag: str | None = None) -> int:
        """Exit the whole position in ``ticker`` at today's open or close. Returns the order id."""
        self._check_at(at)
        return self._engine.queue_order(ticker, kind="close", at=at, tag=tag)

    def reject(self, subject, reason: str) -> None:
        """Record that a candidate was considered and skipped, and why.

        ``subject`` is a ticker or an object with a ``ticker`` attribute (and
        optionally ``id``). Rejections appear in the signal audit.
        """
        if isinstance(subject, str):
            ticker, tag = subject, None
        else:
            ticker = getattr(subject, "ticker", None)
            tag = getattr(subject, "id", None)
            if ticker is None:
                raise ValueError("reject() subject must be a ticker or have a .ticker attribute")
        self._engine.record_reject(str(ticker).upper(), str(reason), None if tag is None else str(tag))

    def log(self, msg: str, **fields) -> None:
        """Write a structured line to the run's log."""
        self._engine.log("info", str(msg), source="strategy", **fields)

    @staticmethod
    def _check_at(at):
        if at not in ("open", "close"):
            raise OrderError(f'at must be "open" or "close", got {at!r}')

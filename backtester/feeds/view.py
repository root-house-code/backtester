"""Point-in-time access to feeds for the engine.

A record becomes visible at the first session whose 09:30 ET open is strictly
after its ``available_at``. Records with ``available_at`` before the previous
session's open count as old news: visible to ``ctx.ref`` and to ``from_feed``
universes, but never delivered by ``ctx.events``.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .. import calendar
from .base import EVENT, FeedData, session_opens_utc, utc_ns


class Event:
    """One record from an event feed, as returned by ``ctx.events``.

    Attributes:
        id: Stable record id (use it as an order ``tag`` and in ``ctx.reject``,
            so the signal audit can link the event to what happened to it).
        ticker: Ticker the record is about.
        available_at: When the record became knowable (UTC timestamp).
        entity_id: Optional entity identifier (CIK, FIGI, ...), or None.
        feed: Name of the feed.
        data: Every value column, as a dict. Each is also an attribute:
            ``e.value`` or ``e["value"]``.
    """

    __slots__ = ("id", "ticker", "available_at", "entity_id", "feed", "data")

    def __init__(self, id, ticker, available_at, entity_id, feed, data):
        self.id, self.ticker, self.available_at = id, ticker, available_at
        self.entity_id, self.feed, self.data = entity_id, feed, data

    def __getattr__(self, name):
        data = object.__getattribute__(self, "data")
        if name in data:
            return data[name]
        raise AttributeError(f"event from {self.feed!r} has no field {name!r}; fields: {sorted(data)}")

    def __getitem__(self, name):
        return self.data[name]

    def get(self, name, default=None):
        return self.data.get(name, default)

    def __repr__(self):
        return f"Event({self.feed}, {self.ticker}, {self.available_at.isoformat()}, {self.data})"


class FeedView:
    """Indexes one feed's records by the session at which each becomes visible."""

    def __init__(self, data: FeedData, sessions: pd.DatetimeIndex):
        self.name, self.kind, self.columns = data.name, data.kind, list(data.value_columns)
        r = data.records.reset_index(drop=True)
        self.records = r
        # opens[0] is the open of the session before the first one; opens[k + 1] is session k's.
        prev = calendar.previous_session(sessions[0])
        opens = session_opens_utc(pd.DatetimeIndex([prev]).append(pd.DatetimeIndex(sessions)))
        times = utc_ns(r["available_at"]) if len(r) else np.array([], dtype=np.int64)
        # Number of opens at or before available_at, minus one: -1 means old news (before the
        # previous session's open); k means the record first becomes visible at session k.
        self.visible = (np.searchsorted(opens, times, side="right") - 1).astype(int)
        self.tickers = r["ticker"].to_numpy() if len(r) else np.array([], dtype=object)

        first = {}
        for t, v in zip(self.tickers, self.visible):
            if t not in first:
                first[t] = v
        self.first_visible = first

        if self.kind != EVENT:
            self._by_ticker = {}
            for t, idx in r.groupby("ticker", sort=False).indices.items():
                idx = np.asarray(idx)
                self._by_ticker[t] = (self.visible[idx], {c: r[c].to_numpy()[idx] for c in self.columns})

    def events_at(self, i: int) -> list[Event]:
        """Records that become visible at session i (in available_at order)."""
        lo, hi = np.searchsorted(self.visible, i, side="left"), np.searchsorted(self.visible, i, side="right")
        out = []
        for k in range(lo, hi):
            row = self.records.iloc[k]
            entity = row["entity_id"]
            out.append(Event(row["id"], row["ticker"], row["available_at"],
                             None if entity is None or (isinstance(entity, float) and math.isnan(entity)) else entity,
                             self.name, {c: _py(row[c]) for c in self.columns}))
        return out

    def asof(self, ticker: str, column: str, i: int, default=float("nan")):
        """Latest value of ``column`` for ``ticker`` visible at session i."""
        entry = self._by_ticker.get(ticker)
        if entry is None:
            return default
        vis, cols = entry
        k = np.searchsorted(vis, i, side="right") - 1
        if k < 0:
            return default
        return _py(cols[column][k])

    def tickers_visible(self, i: int) -> list[str]:
        """Tickers mentioned by any record visible at session i, in order of first mention."""
        return [t for t, v in self.first_visible.items() if v <= i]


def _py(value):
    if isinstance(value, np.generic):
        return value.item()
    return value

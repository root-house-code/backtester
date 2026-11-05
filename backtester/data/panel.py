"""In-memory price panel for a run, and the point-in-time view strategies read through."""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd

from ..errors import LookaheadError
from ..ids import SecurityId
from .provider import BAR_COLUMNS

STRATEGY_FIELDS = ("open", "high", "low", "close", "volume", "dollar_volume")


class Bar(NamedTuple):
    """One completed daily bar, returned by ``ctx.bar``.

    Attributes:
        date: Session date.
        open, high, low, close: Prices, adjusted for splits and dividends.
        volume: Shares traded, adjusted for splits.
    """

    date: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


class PricePanel:
    """Dense [session x security] arrays for every bar field.

    Rows are NYSE sessions; NaN means no bar for that security on that session.
    """

    def __init__(self, sessions: pd.DatetimeIndex, security_ids: list[SecurityId],
                 arrays: dict[str, np.ndarray]):
        self.sessions = sessions
        self.security_ids = list(security_ids)
        self._col = {sid: j for j, sid in enumerate(self.security_ids)}
        self.arrays = arrays
        valid = ~np.isnan(arrays["close"])
        n = len(sessions)
        any_valid = valid.any(axis=0)
        self.first_valid = np.where(any_valid, valid.argmax(axis=0), n)
        self.last_valid = np.where(any_valid, n - 1 - valid[::-1].argmax(axis=0), -1)
        self.last_data_idx = int(self.last_valid.max()) if any_valid.any() else -1
        self._pos = {d: i for i, d in enumerate(sessions)}

    @classmethod
    def build(cls, frames: dict[SecurityId, pd.DataFrame | None], sessions: pd.DatetimeIndex):
        sids = list(frames)
        arrays = {}
        for f in BAR_COLUMNS:
            arr = np.full((len(sessions), len(sids)), np.nan)
            for j, sid in enumerate(sids):
                df = frames[sid]
                if df is not None and not df.empty:
                    arr[:, j] = df[f].reindex(sessions).to_numpy(dtype=float)
            arrays[f] = arr
        return cls(sessions, sids, arrays)

    def has(self, sid: SecurityId) -> bool:
        return sid in self._col

    def col(self, sid: SecurityId) -> int:
        return self._col[sid]

    def index_of(self, date: pd.Timestamp) -> int | None:
        return self._pos.get(pd.Timestamp(date))

    def value(self, field: str, i: int, sid: SecurityId) -> float:
        if sid not in self._col:
            return float("nan")
        return float(self.arrays[field][i, self._col[sid]])

    def last_close_at_or_before(self, i: int, sid: SecurityId) -> float:
        """Most recent non-NaN close at or before row i (NaN if none)."""
        if sid not in self._col or i < 0:
            return float("nan")
        col = self.arrays["close"][: i + 1, self._col[sid]]
        valid = np.flatnonzero(~np.isnan(col))
        return float(col[valid[-1]]) if len(valid) else float("nan")


class PointInTimeView:
    """The only path from strategy code to price data.

    ``cursor`` is the index of the current session t. Rows ``< cursor`` (bars
    through the close of t-1) are visible; any request for row ``>= cursor``
    raises ``LookaheadError``.
    """

    def __init__(self, panel: PricePanel):
        self._panel = panel
        self.cursor = 0

    @property
    def today(self) -> pd.Timestamp:
        return self._panel.sessions[self.cursor]

    def _check_row(self, end_row: int) -> None:
        if end_row >= self.cursor:
            raise LookaheadError(
                f"data for {self._panel.sessions[min(end_row, len(self._panel.sessions) - 1)].date()} "
                f"is not visible on {self.today.date()}: strategies see bars only through "
                f"the previous session's close"
            )

    def window(self, field: str, n: int, sids: list[SecurityId], tickers: list[str]) -> pd.DataFrame:
        """Last n visible rows for the given securities, as a fresh DataFrame."""
        if field not in STRATEGY_FIELDS:
            raise ValueError(f"unknown field {field!r}; use one of {STRATEGY_FIELDS}")
        if not isinstance(n, (int, np.integer)) or n < 1:
            raise ValueError(f"n must be a positive integer, got {n!r}")
        end = self.cursor  # exclusive
        start = max(0, end - int(n))
        if end > start:
            self._check_row(end - 1)
        arr = self._panel.arrays[field]
        data = np.full((end - start, len(sids)), np.nan)
        for k, sid in enumerate(sids):
            if self._panel.has(sid):
                data[:, k] = arr[start:end, self._panel.col(sid)]
        return pd.DataFrame(data, index=self._panel.sessions[start:end].copy(), columns=list(tickers))

    def bar(self, sid: SecurityId, date) -> Bar | None:
        """The completed bar on ``date``; None if the security had no bar that session."""
        date = pd.Timestamp(date).normalize()
        if date >= self.today:
            raise LookaheadError(
                f"bar for {date.date()} is not visible on {self.today.date()}: "
                f"strategies see bars only through the previous session's close"
            )
        i = self._panel.index_of(date)
        if i is None or not self._panel.has(sid):
            return None
        self._check_row(i)
        o, h, l, c, v = (self._panel.value(f, i, sid) for f in ("open", "high", "low", "close", "volume"))
        if np.isnan(c):
            return None
        return Bar(date, o, h, l, c, v)

    def last_close(self, sid: SecurityId) -> float:
        """Most recent visible close (NaN if none)."""
        if self.cursor == 0:
            return float("nan")
        self._check_row(self.cursor - 1)
        return self._panel.last_close_at_or_before(self.cursor - 1, sid)

    def last_raw_close(self, sid: SecurityId) -> float:
        """Most recent visible unadjusted close (NaN if none), for market cap."""
        if self.cursor == 0 or not self._panel.has(sid):
            return float("nan")
        self._check_row(self.cursor - 1)
        col = self._panel.arrays["close_raw"][: self.cursor, self._panel.col(sid)]
        valid = np.flatnonzero(~np.isnan(col))
        return float(col[valid[-1]]) if len(valid) else float("nan")

"""Price provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from ..ids import SecurityId

# Columns every provider returns. OHLC are split- and dividend-adjusted;
# volume is split-adjusted. close_raw is the unadjusted close (for market cap
# in M4). dollar_volume is raw close x raw volume.
BAR_COLUMNS = ("open", "high", "low", "close", "volume", "close_raw", "dollar_volume")


class PriceProvider(ABC):
    """Source of daily bars, keyed by security id."""

    name: str = "provider"

    @abstractmethod
    def get_bars(self, security_id: SecurityId) -> pd.DataFrame | None:
        """Full daily history for one security, or None if it has no data.

        The frame is indexed by tz-naive session dates (ascending, unique) and
        has the columns in ``BAR_COLUMNS``.
        """

    def manifest(self, security_ids: list[SecurityId]) -> dict:
        """Describe the data behind a run, for data_manifest.json."""
        return {"provider": self.name}


def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Fill optional columns and enforce index/column conventions."""
    df = df.copy()
    df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if "close_raw" not in df:
        df["close_raw"] = df["close"]
    if "volume" not in df:
        df["volume"] = 0.0
    if "dollar_volume" not in df:
        df["dollar_volume"] = df["close_raw"] * df["volume"]
    return df[list(BAR_COLUMNS)].astype(float)

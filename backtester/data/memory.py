"""In-memory price provider, for tests and fixtures."""

from __future__ import annotations

import pandas as pd

from ..ids import SecurityId, SecurityMaster
from .provider import PriceProvider, normalize_bars


class InMemoryProvider(PriceProvider):
    """Serves bars from a dict of DataFrames keyed by ticker."""

    name = "in_memory"

    def __init__(self, frames: dict[str, pd.DataFrame]):
        master = SecurityMaster()
        self._frames = {master.to_id(t): normalize_bars(df) for t, df in frames.items()}

    def get_bars(self, security_id: SecurityId) -> pd.DataFrame | None:
        df = self._frames.get(security_id)
        return None if df is None else df.copy()

    def manifest(self, security_ids: list[SecurityId]) -> dict:
        tickers = {}
        for sid in security_ids:
            df = self._frames.get(sid)
            if df is None or df.empty:
                tickers[str(sid)] = {"missing": True}
            else:
                tickers[str(sid)] = {
                    "missing": False,
                    "first": df.index[0].date().isoformat(),
                    "last": df.index[-1].date().isoformat(),
                    "rows": int(len(df)),
                }
        return {
            "provider": self.name,
            "tickers": tickers,
            "missing_tickers": sorted(t for t, v in tickers.items() if v["missing"]),
        }

"""yfinance price provider with a local Parquet cache.

Cache layout: ``<cache_dir>/prices/<TICKER>.parquet`` plus ``<TICKER>.json``
metadata. The cache stores Yahoo's columns as fetched: split-adjusted OHLC,
the dividend-and-split-adjusted close, split-adjusted volume, dividends and
splits. Adjusted bars are derived on read.

Refresh is incremental: fetch the last few sessions and append. If Yahoo has
re-adjusted history since the last fetch (a new dividend or split, or
overlapping rows that changed), the whole history is fetched again so the
adjustment stays consistent.

yfinance is unofficial and rate-limited, so each ticker is fetched at most once
per ``min_refresh_hours``.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..calendar import nyse
from ..errors import DataError
from ..ids import SecurityId
from .provider import BAR_COLUMNS, PriceProvider

ET = ZoneInfo("America/New_York")
RAW_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume", "dividends", "splits"]
_YF_RENAME = {
    "Open": "open", "High": "high", "Low": "low", "Close": "close", "Adj Close": "adj_close",
    "Volume": "volume", "Dividends": "dividends", "Stock Splits": "splits",
}
OVERLAP_DAYS = 10
BAR_FINAL_HOUR_ET = 18  # treat a session's bar as final after 18:00 ET
# What a Yahoo symbol can look like. Anything else (a ticker typed into a filing as "NYSE/TRN")
# has no data, and must never become a cache file path.
SYMBOL = re.compile(r"\^?[A-Z0-9][A-Z0-9.\-=]*")


def last_completed_session(now: datetime | None = None) -> pd.Timestamp:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    cal = nyse()
    today = pd.Timestamp(now.date())
    if cal.is_session(today) and now.hour >= BAR_FINAL_HOUR_ET:
        return today
    return cal.date_to_session(today, direction="previous") if not cal.is_session(today) \
        else cal.previous_session(today)


class YFinanceProvider(PriceProvider):
    name = "yfinance"

    def __init__(self, cache_dir: str | Path = Path("data") / "cache", refresh: bool = True,
                 min_refresh_hours: float = 12.0, retries: int = 3):
        self.dir = Path(cache_dir) / "prices"
        self.refresh = refresh
        self.min_refresh = timedelta(hours=min_refresh_hours)
        self.retries = retries

    # --- public --------------------------------------------------------
    def get_bars(self, security_id: SecurityId) -> pd.DataFrame | None:
        if not SYMBOL.fullmatch(str(security_id)):
            return None
        raw = self._ensure(security_id)
        if raw is None or raw.empty:
            return None
        return to_adjusted_bars(raw)

    def manifest(self, security_ids: list[SecurityId]) -> dict:
        import yfinance

        tickers = {}
        for sid in security_ids:
            meta = self._read_meta(sid)
            tickers[str(sid)] = {
                "missing": bool(meta.get("missing", True)),
                "cache_file": str(self._parquet(sid)),
                "first": meta.get("first"),
                "last": meta.get("last"),
                "rows": meta.get("rows"),
                "fetched_at": meta.get("fetched_at"),
                "full_fetch_at": meta.get("full_fetch_at"),
            }
        return {
            "provider": self.name,
            "source": f"yfinance {yfinance.__version__}",
            "adjustment": "OHLC scaled by Yahoo Adj Close / Close (splits and dividends)",
            "tickers": tickers,
            "missing_tickers": sorted(t for t, v in tickers.items() if v["missing"]),
        }

    # --- cache ---------------------------------------------------------
    def _parquet(self, sid) -> Path:
        return self.dir / f"{sid}.parquet"

    def _meta_path(self, sid) -> Path:
        return self.dir / f"{sid}.json"

    def _read_meta(self, sid) -> dict:
        p = self._meta_path(sid)
        return json.loads(p.read_text()) if p.exists() else {}

    def _save(self, sid, df: pd.DataFrame | None, full: bool) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta = self._read_meta(sid)
        meta["fetched_at"] = now
        if full:
            meta["full_fetch_at"] = now
        if df is None or df.empty:
            meta.update({"missing": True, "first": None, "last": None, "rows": 0})
        else:
            df.to_parquet(self._parquet(sid))
            meta.update({
                "missing": False,
                "first": df.index[0].date().isoformat(),
                "last": df.index[-1].date().isoformat(),
                "rows": int(len(df)),
            })
        self._meta_path(sid).write_text(json.dumps(meta, indent=2) + "\n")

    def _load_cached(self, sid) -> pd.DataFrame | None:
        p = self._parquet(sid)
        return pd.read_parquet(p) if p.exists() else None

    def _recently_fetched(self, meta: dict) -> bool:
        ts = meta.get("fetched_at")
        if not ts:
            return False
        return datetime.now(timezone.utc) - datetime.fromisoformat(ts) < self.min_refresh

    def _ensure(self, sid) -> pd.DataFrame | None:
        meta = self._read_meta(sid)
        cached = self._load_cached(sid)
        if cached is None:
            if meta.get("missing") and (not self.refresh or self._recently_fetched(meta)):
                return None
            if not self.refresh and not meta:
                raise DataError(f"{sid}: not in the price cache and refresh is off")
            df = self._fetch(sid, start=None)
            self._save(sid, df, full=True)
            return df
        if not self.refresh or self._recently_fetched(meta):
            return cached
        last = cached.index[-1]
        if last >= last_completed_session():
            return cached
        new = self._fetch(sid, start=last - timedelta(days=OVERLAP_DAYS))
        if new is None or new.empty:
            self._save(sid, cached, full=False)
            return cached
        if self._readjusted(cached, new, last):
            df = self._fetch(sid, start=None)
            self._save(sid, df, full=True)
            return df
        merged = pd.concat([cached, new[new.index > last]])
        self._save(sid, merged, full=False)
        return merged

    @staticmethod
    def _readjusted(cached: pd.DataFrame, new: pd.DataFrame, last: pd.Timestamp) -> bool:
        overlap = cached.index.intersection(new.index)
        if len(overlap) == 0:
            return True
        a = cached.loc[overlap, ["close", "adj_close"]].to_numpy()
        b = new.loc[overlap, ["close", "adj_close"]].to_numpy()
        if not np.allclose(a, b, rtol=1e-6, atol=0):
            return True
        later = new[new.index > last]
        return bool((later[["dividends", "splits"]].fillna(0) != 0).to_numpy().any())

    # --- network -------------------------------------------------------
    def _fetch(self, sid, start) -> pd.DataFrame | None:
        import yfinance as yf

        last_err = None
        for attempt in range(self.retries):
            try:
                t = yf.Ticker(str(sid))
                if start is None:
                    df = t.history(period="max", auto_adjust=False, actions=True)
                else:
                    df = t.history(start=pd.Timestamp(start).date().isoformat(),
                                   auto_adjust=False, actions=True)
                return self._normalize(df)
            except Exception as exc:  # network / rate limit
                last_err = exc
                time.sleep(2 ** attempt)
        raise DataError(f"{sid}: yfinance fetch failed: {last_err!r}")

    @staticmethod
    def _normalize(df: pd.DataFrame | None) -> pd.DataFrame | None:
        if df is None or df.empty:
            return None
        df = df.rename(columns=_YF_RENAME)
        if "adj_close" not in df:
            raise DataError("yfinance returned no 'Adj Close' column")
        for col in ("dividends", "splits"):
            if col not in df:
                df[col] = 0.0
        df = df[RAW_COLUMNS].astype(float)
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_convert(ET).tz_localize(None)
        df.index = idx.normalize()
        df.index.name = "date"
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df[df.index <= last_completed_session()]
        df = df.dropna(subset=["open", "high", "low", "close", "adj_close"])
        return df


def to_adjusted_bars(raw: pd.DataFrame) -> pd.DataFrame:
    """Derive engine bars from Yahoo's cached columns."""
    factor = raw["adj_close"] / raw["close"]
    splits = raw["splits"].fillna(0).replace(0, 1.0)
    # Product of splits strictly after each date: converts split-adjusted to raw.
    after = splits[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    out = pd.DataFrame(index=raw.index)
    for col in ("open", "high", "low", "close"):
        out[col] = raw[col] * factor
    out["volume"] = raw["volume"]
    out["close_raw"] = raw["close"] * after
    out["dollar_volume"] = raw["close"] * raw["volume"]
    return out[list(BAR_COLUMNS)]

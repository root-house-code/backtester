"""Feed sources: file feeds (a folder with feed.yaml), plug-ins and built-ins."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..errors import DataError
from .base import DEFAULT_TIME, ET, KINDS, REFERENCE, Feed

FEED_YAML = "feed.yaml"
DATA_SUFFIXES = (".csv", ".parquet")
YAML_KEYS = {"kind", "description", "ticker_column", "time_column", "entity_column", "id_column",
             "timezone", "default_time", "columns", "files"}


class FileFeed(Feed):
    """A feed defined by dropping CSV or Parquet files into ``data/feeds/<name>/``
    next to a ``feed.yaml``::

        kind: event                  # or reference
        description: Insider purchases from my screener
        ticker_column: symbol        # default: ticker
        time_column: filed_at        # default: available_at
        timezone: America/New_York   # for timestamps without a UTC offset
        default_time: "18:00"        # for date-only timestamps (default 18:00)
        entity_column: cik           # optional
        id_column: filing_id         # optional; otherwise ids are derived from each record
        columns:                     # optional; default is every other column
          value: float
          title: str
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.name = self.folder.name
        path = self.folder / FEED_YAML
        try:
            import yaml
            spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise DataError(f"feed {self.name}: cannot read {path}: {exc}") from None
        if not isinstance(spec, dict):
            raise DataError(f"feed {self.name}: {FEED_YAML} must be a mapping of settings")
        unknown = set(spec) - YAML_KEYS
        if unknown:
            raise DataError(f"feed {self.name}: unknown {FEED_YAML} keys {sorted(unknown)} "
                            f"(allowed: {sorted(YAML_KEYS)})")
        self.spec = spec
        self.kind = spec.get("kind")
        if self.kind not in KINDS:
            raise DataError(f"feed {self.name}: {FEED_YAML} must set kind to one of {list(KINDS)}")
        self.description = spec.get("description", "")
        self.columns = dict(spec.get("columns") or {})
        self.timezone = spec.get("timezone")
        self.default_time = str(spec.get("default_time", DEFAULT_TIME))
        self.ticker_column = spec.get("ticker_column", "ticker")
        self.time_column = spec.get("time_column", "available_at")
        self.entity_column = spec.get("entity_column")
        self.id_column = spec.get("id_column")

    def data_files(self) -> list[Path]:
        patterns = self.spec.get("files")
        if patterns:
            files = sorted({p for pat in patterns for p in self.folder.glob(pat)})
        else:
            files = sorted(p for p in self.folder.iterdir() if p.suffix.lower() in DATA_SUFFIXES)
        return [p for p in files if p.is_file()]

    def fingerprint(self) -> str:
        h = hashlib.sha256((self.folder / FEED_YAML).read_bytes())
        for p in self.data_files():
            st = p.stat()
            h.update(f"{p.name}|{st.st_size}|{st.st_mtime_ns}".encode())
        return h.hexdigest()

    def fetch(self, start=None, end=None, tickers=None) -> pd.DataFrame:
        files = self.data_files()
        if not files:
            raise DataError(f"feed {self.name}: no .csv or .parquet files in {self.folder}")
        frames = []
        for p in files:
            if p.suffix.lower() == ".parquet":
                frames.append(pd.read_parquet(p))
            else:
                frames.append(pd.read_csv(p, dtype={self.time_column: str}, keep_default_na=True))
        return pd.concat(frames, ignore_index=True)

    def normalize_kwargs(self) -> dict:
        return {"ticker_column": self.ticker_column, "time_column": self.time_column,
                "entity_column": self.entity_column, "id_column": self.id_column}

    def source(self) -> dict:
        return {
            "type": "file",
            "folder": str(self.folder),
            "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.data_files()},
        }


# --- built-ins ---------------------------------------------------------------------

def _fetch_shares_yahoo(ticker: str) -> pd.Series:
    import warnings

    import yfinance as yf
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = yf.Ticker(ticker).get_shares_full(start="1990-01-01")
    if s is None or len(s) == 0:
        return pd.Series(dtype=float)
    return s


class SharesOutstanding(Feed):
    """Built-in: shares outstanding per ticker, from Yahoo Finance.

    Each value counts as known from its report date, after the close (so it can
    first be used the next session). Shares are reported unadjusted.
    """

    name = "shares_outstanding"
    kind = REFERENCE
    description = "Shares outstanding (Yahoo Finance), as reported"
    columns = {"shares_outstanding": float}
    timezone = ET
    min_refresh = timedelta(hours=12)

    #: Replaced in tests; returns a Series of share counts indexed by date.
    fetcher = staticmethod(_fetch_shares_yahoo)

    def __init__(self, cache_dir: Path, refresh: bool = True):
        self.dir = Path(cache_dir) / "shares"
        self.refresh = refresh
        self.cacheable = False  # cached per ticker below
        self.fetched: dict[str, str] = {}

    def _load_ticker(self, ticker: str) -> pd.DataFrame:
        path, meta_path = self.dir / f"{ticker}.parquet", self.dir / f"{ticker}.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        fresh = meta.get("fetched_at") and (
            datetime.now(timezone.utc) - datetime.fromisoformat(meta["fetched_at"]) < self.min_refresh)
        if path.exists() and (fresh or not self.refresh):
            self.fetched[ticker] = meta.get("fetched_at")
            return pd.read_parquet(path)
        if not self.refresh:
            return pd.DataFrame(columns=["date", "shares_outstanding"])
        try:
            s = self.fetcher(ticker)
        except Exception:
            s = pd.Series(dtype=float)
        idx = pd.DatetimeIndex(s.index)
        if idx.tz is not None:
            idx = idx.tz_convert(ET).tz_localize(None)
        df = pd.DataFrame({"date": idx.normalize(), "shares_outstanding": pd.to_numeric(s.values, errors="coerce")})
        df = df.dropna().drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta_path.write_text(json.dumps({"fetched_at": now, "rows": len(df)}) + "\n")
        self.fetched[ticker] = now
        return df

    def fetch(self, start, end, tickers=None) -> pd.DataFrame:
        rows = []
        for t in sorted(set(tickers or [])):
            df = self._load_ticker(t)
            if len(df):
                rows.append(pd.DataFrame({
                    "ticker": t,
                    "available_at": df["date"].dt.strftime("%Y-%m-%d"),  # date-only -> default time
                    "shares_outstanding": df["shares_outstanding"].astype(float),
                }))
        if not rows:
            return pd.DataFrame(columns=["ticker", "available_at", "shares_outstanding"])
        return pd.concat(rows, ignore_index=True)

    def source(self) -> dict:
        return {"type": "builtin", "provider": "yfinance get_shares_full", "fetched_at": dict(sorted(self.fetched.items()))}


class MarketCap(Feed):
    """Built-in: market cap = the last raw (unadjusted) close x the latest
    shares outstanding known at the time. Derived when read; requires prices."""

    name = "market_cap"
    kind = REFERENCE
    description = "Market cap: raw close x shares outstanding (derived)"
    columns = {"market_cap": float}
    derived_from = "shares_outstanding"

    def fetch(self, start, end, tickers=None):
        raise DataError("market_cap is derived from shares_outstanding and prices; it has no records of its own")


BUILTINS = {"shares_outstanding": SharesOutstanding, "market_cap": MarketCap}


# --- plug-ins ----------------------------------------------------------------------

def load_plugin_file(path: Path) -> type[Feed]:
    path = Path(path).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    module_name = f"_bt_feed_{path.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise DataError(f"feed plug-in {path.name}: failed to import: {exc!r}") from exc
    classes = [o for o in vars(module).values() if inspect.isclass(o) and issubclass(o, Feed)
               and o.__module__ == module_name]
    if len(classes) != 1:
        raise DataError(f"feed plug-in {path.name}: expected exactly one Feed subclass, found {len(classes)}")
    cls = classes[0]
    if not cls.name or cls.kind not in KINDS:
        raise DataError(f"feed plug-in {path.name}: set `name` and `kind` (one of {list(KINDS)})")
    return cls

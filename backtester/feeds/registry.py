"""Finds feeds and loads them, with a Parquet cache of normalized records."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..errors import DataError
from .base import Feed, FeedData, FeedValidation, normalize_records
from .sources import BUILTINS, FEED_YAML, FileFeed, MarketCap, SharesOutstanding, load_plugin_file

DEFAULT_FEEDS_DIR = Path("data") / "feeds"
DEFAULT_PLUGINS_DIR = Path("feeds")
DEFAULT_CACHE_DIR = Path("data") / "cache"
PLUGIN_REFRESH = timedelta(hours=12)
RESERVED_NAMES = {"prices"}


class FeedRegistry:
    """Every feed a strategy can require, by name.

    Args:
        feeds_dir: Folder of file feeds (``<name>/feed.yaml`` plus data files).
        plugins_dir: Folder of plug-in ``.py`` files, one ``Feed`` subclass each.
        cache_dir: Root of the local cache (feeds go under ``feeds/``).
        refresh: Whether plug-in and built-in feeds may fetch new data.
        extra: Feeds supplied directly (fixtures and tests), by name. These
            replace discovered feeds of the same name.
    """

    def __init__(self, feeds_dir=DEFAULT_FEEDS_DIR, plugins_dir=DEFAULT_PLUGINS_DIR,
                 cache_dir=DEFAULT_CACHE_DIR, refresh: bool = True, extra: dict | None = None):
        self.feeds_dir = Path(feeds_dir) if feeds_dir is not None else None
        self.plugins_dir = Path(plugins_dir) if plugins_dir is not None else None
        self.cache_dir = Path(cache_dir)
        self.refresh = refresh
        self.extra = dict(extra or {})
        self._found: dict[str, tuple[str, object]] | None = None
        self._instances: dict[str, Feed] = {}

    # --- discovery ---------------------------------------------------------------
    def _discover(self) -> dict[str, tuple[str, object]]:
        if self._found is not None:
            return self._found
        found: dict[str, tuple[str, object]] = {name: ("builtin", cls) for name, cls in BUILTINS.items()}

        def add(name, origin, obj):
            if name in RESERVED_NAMES:
                raise DataError(f"a feed cannot be named {name!r}")
            if name in found and found[name][0] != "builtin":
                raise DataError(f"two feeds are named {name!r} ({found[name][0]} and {origin})")
            if name in found:
                raise DataError(f"feed name {name!r} is taken by a built-in feed")
            found[name] = (origin, obj)

        if self.feeds_dir is not None and self.feeds_dir.is_dir():
            for folder in sorted(p for p in self.feeds_dir.iterdir() if (p / FEED_YAML).is_file()):
                add(folder.name, "file", folder)
        if self.plugins_dir is not None and self.plugins_dir.is_dir():
            for path in sorted(self.plugins_dir.glob("*.py")):
                if path.name.startswith(("_", "test_")):
                    continue
                cls = load_plugin_file(path)
                add(cls.name, "plugin", cls)
        for name, feed in self.extra.items():
            found[name] = ("given", feed)
        self._found = found
        return found

    def names(self) -> list[str]:
        return sorted(self._discover())

    def origin(self, name: str) -> str:
        return self._discover()[name][0]

    def get(self, name: str) -> Feed:
        """The feed object for ``name``; DataError if there is none."""
        if name in self._instances:
            return self._instances[name]
        found = self._discover()
        if name not in found:
            known = ", ".join(self.names()) or "none"
            raise DataError(f"unknown feed {name!r} (known feeds: {known}). Add data/feeds/{name}/feed.yaml "
                            f"with data files, or a plug-in in feeds/.")
        origin, obj = found[name]
        if origin == "file":
            feed = FileFeed(obj)
        elif origin == "plugin":
            feed = obj()
        elif origin == "given":
            feed = obj
        elif obj is SharesOutstanding:
            feed = SharesOutstanding(self.cache_dir, refresh=self.refresh)
        else:
            feed = obj()
        self._instances[name] = feed
        return feed

    def kind(self, name: str) -> str:
        return self.get(name).kind

    # --- loading -----------------------------------------------------------------
    def _cache_paths(self, name: str):
        d = self.cache_dir / "feeds"
        return d / f"{name}.parquet", d / f"{name}.json"

    def _normalize(self, feed: Feed, raw: pd.DataFrame, known_tickers=None) -> FeedData:
        kwargs = feed.normalize_kwargs() if isinstance(feed, FileFeed) else {}
        data = normalize_records(raw, feed.name, feed.kind, columns=feed.columns or None,
                                 timezone=feed.timezone, default_time=feed.default_time,
                                 known_tickers=known_tickers, **kwargs)
        data.source = feed.source()
        return data

    def _save(self, name: str, data: FeedData, meta: dict) -> None:
        path, meta_path = self._cache_paths(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        data.records.to_parquet(path, index=False)
        meta = {**meta, "kind": data.kind, "value_columns": data.value_columns, "source": data.source,
                "validation": {"errors": data.validation.errors, "warnings": data.validation.warnings,
                               "stats": data.validation.stats}}
        meta_path.write_text(json.dumps(meta, indent=2, default=str) + "\n")

    def _cached(self, name: str):
        path, meta_path = self._cache_paths(name)
        if not (path.is_file() and meta_path.is_file()):
            return None, None
        meta = json.loads(meta_path.read_text())
        v = meta.get("validation", {})
        validation = FeedValidation(name, meta["kind"], v.get("errors", []), v.get("warnings", []), v.get("stats", {}))
        records = pd.read_parquet(path)
        return FeedData(name, meta["kind"], records, meta["value_columns"], validation, meta.get("source", {})), meta

    def load(self, name: str, start=None, end=None, tickers=None, check: bool = True) -> FeedData:
        """Load a feed's normalized records. With ``check``, a feed that fails
        validation raises DataError with the validation report."""
        feed = self.get(name)
        if isinstance(feed, MarketCap):
            data = FeedData(name, feed.kind, pd.DataFrame(columns=["id", "ticker", "available_at", "entity_id"]),
                            ["market_cap"], FeedValidation(name, feed.kind),
                            {"type": "derived", "from": ["shares_outstanding", "prices"],
                             "formula": "raw close x shares outstanding"})
        elif isinstance(feed, FileFeed):
            fingerprint = feed.fingerprint()
            data, meta = self._cached(name)
            if data is None or meta.get("fingerprint") != fingerprint:
                data = self._normalize(feed, feed.fetch())
                self._save(name, data, {"fingerprint": fingerprint})
        elif feed.cacheable:
            data, meta = self._cached(name)
            end_s = None if end is None else pd.Timestamp(end).date().isoformat()
            # A fetch with no end (``backtest feeds refresh``) ran up to that moment, so it covers any end.
            covers = data is not None and (end_s is None or meta.get("end") is None or meta["end"] >= end_s)
            fresh = data is not None and datetime.now(timezone.utc) - datetime.fromisoformat(meta["fetched_at"]) < PLUGIN_REFRESH
            if not covers or (self.refresh and not fresh):
                if not self.refresh:
                    raise DataError(f"feed {name!r} has no cached data through {end_s}; refresh it with "
                                    f"`backtest feeds refresh {name}`")
                try:
                    raw = feed.fetch(None, None if end is None else pd.Timestamp(end), tickers)
                except DataError:
                    raise
                except Exception as exc:
                    raise DataError(f"feed {name!r}: fetch failed: {exc!r}") from exc
                data = self._normalize(feed, raw)
                self._save(name, data, {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                        "end": end_s})
        else:
            data = self._normalize(feed, feed.fetch(None, end, tickers))
        if check and not data.validation.ok:
            raise DataError(data.validation.text().rstrip())
        return data

    def validate(self, name: str, price_cache_dir: Path | None = None) -> FeedValidation:
        """Re-read a feed from its source and check it (for the CLI and the UI)."""
        feed = self.get(name)
        known = None
        prices = Path(price_cache_dir or self.cache_dir) / "prices"
        if prices.is_dir():
            known = {p.stem for p in prices.glob("*.parquet")}
        if isinstance(feed, MarketCap):
            return FeedValidation(name, feed.kind, stats={"derived from": "shares_outstanding x raw close"})
        if isinstance(feed, SharesOutstanding):
            return FeedValidation(name, feed.kind, stats={"fetched": "per ticker, when a run needs it"})
        if isinstance(feed, FileFeed):
            try:
                raw = feed.fetch()
            except DataError as exc:
                return FeedValidation(name, feed.kind, errors=[str(exc)])
            return self._normalize(feed, raw, known).validation
        data, _ = self._cached(name)
        if data is None:
            return FeedValidation(name, feed.kind, errors=["no cached data yet; run `backtest feeds refresh "
                                                           f"{name}` to fetch it"])
        return normalize_records(data.records, name, data.kind, columns={c: None for c in data.value_columns},
                                 timezone="UTC", known_tickers=known).validation

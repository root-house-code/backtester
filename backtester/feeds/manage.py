"""Adding file feeds and summarizing local data, for the Data screen and the CLI."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pandas as pd
import yaml

from ..errors import DataError
from .base import DEFAULT_TIME, ET, FeedValidation, normalize_records
from .sources import BUILTINS, FEED_YAML

FEED_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def read_upload(filename: str, data: bytes) -> pd.DataFrame:
    """Parse an uploaded CSV or Parquet file."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=True)
    if suffix == ".parquet":
        return pd.read_parquet(io.BytesIO(data))
    raise DataError(f"{filename}: upload a .csv or .parquet file")


def add_file_feed(feeds_dir, name: str, filename: str, data: bytes, kind: str, ticker_column: str,
                  time_column: str, timezone: str | None = ET, default_time: str = DEFAULT_TIME,
                  description: str = "", known_tickers: set | None = None) -> tuple[FeedValidation, Path | None]:
    """Check an uploaded file as a feed and, if it passes, save it as
    ``feeds_dir/<name>/`` with a ``feed.yaml``. Nothing is written when the
    checks fail. Returns (validation, folder or None)."""
    name = name.strip()
    if not FEED_NAME.match(name):
        return FeedValidation(name, kind, errors=["feed name must be lowercase letters, digits and _, "
                                                 "starting with a letter"]), None
    folder = Path(feeds_dir) / name
    if folder.exists() or name in BUILTINS or name == "prices":
        return FeedValidation(name, kind, errors=[f"a feed named {name!r} already exists"]), None
    try:
        raw = read_upload(filename, data)
    except Exception as exc:
        return FeedValidation(name, kind, errors=[f"cannot read {filename}: {exc}"]), None
    checked = normalize_records(raw, name, kind, ticker_column=ticker_column, time_column=time_column,
                                timezone=timezone or None, default_time=default_time, known_tickers=known_tickers)
    if not checked.validation.ok:
        return checked.validation, None
    spec = {"kind": kind}
    if description:
        spec["description"] = description
    if ticker_column != "ticker":
        spec["ticker_column"] = ticker_column
    if time_column != "available_at":
        spec["time_column"] = time_column
    if timezone:
        spec["timezone"] = timezone
    if default_time != DEFAULT_TIME:
        spec["default_time"] = default_time
    folder.mkdir(parents=True)
    (folder / Path(filename).name).write_bytes(data)
    (folder / FEED_YAML).write_text("# Added from the Data page.\n" + yaml.safe_dump(spec, sort_keys=False),
                                    encoding="utf-8")
    return checked.validation, folder


def price_cache_coverage(cache_dir) -> pd.DataFrame:
    """One row per ticker in the price cache: date range, rows, last fetch, missing flag."""
    prices = Path(cache_dir) / "prices"
    rows = []
    for meta_path in sorted(prices.glob("*.json")) if prices.is_dir() else []:
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({
            "ticker": meta_path.stem,
            "first": meta.get("first"),
            "last": meta.get("last"),
            "rows": meta.get("rows"),
            "fetched_at": meta.get("fetched_at"),
            "missing": bool(meta.get("missing", False)),
        })
    return pd.DataFrame(rows, columns=["ticker", "first", "last", "rows", "fetched_at", "missing"])


def feed_overview(registry) -> pd.DataFrame:
    """One row per feed: kind, origin, record counts, date range and check status."""
    rows = []
    for name in registry.names():
        feed = registry.get(name)
        v = registry.validate(name)
        cached_meta = registry.cache_dir / "feeds" / f"{name}.json"
        fetched = None
        if cached_meta.is_file():
            try:
                fetched = json.loads(cached_meta.read_text()).get("fetched_at")
            except (OSError, json.JSONDecodeError):
                pass
        rows.append({
            "feed": name,
            "kind": feed.kind,
            "origin": registry.origin(name),
            "status": "OK" if v.ok else "Failed",
            "records": v.stats.get("records"),
            "tickers": v.stats.get("tickers"),
            "first": v.stats.get("first available_at"),
            "last": v.stats.get("last available_at"),
            "warnings": len(v.warnings),
            "fetched_at": fetched,
            "description": feed.description,
            "report": v.text(),
        })
    return pd.DataFrame(rows)

"""The feed contract, record normalization and feed validation.

Every feed record has a ``ticker``, an ``available_at`` timestamp (when it
became knowable), an optional ``entity_id``, and any other columns the feed
declares. After normalization every record also has a stable ``id`` and
``available_at`` is a UTC timestamp.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import time

import numpy as np
import pandas as pd

from ..errors import DataError

EVENT, REFERENCE = "event", "reference"
KINDS = (EVENT, REFERENCE)
ET = "America/New_York"
DEFAULT_TIME = "18:00"  # date-only records: after the close, so never actionable the same day
RESERVED = ("id", "ticker", "available_at", "entity_id")
COLUMN_TYPES = {"float": float, "int": int, "str": str, "bool": bool}

_DATE_ONLY = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*$")
_HAS_OFFSET = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})\s*$")


class Feed:
    """Base class for Python plug-in feeds: data that must be fetched or derived.

    Put a subclass in a ``.py`` file in the ``feeds/`` folder (one per file).

    Class attributes:
        name: Feed name, used in a strategy's ``requires``.
        kind: ``"event"`` (records that appear at a moment, read with
            ``ctx.events``) or ``"reference"`` (a per-ticker value that changes
            over time, read with ``ctx.ref``).
        description: One line on what the feed holds.
        columns: Value columns and their types, e.g. ``{"value": float}``.
        timezone: Time zone for ``available_at`` values without an offset.
            Leave as None if every timestamp carries its own offset.
        default_time: Time of day given to date-only ``available_at`` values,
            in ``timezone`` (or US/Eastern). The default is after the close.

    Example:
        ``class Ratings(Feed): name = "ratings"; kind = "event"; ...``
    """

    name: str = ""
    kind: str = EVENT
    description: str = ""
    columns: dict = {}
    timezone: str | None = None
    default_time: str = DEFAULT_TIME
    #: Whether fetched records are cached under data/cache/feeds/.
    cacheable: bool = True

    def fetch(self, start: pd.Timestamp | None, end: pd.Timestamp | None, tickers: list[str] | None = None) -> pd.DataFrame:
        """Return records with ``available_at`` between ``start`` and ``end``.

        The frame must have ``ticker`` and ``available_at`` columns plus the
        declared ``columns``; ``entity_id`` and ``id`` are optional. ``tickers``
        is a hint (the run's tickers, when known); returning more is fine.
        """
        raise NotImplementedError

    def source(self) -> dict:
        """Describes where the data came from, for the run's data manifest."""
        return {"type": "plugin", "class": type(self).__name__}


class StaticFeed(Feed):
    """A feed held in memory, for fixtures and tests."""

    cacheable = False

    def __init__(self, name: str, kind: str, records, columns: dict | None = None,
                 timezone: str | None = ET, default_time: str = DEFAULT_TIME):
        self.name, self.kind = name, kind
        self.records = pd.DataFrame(records) if not isinstance(records, pd.DataFrame) else records.copy()
        extra = [c for c in self.records.columns if c not in RESERVED]
        self.columns = columns if columns is not None else {c: None for c in extra}
        self.timezone, self.default_time = timezone, default_time
        self.description = f"in-memory {kind} feed"

    def fetch(self, start, end, tickers=None) -> pd.DataFrame:
        return self.records.copy()

    def source(self) -> dict:
        return {"type": "in_memory"}


@dataclass
class FeedValidation:
    """Result of checking a feed's records."""

    name: str
    kind: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        lines = [f"Feed {self.name} ({self.kind}): {'OK' if self.ok else 'FAILED'}"]
        for k, v in self.stats.items():
            lines.append(f"  {k}: {v}")
        lines += [f"  error: {e}" for e in self.errors]
        lines += [f"  warning: {w}" for w in self.warnings]
        return "\n".join(lines) + "\n"


@dataclass
class FeedData:
    """A loaded, normalized feed, ready for the engine."""

    name: str
    kind: str
    records: pd.DataFrame          # id, ticker, available_at (UTC), entity_id, value columns
    value_columns: list[str]
    validation: FeedValidation
    source: dict = field(default_factory=dict)

    def manifest(self) -> dict:
        r = self.records
        return {
            "kind": self.kind,
            "source": self.source,
            "records": int(len(r)),
            "tickers": int(r["ticker"].nunique()) if len(r) else 0,
            "first_available_at": r["available_at"].min().isoformat() if len(r) else None,
            "last_available_at": r["available_at"].max().isoformat() if len(r) else None,
            "warnings": list(self.validation.warnings),
        }


def _parse_time_of_day(value: str) -> time:
    try:
        return pd.Timestamp(f"2000-01-01 {value}").time()
    except (ValueError, TypeError):
        raise DataError(f"default_time {value!r} is not a time of day like '18:00'") from None


def parse_available_at(values: pd.Series, timezone: str | None, default_time: str):
    """Convert raw timestamps to UTC.

    Returns (utc series, list of errors, mask of values missing a time zone).
    Values with an offset are converted
    directly. Date-only values get ``default_time`` in ``timezone`` (US/Eastern
    if none is set). Values with a time but no offset need ``timezone``.
    """
    errors = []
    n = len(values)
    out = pd.Series(pd.NaT, index=values.index, dtype="datetime64[us, UTC]")
    if n == 0:
        return out, errors, pd.Series(False, index=values.index)
    tod = _parse_time_of_day(default_time)
    local_tz = timezone or ET

    if isinstance(values.dtype, pd.DatetimeTZDtype):
        return values.dt.tz_convert("UTC").astype("datetime64[us, UTC]"), errors, pd.Series(False, index=values.index)

    if pd.api.types.is_datetime64_dtype(values):
        naive = pd.to_datetime(values)
        date_only = naive.notna() & (naive == naive.dt.normalize())
        with_time = naive.notna() & ~date_only
        strings = None
    else:
        strings = values.astype("string").str.strip()
        is_date = values.map(lambda v: hasattr(v, "year") and not hasattr(v, "hour"))  # datetime.date objects
        date_only = strings.str.match(_DATE_ONLY).fillna(False).astype(bool) | is_date
        has_offset = strings.str.contains(_HAS_OFFSET).fillna(False).astype(bool) & ~date_only
        with_time = strings.notna() & ~date_only & ~has_offset
        if has_offset.any():
            out[has_offset] = pd.to_datetime(strings[has_offset], utc=True, format="ISO8601", errors="coerce")
        naive = pd.Series(pd.NaT, index=values.index, dtype="datetime64[us]")
        if (date_only | with_time).any():
            mask = date_only | with_time
            naive[mask] = pd.to_datetime(strings[mask].astype(str), format="ISO8601", errors="coerce")
        # Text that is not a timestamp at all is reported as unreadable, not as a missing time zone.
        with_time = with_time & naive.notna()

    if date_only.any():
        local = naive[date_only].dt.normalize() + pd.Timedelta(hours=tod.hour, minutes=tod.minute, seconds=tod.second)
        out[date_only] = local.dt.tz_localize(local_tz, ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")
    no_zone = pd.Series(False, index=values.index)
    if with_time.any():
        if timezone is None:
            no_zone = with_time
            errors.append(f"{int(with_time.sum())} available_at values have a time but no time zone "
                          "(add a UTC offset to the values, or set `timezone:` for the feed)")
        else:
            out[with_time] = naive[with_time].dt.tz_localize(timezone, ambiguous="NaT",
                                                             nonexistent="shift_forward").dt.tz_convert("UTC")
    return out, errors, no_zone


def _record_ids(df: pd.DataFrame, name: str) -> pd.Series:
    """Stable ids from each record's contents (plus a counter for identical rows)."""
    if df.empty:
        return pd.Series([], index=df.index, dtype=object)
    keys = pd.Series(["\x1f".join(map(str, row)) for row in df.itertuples(index=False)], index=df.index)
    ids = keys.map(lambda k: hashlib.sha1(k.encode()).hexdigest()[:12])
    dup = ids.groupby(ids).cumcount()
    return name + "-" + ids + dup.map(lambda k: "" if k == 0 else f"-{k}")


def normalize_records(raw: pd.DataFrame, name: str, kind: str, *, ticker_column="ticker",
                      time_column="available_at", entity_column=None, id_column=None,
                      columns: dict | None = None, timezone: str | None = None,
                      default_time: str = DEFAULT_TIME, now: pd.Timestamp | None = None,
                      known_tickers: set | None = None) -> FeedData:
    """Normalize and validate raw feed records. Never raises for bad data; the
    problems are in ``FeedData.validation``. Records with errors are dropped."""
    v = FeedValidation(name, kind)
    if kind not in KINDS:
        v.errors.append(f"kind must be one of {KINDS}, got {kind!r}")
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]
    for label, col in (("ticker", ticker_column), ("time", time_column)):
        if col not in df.columns:
            v.errors.append(f"missing {label} column {col!r} (columns: {list(df.columns)})")
    declared = dict(columns or {})
    for col in declared:
        if col not in df.columns:
            v.errors.append(f"missing declared column {col!r}")
    if v.errors:
        empty = pd.DataFrame(columns=["id", "ticker", "available_at", "entity_id"])
        return FeedData(name, kind, empty, [], v)

    rename = {ticker_column: "ticker", time_column: "available_at"}
    if entity_column:
        if entity_column not in df.columns:
            v.errors.append(f"missing entity column {entity_column!r}")
        rename[entity_column] = "entity_id"
    if id_column:
        if id_column not in df.columns:
            v.errors.append(f"missing id column {id_column!r}")
        rename[id_column] = "id"
    df = df.rename(columns=rename)

    value_columns = list(declared) if declared else [c for c in df.columns if c not in RESERVED]
    for col in value_columns:
        typ = declared.get(col)
        if isinstance(typ, str):
            typ = COLUMN_TYPES.get(typ)
        if typ in (float, int):
            coerced = pd.to_numeric(df[col], errors="coerce")
            bad = coerced.isna() & df[col].notna()
            if bad.any():
                v.errors.append(f"column {col!r}: {int(bad.sum())} values are not numbers")
            df[col] = coerced.astype(float)
        elif typ is str:
            df[col] = df[col].astype("string").astype(object).where(df[col].notna(), None)
        elif typ is bool:
            df[col] = df[col].astype(bool)

    df["ticker"] = df["ticker"].astype("string").str.strip().str.upper()
    missing_ticker = df["ticker"].isna() | (df["ticker"] == "")
    times, time_errors, no_zone = parse_available_at(df["available_at"], timezone, default_time)
    v.errors += time_errors
    df["available_at"] = times
    missing_time = df["available_at"].isna()
    if missing_ticker.any():
        v.errors.append(f"{int(missing_ticker.sum())} records have no ticker")
    unreadable = int((missing_time & ~missing_ticker & ~no_zone).sum())
    if unreadable:
        v.errors.append(f"{unreadable} records have a missing or unreadable available_at")
    df = df[~missing_ticker & ~missing_time]

    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    future = df["available_at"] > now
    if future.any():
        v.errors.append(f"{int(future.sum())} records have available_at in the future "
                        f"(latest {df.loc[future, 'available_at'].max().isoformat()})")

    keep = ["ticker", "available_at"] + (["entity_id"] if "entity_id" in df else []) \
        + (["id"] if "id" in df else []) + value_columns
    df = df[keep]
    dups = df.duplicated(keep="first")
    if dups.any():
        v.warnings.append(f"{int(dups.sum())} exact duplicate records were dropped")
        df = df[~dups]
    if "entity_id" not in df:
        df["entity_id"] = None
    if "id" not in df:
        df["id"] = _record_ids(df[["ticker", "available_at"] + value_columns], name)
    else:
        df["id"] = df["id"].astype(str)
        if df["id"].duplicated().any():
            v.errors.append(f"{int(df['id'].duplicated().sum())} records share an id with another record")

    df = df[["id", "ticker", "available_at", "entity_id"] + value_columns]
    df = df.sort_values(["available_at", "ticker", "id"], kind="stable").reset_index(drop=True)

    tickers = set(df["ticker"])
    v.stats = {
        "records": int(len(df)),
        "tickers": len(tickers),
        "first available_at": df["available_at"].min().isoformat() if len(df) else None,
        "last available_at": df["available_at"].max().isoformat() if len(df) else None,
    }
    if known_tickers is not None:
        unknown = sorted(tickers - known_tickers)
        v.stats["tickers without cached prices"] = len(unknown)
        if unknown:
            shown = ", ".join(unknown[:10]) + (" ..." if len(unknown) > 10 else "")
            v.warnings.append(f"{len(unknown)} tickers have no cached prices yet ({shown}); "
                              "they are fetched on the first run, and records Yahoo has no prices for are skipped")
    return FeedData(name, kind, df, value_columns, v)


def session_opens_utc(sessions: pd.DatetimeIndex) -> np.ndarray:
    """09:30 ET on each session date, as UTC nanoseconds."""
    local = (pd.DatetimeIndex(sessions) + pd.Timedelta(hours=9, minutes=30)).tz_localize(ET)
    return local.tz_convert("UTC").as_unit("ns").asi8


def utc_ns(times: pd.Series) -> np.ndarray:
    """UTC timestamps as nanoseconds, whatever unit pandas stored them in."""
    return pd.DatetimeIndex(times).tz_convert("UTC").as_unit("ns").asi8

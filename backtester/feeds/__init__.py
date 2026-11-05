"""Feeds: data beyond prices, filtered point-in-time on ``available_at``.

Two kinds:
    event      Discrete records that appear at a moment; read with ``ctx.events(name)``.
    reference  A per-ticker value that changes over time; read with ``ctx.ref(ticker, field)``.

Two ways to add one:
    File import  Drop CSV or Parquet files into ``data/feeds/<name>/`` with a ``feed.yaml``.
    Plug-in      Subclass ``Feed`` in a ``.py`` file in ``feeds/``.

Built-in: ``shares_outstanding`` (Yahoo) and ``market_cap`` (raw close x shares outstanding).
"""

from .base import EVENT, REFERENCE, Feed, FeedData, FeedValidation, StaticFeed, normalize_records
from .registry import FeedRegistry
from .view import Event, FeedView

__all__ = ["EVENT", "REFERENCE", "Event", "Feed", "FeedData", "FeedRegistry", "FeedValidation",
           "FeedView", "StaticFeed", "normalize_records"]

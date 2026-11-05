"""The validation dry run, executed in a child process.

    python -m backtester.dryrun <strategy.py> [--cache-dir DIR] [--universes-dir DIR]

Runs the strategy for the last 60 sessions on up to 5 of its tickers and prints
one JSON line: ``{"ok": true, "hash": ..., "summary": ...}`` or
``{"ok": false, "issues": [...]}``. Running in a child process lets the
validator time out runaway strategies and compare runs made with different
hash seeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
import warnings
from pathlib import Path

import pandas as pd

from .validation import DRY_RUN_SESSIONS, DRY_RUN_TICKERS, Issue, _src_line, _strategy_frame_line

SYNTHETIC_START, SYNTHETIC_END = "2021-01-04", "2023-12-29"

HINTS = {
    "LookaheadError": "Only bars before today are visible. Use ctx.history(...), which ends at "
                      "yesterday's close, or ctx.bar(...) with an earlier date.",
    "OrderError": "Fix the order arguments; see ctx.order and ctx.target_weights in STRATEGY_API.md.",
    "KeyError": "A ticker or key was missing. ctx.history has columns only for ctx.universe (or the "
                "tickers you pass); check `if ticker in df.columns` before using one.",
    "IndexError": "History can be shorter than requested at the start of a run; check len(...) "
                  "before indexing.",
    "ZeroDivisionError": "Guard the division: a price, count or volatility can be zero.",
    "AttributeError": "Check the name against STRATEGY_API.md; self.p.<name> holds the params.",
    "TypeError": "Check the arguments against the signatures in STRATEGY_API.md.",
    "ValueError": "Check the values passed at this line (NaN prices from missing bars are a common cause).",
}


def _frames(tickers: list[str], cache_dir: Path | None, end: pd.Timestamp | None):
    """Cached adjusted bars for every ticker, or synthetic bars if any is missing
    (or the cache does not reach ``end``)."""
    from .data.yfinance_provider import YFinanceProvider
    from .ids import SecurityMaster
    from .testing import random_walk_bars

    provider = YFinanceProvider(cache_dir=cache_dir or Path("data") / "cache", refresh=False)
    master = SecurityMaster()
    frames, missing = {}, []
    for t in tickers:
        try:
            df = provider.get_bars(master.to_id(t))
        except Exception:
            df = None
        if df is not None and end is not None:
            df = df[df.index <= end]
            if len(df) and df.index[-1] < end - pd.Timedelta(days=7):
                df = None
        if df is None or len(df) <= DRY_RUN_SESSIONS:
            missing.append(t)
        else:
            frames[t] = df
    if not missing:
        return frames, "cached prices"
    s_end = end if end is not None else pd.Timestamp(SYNTHETIC_END)
    s_start = s_end - pd.Timedelta(days=3 * 365) if end is not None else pd.Timestamp(SYNTHETIC_START)
    synthetic = {
        t: random_walk_bars(s_start, s_end, seed=7 + k, start_price=40 + 15 * k,
                            drift=0.0002 + 0.0001 * k, daily_vol=0.01 + 0.002 * k)
        for k, t in enumerate(tickers)
    }
    return synthetic, f"synthetic prices; not cached: {', '.join(missing)}"


def _event_window_end(event_records: list[pd.DataFrame]) -> pd.Timestamp | None:
    """A session shortly after the latest event, so the dry run sees real events."""
    from . import calendar
    from .calendar import sessions as nyse_sessions

    last = max((r["available_at"].max() for r in event_records if len(r)), default=None)
    if last is None:
        return None
    day = last.tz_convert("America/New_York").tz_localize(None).normalize()
    after = nyse_sessions(day, day + pd.Timedelta(days=20))
    end = after[min(5, len(after) - 1)] if len(after) else day
    latest = calendar.previous_session(pd.Timestamp.now().normalize())
    return min(end, latest)


def _top_tickers(records: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, n: int) -> list[str]:
    """The n tickers with the most records in [start, end] (or overall, if none)."""
    times = records["available_at"].dt.tz_convert("America/New_York").dt.tz_localize(None)
    window = records[(times >= start - pd.Timedelta(days=3)) & (times <= end)]
    pool = window if len(window) else records
    counts = pool.groupby("ticker").size().reset_index(name="n").sort_values(["n", "ticker"], ascending=[False, True])
    return counts["ticker"].head(n).tolist()


def dry_run(path: Path, cache_dir: Path | None = None, universes_dir: Path | None = None,
            feeds_dir: Path | None = None, plugins_dir: Path | None = None) -> dict:
    from .calendar import sessions as nyse_sessions
    from .config import resolve_universe, universe_feed
    from .feeds import StaticFeed
    from .feeds.registry import DEFAULT_CACHE_DIR, DEFAULT_FEEDS_DIR, DEFAULT_PLUGINS_DIR, FeedRegistry
    from .run import metrics_json, run_backtest
    from .strategy import load_strategy_file
    from .testing import InMemoryProvider

    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    cls = load_strategy_file(path)
    registry_args = (feeds_dir or DEFAULT_FEEDS_DIR, plugins_dir or DEFAULT_PLUGINS_DIR, cache_dir or DEFAULT_CACHE_DIR)
    registry = FeedRegistry(*registry_args, refresh=False)

    # Event feeds decide the window; the run gets them cut down to the chosen tickers.
    event_data = {}
    for name in dict.fromkeys(cls.requires):
        if name not in ("prices", "shares_outstanding", "market_cap") and registry.kind(name) == "event":
            event_data[name] = registry.load(name)
    window_end = _event_window_end([d.records for d in event_data.values()])
    from_feed = universe_feed(cls.universe)
    if from_feed is not None:
        records = event_data[from_feed].records if from_feed in event_data else registry.load(from_feed).records
        anchor = window_end if window_end is not None else records["available_at"].max().tz_localize(None)
        span = nyse_sessions(anchor - pd.Timedelta(days=DRY_RUN_SESSIONS * 2 + 30), anchor)[-DRY_RUN_SESSIONS:]
        tickers = _top_tickers(records, span[0], anchor, DRY_RUN_TICKERS)
    else:
        tickers = resolve_universe(cls.universe, universes_dir)[:DRY_RUN_TICKERS]
    extra = {name: StaticFeed(name, "event", d.records[d.records["ticker"].isin(tickers)], timezone="UTC")
             for name, d in event_data.items()}
    if from_feed is not None and from_feed not in extra:
        recs = registry.load(from_feed)
        extra[from_feed] = StaticFeed(from_feed, recs.kind, recs.records[recs.records["ticker"].isin(tickers)],
                                      timezone="UTC")
    feeds = FeedRegistry(*registry_args, refresh=False, extra=extra)

    frames, source = _frames(tickers, cache_dir, window_end)
    end = min(df.index[-1] for df in frames.values())
    sessions = nyse_sessions(end - pd.Timedelta(days=DRY_RUN_SESSIONS * 2 + 30), end)[-DRY_RUN_SESSIONS:]
    overrides = {"start": sessions[0].date().isoformat(), "end": end.date().isoformat(), "benchmark": tickers[0]}
    if from_feed is None:
        overrides["universe"] = tickers
    try:
        result = run_backtest(path, overrides, provider=InMemoryProvider(frames), universes_dir=universes_dir,
                              feeds=feeds)
    except Exception as exc:
        line = _strategy_frame_line(exc.__traceback__, path)
        name = type(exc).__name__
        where = "" if line is not None else " (inside the engine, called from the strategy)"
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:])
        return {"ok": False, "issues": [asdict_issue(Issue(
            "exception", f"{name}: {exc}{where}", HINTS.get(name, "Fix the error at this line."),
            path.name, line, _src_line(lines, line), detail=None if line is not None else tb,
        ))]}

    tickers = result.manifest.get("universe_tickers", tickers)
    issues = []
    strategy_orders = result.orders[result.orders["source"] == "strategy"]
    outside = sorted(set(strategy_orders["ticker"]) - set(tickers))
    if outside:
        # Point at the first line that names one of the tickers, if any does.
        line = next((k for k, text in enumerate(lines, start=1)
                     if any(f'"{t}"' in text or f"'{t}'" in text for t in outside)), None)
        issues.append(Issue(
            "order-ticker", f"orders placed for tickers outside the universe: {', '.join(outside)}",
            "Trade only tickers in ctx.universe (add the ticker to `universe` if you meant to trade it); "
            "other tickers have no price data and can never fill.",
            path.name, line, _src_line(lines, line)))
    if issues:
        return {"ok": False, "issues": [asdict_issue(i) for i in issues]}

    digest = hashlib.sha256()
    digest.update(metrics_json(result.metrics).encode())
    for frame in (result.orders, result.trades, result.signals, result.equity):
        digest.update(frame.to_csv().encode())
    digest.update("\n".join(result.log).encode())

    def n(count, word):
        return f"{count} {word}{'' if count == 1 else 's'}"

    events = int((result.signals["decision"] == "event").sum())
    summary = (f"{n(len(result.equity), 'session')}, {n(len(tickers), 'ticker')}, {source}; "
               + (f"{n(events, 'event')}, " if events else "")
               + f"{n(len(strategy_orders), 'order')}, {n(len(result.fills), 'fill')}")
    return {"ok": True, "hash": digest.hexdigest(), "summary": summary}


def asdict_issue(issue: Issue) -> dict:
    from dataclasses import asdict
    return asdict(issue)


def main(argv=None) -> int:
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser()
    p.add_argument("strategy", type=Path)
    p.add_argument("--cache-dir", type=Path)
    p.add_argument("--universes-dir", type=Path)
    p.add_argument("--feeds-dir", type=Path)
    p.add_argument("--plugins-dir", type=Path)
    args = p.parse_args(argv)
    try:
        out = dry_run(args.strategy, args.cache_dir, args.universes_dir, args.feeds_dir, args.plugins_dir)
    except Exception as exc:  # setup failures (data, config) rather than strategy errors
        out = {"ok": False, "issues": [asdict_issue(Issue(
            "dry-run-setup", f"{type(exc).__name__}: {exc}",
            "Check the strategy's universe and that its declarations load.", args.strategy.name))]}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

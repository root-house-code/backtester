"""Run a strategy end to end and write its run folder.

A run is a config in and a folder out:
    runs/<YYYYmmdd-HHMMSS>-<strategy>/
        config.json  strategy.py  strategy.sha256  equity.parquet  trades.parquet
        signals.parquet  orders.parquet  prices.parquet  metrics.json
        data_manifest.json  log.txt
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .analytics import compute_metrics, in_out_of_sample, walk_forward
from .calendar import sessions as nyse_sessions
from .config import ResolvedConfig, resolve_config, universe_feed
from .data.panel import PricePanel
from .data.provider import PriceProvider
from .engine import Engine
from .errors import DataError, StrategyError
from .feeds import FeedRegistry, FeedView
from .ids import SecurityMaster
from .strategy import Strategy, check_strategy_class, load_strategy_file


@dataclass
class RunResult:
    config: ResolvedConfig
    strategy_source: str
    strategy_sha256: str
    equity: pd.DataFrame
    trades: pd.DataFrame
    orders: pd.DataFrame
    signals: pd.DataFrame
    fills: pd.DataFrame
    metrics: dict
    manifest: dict
    log: list[str]
    prices: pd.DataFrame


def metrics_json(metrics: dict) -> str:
    """Canonical serialization of metrics.json (stable key order, trailing newline)."""
    return json.dumps(metrics, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _load(strategy) -> tuple[type[Strategy], str, str]:
    if isinstance(strategy, (str, Path)):
        from .validation import static_report

        path = Path(strategy)
        if not path.is_file():
            raise StrategyError(f"strategy file not found: {path}")
        source = path.read_text(encoding="utf-8")
        report = static_report(path, source)
        if report is not None:
            raise StrategyError(report.text())
        cls = load_strategy_file(path)
        return cls, source, path.name
    check_strategy_class(strategy)
    try:
        source = inspect.getsource(inspect.getmodule(strategy))
    except (OSError, TypeError):
        source = ""
    return strategy, source, f"<class {strategy.__name__}>"


def load_feeds(cls, cfg: ResolvedConfig, registry: FeedRegistry):
    """Load the feeds a strategy requires and resolve its tickers.

    Returns (feed data by name, tickers to load prices for, from_feed name or None).
    """
    required = [r for r in dict.fromkeys(cls.requires) if r != "prices"]
    data = {}
    for name in required:
        if name != "shares_outstanding":
            data[name] = registry.load(name, end=cfg.end)
    feed = universe_feed(cfg["universe"])
    if feed is not None:
        records = data[feed].records
        cutoff = (cfg.end + pd.Timedelta(days=1, hours=9, minutes=30)).tz_localize("America/New_York")
        tickers = list(dict.fromkeys(records.loc[records["available_at"] < cutoff, "ticker"]))
        if not tickers:
            raise DataError(f"universe from_feed:{feed} is empty: the feed has no records before {cfg['end']}")
    else:
        tickers = list(cfg["universe"])
    if "shares_outstanding" in required or "market_cap" in required:
        data["shares_outstanding"] = registry.load("shares_outstanding", end=cfg.end, tickers=tickers)
    return data, tickers, feed


def _ref_fields(cls, data: dict) -> dict:
    """Map ``ctx.ref`` field names to (feed, column). Ambiguous plain names need "feed.column"."""
    plain: dict[str, list] = {}
    fields = {}
    for name in dict.fromkeys(cls.requires):
        if name == "market_cap":
            fields["market_cap"] = ("market_cap", "market_cap")
            continue
        d = data.get(name)
        if d is None or d.kind != "reference":
            continue
        for col in d.value_columns:
            plain.setdefault(col, []).append((name, col))
            fields[f"{name}.{col}"] = (name, col)
    for col, owners in plain.items():
        if len(owners) == 1 and col not in fields:
            fields[col] = owners[0]
    return fields


def run_backtest(
    strategy,
    overrides: dict,
    provider: PriceProvider | None = None,
    universes_dir: Path | None = None,
    feeds: FeedRegistry | None = None,
) -> RunResult:
    """Run ``strategy`` (a file path or Strategy subclass) with config ``overrides``.

    ``overrides`` holds ``start`` and ``end`` (required) plus any config field
    or ``params`` dict. Unset fields fall back to strategy, then engine defaults.
    ``feeds`` supplies the strategy's required feeds (default: the feeds under
    ``data/feeds`` and ``feeds/``, plus the built-ins).
    """
    cls, source, file_label = _load(strategy)
    cfg = resolve_config(cls, overrides, universes_dir)
    sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    cfg.strategy = {
        "name": cls.name, "class": cls.__name__, "file": file_label,
        "sha256": sha, "api_version": cls.api_version,
    }

    if provider is None:
        from .data.yfinance_provider import YFinanceProvider
        provider = YFinanceProvider()
    if feeds is None:
        cache_root = provider.dir.parent if hasattr(provider, "dir") else Path("data") / "cache"
        feeds = FeedRegistry(cache_dir=cache_root, refresh=getattr(provider, "refresh", True))
    feed_data, tickers, from_feed = load_feeds(cls, cfg, feeds)

    master = SecurityMaster()
    universe_ids = [master.to_id(t) for t in tickers]
    bench_id = master.to_id(cfg["benchmark"])
    sids = list(dict.fromkeys(universe_ids + [bench_id]))
    def bars(sid):
        try:
            return provider.get_bars(sid)
        except DataError:
            if from_feed is None:
                raise
            return None  # a ticker a feed mentions but no price source has: logged and skipped

    frames = {sid: bars(sid) for sid in sids}
    present = [df for df in frames.values() if df is not None and not df.empty]
    if not present:
        raise DataError(f"no price data for any of {[str(s) for s in sids]}")

    first = min(df.index[0] for df in present)
    last = max(df.index[-1] for df in present)
    sessions = nyse_sessions(min(first, cfg.start), cfg.end)
    panel = PricePanel.build(frames, sessions)
    end = min(cfg.end, last)
    start_idx = int(np.searchsorted(sessions, cfg.start, side="left"))
    end_idx = int(np.searchsorted(sessions, end, side="right")) - 1
    if start_idx > end_idx or start_idx >= len(sessions):
        raise DataError(f"no sessions with data between {cfg['start']} and {cfg['end']}")

    views = {name: FeedView(d, sessions) for name, d in feed_data.items() if name != "market_cap"}
    engine = Engine(cls, cfg, panel, master, start_idx, end_idx, universe=tickers, feeds=views,
                    universe_feed=from_feed, ref_fields=_ref_fields(cls, feed_data))
    engine.log("info", f"run start: {cls.name} ({file_label}) {cfg['start']} -> {cfg['end']}")
    if cfg.end > last:
        engine.log("warning", f"data ends {last.date()}; run end clipped from {cfg['end']}")
    missing = [str(s) for s, df in frames.items() if df is None or df.empty]
    if missing:
        engine.log("warning", f"no price data for: {', '.join(missing)}")
    priced = {str(s) for s, df in frames.items() if df is not None and not df.empty}
    for name, d in feed_data.items():
        if d.kind == "event" and len(d.records):
            unmatched = d.records[~d.records["ticker"].isin(priced)]
            if len(unmatched):
                engine.log("warning", f"feed {name}: {len(unmatched)} records for {unmatched['ticker'].nunique()} "
                                      "tickers with no price data; orders for them are skipped")
    engine.run()
    engine.log("info", "run complete")

    equity = pd.DataFrame(engine.equity_rows).set_index("date")
    equity["benchmark"] = _benchmark(panel, bench_id, start_idx, end_idx, cfg["initial_capital"])
    trades = engine.trades_frame()
    fills = engine.fills_frame()
    signals = engine.signals_frame()
    orders = engine.orders_frame()
    capital, seed = cfg["initial_capital"], cfg["seed"]
    metrics = compute_metrics(equity, trades, fills, signals, capital, seed=seed)
    if cfg["in_sample_end"] is not None:
        metrics["split"] = in_out_of_sample(equity, trades, fills, signals, cfg["in_sample_end"], capital, seed)
    if cfg["walk_forward"] is not None:
        wf = cfg["walk_forward"]
        metrics["walk_forward"] = walk_forward(equity, trades, fills, signals, wf["window"], wf["step"], capital, seed)
        if not metrics["walk_forward"]["folds"]:
            engine.log("warning", f"walk-forward {wf['window']},{wf['step']} has no complete fold in this run")

    manifest = provider.manifest(sids)
    manifest.update({
        "universe": cfg["universe"] if from_feed else list(cfg["universe"]),
        "universe_tickers": tickers,
        "feeds": {name: d.manifest() for name, d in feed_data.items()},
        "benchmark": str(bench_id),
        "sessions": {"first": sessions[start_idx].date().isoformat(),
                     "last": sessions[end_idx].date().isoformat(),
                     "count": end_idx - start_idx + 1},
        "calendar": "XNYS (exchange_calendars)",
    })
    return RunResult(cfg, source, sha, equity, trades, orders, signals, fills, metrics,
                     manifest, engine.log_lines, _traded_prices(panel, fills, start_idx, end_idx))


def _benchmark(panel: PricePanel, sid, start_idx: int, end_idx: int, capital: float) -> np.ndarray:
    """Benchmark value: capital invested at the first available open, marked at each close."""
    n = end_idx - start_idx + 1
    if not panel.has(sid):
        return np.full(n, np.nan)
    c = panel.col(sid)
    opens = panel.arrays["open"][start_idx:end_idx + 1, c]
    closes = pd.Series(panel.arrays["close"][start_idx:end_idx + 1, c]).ffill().to_numpy()
    valid = np.flatnonzero(~np.isnan(opens))
    if not len(valid):
        return np.full(n, np.nan)
    b0 = valid[0]
    out = np.full(n, capital, dtype=float)
    out[b0:] = capital * closes[b0:] / opens[b0]
    return out


PRICE_CONTEXT_SESSIONS = 60


def _traded_prices(panel: PricePanel, fills: pd.DataFrame, start_idx: int, end_idx: int) -> pd.DataFrame:
    """Adjusted OHLC for every traded ticker, so trades can be charted from the run folder alone.

    Covers the run plus some sessions before it, for chart context.
    """
    cols = ["date", "ticker", "open", "high", "low", "close"]
    lo = max(0, start_idx - PRICE_CONTEXT_SESSIONS)
    frames = []
    for sid in sorted(fills["security_id"].unique()) if len(fills) else []:
        c = panel.col(sid)
        df = pd.DataFrame({f: panel.arrays[f][lo:end_idx + 1, c] for f in cols[2:]})
        df.insert(0, "ticker", sid)
        df.insert(0, "date", panel.sessions[lo:end_idx + 1])
        frames.append(df.dropna(subset=["close"]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "strategy"


def write_run_folder(result: RunResult, runs_dir: str | Path = "runs", now: datetime | None = None) -> Path:
    """Write a self-contained run folder and return its path."""
    runs_dir = Path(runs_dir)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{_slug(result.config.strategy['name'])}"
    folder = runs_dir / base
    k = 2
    while folder.exists():
        folder = runs_dir / f"{base}-{k}"
        k += 1
    folder.mkdir(parents=True)

    config = result.config.to_dict()
    config["engine_version"] = __version__
    (folder / "config.json").write_text(json.dumps(config, indent=2, default=str) + "\n")
    (folder / "strategy.py").write_text(result.strategy_source, encoding="utf-8")
    (folder / "strategy.sha256").write_text(f"{result.strategy_sha256}  strategy.py\n")
    result.equity.reset_index().to_parquet(folder / "equity.parquet", index=False)
    result.trades.to_parquet(folder / "trades.parquet", index=False)
    result.signals.to_parquet(folder / "signals.parquet", index=False)
    result.orders.to_parquet(folder / "orders.parquet", index=False)
    result.prices.to_parquet(folder / "prices.parquet", index=False)
    (folder / "metrics.json").write_text(metrics_json(result.metrics))
    (folder / "data_manifest.json").write_text(json.dumps(result.manifest, indent=2, default=str) + "\n")
    (folder / "log.txt").write_text("\n".join(result.log) + "\n", encoding="utf-8")
    return folder

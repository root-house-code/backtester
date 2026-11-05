"""Feeds: point-in-time event timing, reference values, file and plug-in feeds,
feed validation, from_feed universes and the signal audit."""

import json
import math
import textwrap

import pandas as pd
import pytest
from helpers import make_strategy, rotation_frames

from backtester import Param, Strategy
from backtester.data.yfinance_provider import YFinanceProvider
from backtester.errors import ConfigError, DataError, StrategyError
from backtester.feeds import FeedRegistry, normalize_records
from backtester.feeds.sources import SharesOutstanding
from backtester.ids import SecurityId
from backtester.run import run_backtest, write_run_folder
from backtester.testing import InMemoryProvider, make_fixture, random_walk_bars, run_on_fixture
from backtester.validation import validate

# Fixture sessions: 0 Tue 2024-01-02, 1 Wed 01-03, 2 Thu 01-04, 3 Fri 01-05, 4 Mon 01-08.
PRICES = {"AAA": [10.0] * 12, "BBB": [20.0] * 12}


def buy_on_events(feed="ev", universe=("AAA", "BBB")):
    def on_bar(self, ctx):
        for e in ctx.events(feed):
            ctx.order(e.ticker, shares=1, at="open", tag=e.id)
    return make_strategy(on_bar, universe=universe, requires=["prices", feed])


def fill_session(available_at, **record):
    fx = make_fixture(prices=PRICES, events={"ev": [{"ticker": "AAA", "available_at": available_at, **record}]})
    result = run_on_fixture(buy_on_events(), fx)
    assert len(result.fills) == 1, result.orders
    fill = result.fills.iloc[0]
    assert fill["at"] == "open"
    return int(fill["session"])


# --- event timing (spec: Correctness and testing) -------------------------------------

def test_event_after_the_close_fills_at_the_next_open():
    assert fill_session("2024-01-03 17:00") == 2


def test_event_during_the_session_fills_at_the_next_open_not_the_one_that_already_happened():
    assert fill_session("2024-01-03 10:00") == 2


def test_date_only_record_never_trades_the_same_session():
    assert fill_session("2024-01-03") == 2


def test_event_before_the_open_fills_at_that_open():
    assert fill_session("2024-01-03 09:00") == 1
    assert fill_session("2024-01-03T14:29:59Z") == 1   # 09:29:59 ET


def test_event_exactly_at_the_open_waits_for_the_next_session():
    assert fill_session("2024-01-03 09:30") == 2


def test_weekend_event_fills_on_monday():
    assert fill_session("2024-01-06 12:00") == 4


def test_events_are_delivered_once_and_never_early():
    seen = []

    def on_bar(self, ctx):
        open_utc = (ctx.today + pd.Timedelta(hours=9, minutes=30)).tz_localize("America/New_York").tz_convert("UTC")
        for e in ctx.events("ev"):
            assert e.available_at < open_utc
            seen.append(e.id)

    records = [{"ticker": "AAA", "available_at": f"2024-01-{d:02d} {h:02d}:00"} for d in range(2, 12) for h in (8, 12, 20)]
    fx = make_fixture(prices=PRICES, events={"ev": records})
    run_on_fixture(make_strategy(on_bar, universe=["AAA"], requires=["prices", "ev"]), fx)
    assert len(seen) == len(set(seen))
    # Every record becomes known before the last session's open, so all are delivered exactly once.
    assert len(seen) == len(records)


def test_old_events_are_not_delivered_as_new():
    fx = make_fixture(prices=PRICES, events={"ev": [{"ticker": "AAA", "available_at": "2023-12-20 17:00"}]})
    assert run_on_fixture(buy_on_events(), fx).orders.empty


def test_event_fields_are_attributes_and_items():
    got = {}

    def on_bar(self, ctx):
        for e in ctx.events("ev"):
            got.update(score=e.score, item=e["note"], ticker=e.ticker, feed=e.feed)
            with pytest.raises(AttributeError, match="no field 'missing'"):
                e.missing

    fx = make_fixture(prices=PRICES, events={"ev": [
        {"ticker": "aaa", "available_at": "2024-01-03 17:00", "score": 0.7, "note": "hi"}]})
    run_on_fixture(make_strategy(on_bar, universe=["AAA"], requires=["prices", "ev"]), fx)
    assert got == {"score": 0.7, "item": "hi", "ticker": "AAA", "feed": "ev"}


def test_unknown_event_feed_is_an_error():
    def on_bar(self, ctx):
        ctx.events("nope")

    fx = make_fixture(prices=PRICES)
    with pytest.raises(StrategyError, match="not an event feed"):
        run_on_fixture(make_strategy(on_bar, universe=["AAA"]), fx)


# --- reference values ---------------------------------------------------------------------

def test_ref_is_point_in_time():
    seen = {}

    def on_bar(self, ctx):
        seen[len(seen)] = ctx.ref("AAA", "sector")

    fx = make_fixture(prices=PRICES, refs={"sectors": [
        {"ticker": "AAA", "available_at": "2023-06-01", "sector": "Tech"},
        {"ticker": "AAA", "available_at": "2024-01-03 17:00", "sector": "Energy"},
    ]})
    run_on_fixture(make_strategy(on_bar, universe=["AAA"], requires=["prices", "sectors"]), fx)
    assert [seen[k] for k in range(4)] == ["Tech", "Tech", "Energy", "Energy"]


def test_ref_default_and_unknown_field():
    def on_bar(self, ctx):
        assert math.isnan(ctx.ref("BBB", "sector"))
        assert ctx.ref("BBB", "sector", default="?") == "?"
        with pytest.raises(StrategyError, match="unknown reference field"):
            ctx.ref("AAA", "colour")

    fx = make_fixture(prices=PRICES, refs={"sectors": [{"ticker": "AAA", "available_at": "2023-06-01", "sector": "x"}]})
    run_on_fixture(make_strategy(on_bar, universe=["AAA", "BBB"], requires=["prices", "sectors"]), fx)


def test_market_cap_is_raw_close_times_shares_known_at_the_time():
    caps = []

    def on_bar(self, ctx):
        caps.append(ctx.ref("AAA", "market_cap"))

    closes = [10.0, 11.0, 12.0, 13.0, 14.0]
    fx = make_fixture(prices={"AAA": closes}, refs={"shares_outstanding": [
        {"ticker": "AAA", "available_at": "2023-12-01", "shares_outstanding": 1_000},
        {"ticker": "AAA", "available_at": "2024-01-03", "shares_outstanding": 2_000},  # after the close
    ]})
    run_on_fixture(make_strategy(on_bar, universe=["AAA"], requires=["prices", "market_cap"]), fx)
    # Session 0 has no visible close yet; later sessions use the previous close.
    assert math.isnan(caps[0])
    assert caps[1:] == [10.0 * 1_000, 11.0 * 2_000, 12.0 * 2_000, 13.0 * 2_000]


def test_market_cap_uses_the_unadjusted_close():
    bars = random_walk_bars("2024-01-02", "2024-01-31", seed=1)
    bars["close_raw"] = bars["close"] * 2   # e.g. a later 2:1 split halves the adjusted prices
    seen = []

    def on_bar(self, ctx):
        seen.append((ctx.ref("AAA", "market_cap"), ctx.price("AAA")))

    strategy = make_strategy(on_bar, universe=["AAA"], requires=["prices", "market_cap"])
    feeds = FeedRegistry(None, None, "unused", refresh=False, extra={
        "shares_outstanding": make_fixture(prices={"X": [1.0]}, refs={"shares_outstanding": [
            {"ticker": "AAA", "available_at": "2023-01-01", "shares_outstanding": 100}]}).feeds["shares_outstanding"]})
    run_backtest(strategy, {"start": "2024-01-03", "end": "2024-01-10", "benchmark": "AAA"},
                 provider=InMemoryProvider({"AAA": bars}), feeds=feeds)
    for cap, adjusted_close in seen:
        assert cap == pytest.approx(100 * 2 * adjusted_close)


def test_shares_outstanding_are_cached_per_ticker(tmp_path, monkeypatch):
    calls = []

    def fake(ticker):
        calls.append(ticker)
        return pd.Series([1e6, 2e6], index=pd.DatetimeIndex(["2023-01-05", "2023-06-05"]).tz_localize("America/New_York"))

    monkeypatch.setattr(SharesOutstanding, "fetcher", staticmethod(fake))
    reg = FeedRegistry(None, None, tmp_path, refresh=True)
    data = reg.load("shares_outstanding", tickers=["AAA", "BBB"])
    assert sorted(data.records["ticker"].unique()) == ["AAA", "BBB"]
    assert data.records["available_at"].iloc[0] == pd.Timestamp("2023-01-05 18:00", tz="America/New_York")
    FeedRegistry(None, None, tmp_path, refresh=True).load("shares_outstanding", tickers=["AAA"])
    assert calls == ["AAA", "BBB"]   # second load came from the cache
    offline = FeedRegistry(None, None, tmp_path, refresh=False).load("shares_outstanding", tickers=["CCC"])
    assert offline.records.empty and calls == ["AAA", "BBB"]


# --- from_feed universes --------------------------------------------------------------------

def test_from_feed_universe_grows_as_records_become_visible():
    seen = []

    def on_bar(self, ctx):
        seen.append(ctx.universe)

    fx = make_fixture(prices=PRICES, events={"ev": [
        {"ticker": "BBB", "available_at": "2024-01-02 17:00"},
        {"ticker": "AAA", "available_at": "2024-01-04 17:00"},
    ]})
    strategy = make_strategy(on_bar, universe="from_feed:ev", requires=["prices", "ev"])
    run_on_fixture(strategy, fx)
    assert seen[:5] == [[], ["BBB"], ["BBB"], ["BBB", "AAA"], ["BBB", "AAA"]]


def test_the_benchmark_is_tradable_outside_a_from_feed_universe():
    # Park cash in the benchmark (AAA, the fixture's first ticker) while the feed names only BBB.
    def on_bar(self, ctx):
        if not ctx.positions:
            ctx.order(ctx.benchmark, pct=0.5, at="open")

    fx = make_fixture(prices=PRICES, events={"ev": [{"ticker": "BBB", "available_at": "2024-01-02 17:00"}]})
    strategy = make_strategy(on_bar, universe="from_feed:ev", requires=["prices", "ev"])
    result = run_on_fixture(strategy, fx)
    assert result.fills["ticker"].tolist() == ["AAA"]
    assert result.fills["session"].iloc[0] == 0


def test_from_feed_needs_the_feed_in_requires():
    fx = make_fixture(prices=PRICES, events={"ev": [{"ticker": "AAA", "available_at": "2024-01-02"}]})
    strategy = make_strategy(lambda self, ctx: None, universe="from_feed:ev", requires=["prices"])
    with pytest.raises(ConfigError, match="needs 'ev' in the strategy's requires"):
        run_on_fixture(strategy, fx)


# --- signal audit ---------------------------------------------------------------------------

def test_every_event_gets_an_outcome():
    def on_bar(self, ctx):
        for e in ctx.events("ev"):
            if e.score > 0.8:
                ctx.order(e.ticker, shares=1, tag=e.id)
            elif e.score > 0.4:
                ctx.reject(e, "score too low")

    fx = make_fixture(prices={"AAA": [10.0] * 6, "BBB": [5.0] * 6}, events={"ev": [
        {"ticker": "AAA", "available_at": "2024-01-02 17:00", "score": 0.9},
        {"ticker": "BBB", "available_at": "2024-01-02 17:00", "score": 0.5},
        {"ticker": "AAA", "available_at": "2024-01-03 17:00", "score": 0.1},
        {"ticker": "ZZZ", "available_at": "2024-01-03 17:00", "score": 0.95},   # no price data
    ]})
    result = run_on_fixture(make_strategy(on_bar, universe=["AAA", "BBB", "ZZZ"], requires=["prices", "ev"]), fx)
    events = result.signals[result.signals["decision"] == "event"]
    assert events[["ticker", "outcome", "outcome_reason"]].fillna("").values.tolist() == [
        ["AAA", "filled", ""],
        ["BBB", "rejected", "score too low"],
        ["AAA", "no action", ""],
        ["ZZZ", "skipped", "no price data"],
    ]
    assert events["available_at"].notna().all()


# --- file feeds -----------------------------------------------------------------------------

def write_feed(root, name, yaml, csv):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "feed.yaml").write_text(textwrap.dedent(yaml))
    (folder / "data.csv").write_text(textwrap.dedent(csv).lstrip())
    return folder


def test_file_feed_maps_columns_and_times(tmp_path):
    write_feed(tmp_path / "feeds", "ratings", """
        kind: event
        ticker_column: symbol
        time_column: when
        timezone: America/New_York
        columns:
          rating: str
          target: float
    """, """
        symbol,when,rating,target
        aaa,2024-01-03 10:00,buy,12.5
        BBB,2024-01-04,sell,
        CCC,2024-01-05T21:00:00Z,hold,7
    """)
    data = FeedRegistry(tmp_path / "feeds", None, tmp_path / "cache").load("ratings")
    r = data.records
    assert list(r.columns) == ["id", "ticker", "available_at", "entity_id", "rating", "target"]
    assert r["ticker"].tolist() == ["AAA", "BBB", "CCC"]
    assert r["available_at"].tolist() == [
        pd.Timestamp("2024-01-03 15:00", tz="UTC"),   # 10:00 ET
        pd.Timestamp("2024-01-04 23:00", tz="UTC"),   # date-only -> 18:00 ET
        pd.Timestamp("2024-01-05 21:00", tz="UTC"),
    ]
    assert math.isnan(r["target"].iloc[1])
    assert r["id"].is_unique


def test_file_feed_is_cached_until_its_files_change(tmp_path):
    folder = write_feed(tmp_path / "feeds", "ev", "kind: event\n", "ticker,available_at\nAAA,2024-01-03\n")
    reg = FeedRegistry(tmp_path / "feeds", None, tmp_path / "cache")
    first = reg.load("ev")
    meta = json.loads((tmp_path / "cache" / "feeds" / "ev.json").read_text())
    assert meta["fingerprint"]
    (folder / "data.csv").write_text("ticker,available_at\nAAA,2024-01-03\nBBB,2024-01-04\n")
    second = FeedRegistry(tmp_path / "feeds", None, tmp_path / "cache").load("ev")
    assert len(first.records) == 1 and len(second.records) == 2


def test_record_ids_are_stable(tmp_path):
    folder = write_feed(tmp_path / "feeds", "ev", "kind: event\n", "ticker,available_at,x\nAAA,2024-01-03,1\nBBB,2024-01-04,2\n")
    ids = FeedRegistry(tmp_path / "feeds", None, tmp_path / "c1").load("ev").records.set_index("ticker")["id"]
    (folder / "data.csv").write_text("ticker,available_at,x\nBBB,2024-01-04,2\nAAA,2024-01-03,1\n")  # reordered
    again = FeedRegistry(tmp_path / "feeds", None, tmp_path / "c2").load("ev").records.set_index("ticker")["id"]
    assert ids.to_dict() == again.to_dict()


@pytest.mark.parametrize("csv,error", [
    ("symbol,available_at\nAAA,2024-01-03\n", "missing ticker column 'ticker'"),
    ("ticker,available_at\nAAA,2024-01-03 10:00\n", "have a time but no time zone"),
    ("ticker,available_at\nAAA,2999-01-01\n", "in the future"),
    ("ticker,available_at\n,2024-01-03\n", "have no ticker"),
    ("ticker,available_at\nAAA,not a date\n", "unreadable available_at"),
])
def test_feed_validation_errors(tmp_path, csv, error):
    write_feed(tmp_path / "feeds", "ev", "kind: event\n", csv)
    reg = FeedRegistry(tmp_path / "feeds", None, tmp_path / "cache")
    with pytest.raises(DataError, match=error):
        reg.load("ev")
    assert not reg.validate("ev").ok


def test_duplicates_and_unknown_tickers_are_flagged(tmp_path):
    raw = pd.DataFrame({"ticker": ["AAA", "AAA", "ZZZ"], "available_at": ["2024-01-03"] * 3})
    data = normalize_records(raw, "ev", "event", known_tickers={"AAA"})
    assert data.validation.ok
    assert len(data.records) == 2
    assert any("duplicate" in w for w in data.validation.warnings)
    assert data.validation.stats["tickers without cached prices"] == 1


def test_bad_feed_yaml(tmp_path):
    write_feed(tmp_path / "feeds", "ev", "kind: stream\n", "ticker,available_at\nAAA,2024-01-03\n")
    with pytest.raises(DataError, match="kind"):
        FeedRegistry(tmp_path / "feeds", None, tmp_path / "cache").get("ev")


def test_unknown_feed(tmp_path):
    with pytest.raises(DataError, match="unknown feed 'nope'"):
        FeedRegistry(tmp_path, None, tmp_path).load("nope")


# --- plug-in feeds --------------------------------------------------------------------------

PLUGIN = '''
import pandas as pd
from backtester.feeds import Feed

CALLS = []


class Ratings(Feed):
    name = "plug"
    kind = "event"
    columns = {"value": float}
    timezone = "America/New_York"

    def fetch(self, start, end, tickers=None):
        CALLS.append(end)
        return pd.DataFrame({"ticker": ["AAA"], "available_at": ["2024-01-03 17:00"], "value": [1.5]})
'''


def test_plugin_feed_is_fetched_cached_and_refreshable(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "ratings.py").write_text(PLUGIN)
    reg = FeedRegistry(None, plugins, tmp_path / "cache", refresh=True)
    assert reg.names() == ["market_cap", "plug", "shares_outstanding"]
    data = reg.load("plug", end="2024-06-30")
    assert data.records["value"].tolist() == [1.5]
    # Offline, the cache serves the same range; a later end needs a refresh.
    offline = FeedRegistry(None, plugins, tmp_path / "cache", refresh=False)
    assert offline.load("plug", end="2024-06-30").records["value"].tolist() == [1.5]
    with pytest.raises(DataError, match="backtest feeds refresh plug"):
        offline.load("plug", end="2024-12-31")


def test_a_plugin_feed_refreshed_without_an_end_covers_later_runs_offline(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "ratings.py").write_text(PLUGIN)
    FeedRegistry(None, plugins, tmp_path / "cache", refresh=True).load("plug")   # what `feeds refresh` does
    offline = FeedRegistry(None, plugins, tmp_path / "cache", refresh=False)
    assert offline.load("plug", end="2024-06-30").records["value"].tolist() == [1.5]


def test_duplicate_feed_names_are_an_error(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "ratings.py").write_text(PLUGIN)
    write_feed(tmp_path / "feeds", "plug", "kind: event\n", "ticker,available_at\nAAA,2024-01-03\n")
    with pytest.raises(DataError, match="two feeds are named 'plug'"):
        FeedRegistry(tmp_path / "feeds", plugins, tmp_path / "cache").names()


# --- done-when: a feed added only by dropping in a file drives a strategy end to end ----------

STRATEGY = '''
from backtester import Param, Strategy


class OnSignal(Strategy):
    api_version = 1
    name = "On signal"
    description = "Buy the open after each signal; hold five sessions."
    requires = ["prices", "my_signals"]
    universe = "from_feed:my_signals"
    params = {"hold": Param(int, default=5, min=1, max=50)}

    def on_bar(self, ctx):
        for e in ctx.events("my_signals"):
            if e.strength < 0.5:
                ctx.reject(e, "weak signal")
            else:
                ctx.order(e.ticker, pct=0.25, at="open", exit_after=self.p.hold, tag=e.id)
'''


def test_dropped_in_file_feed_drives_a_strategy_end_to_end(tmp_path):
    # 1. A price cache (synthetic, no network) and a strategy file.
    cache = tmp_path / "cache"
    provider = YFinanceProvider(cache_dir=cache, refresh=False)
    for ticker, df in rotation_frames("2018-01-01", "2019-12-31").items():
        provider._save(SecurityId(ticker), df.assign(adj_close=df["close"], dividends=0.0, splits=0.0), full=True)
    strategy = tmp_path / "on_signal.py"
    strategy.write_text(STRATEGY)
    # 2. The feed: nothing but a folder with feed.yaml and a CSV.
    write_feed(tmp_path / "feeds", "my_signals", """
        kind: event
        timezone: America/New_York
    """, """
        ticker,available_at,strength
        QQQ,2019-03-04 17:30,0.9
        GLD,2019-03-05 11:00,0.2
        SPY,2019-03-06,0.8
        NOPE,2019-03-07 08:00,0.9
    """)
    feeds = FeedRegistry(tmp_path / "feeds", tmp_path / "no_plugins", cache, refresh=False)

    report = validate(strategy, cache_dir=cache, feeds_dir=tmp_path / "feeds", plugins_dir=tmp_path / "no_plugins")
    assert report.ok, report.text()

    result = run_backtest(strategy, {"start": "2019-03-01", "end": "2019-03-29"}, provider=provider, feeds=feeds)
    trades = result.trades.set_index("ticker")
    assert trades.loc["QQQ", "entry_date"] == pd.Timestamp("2019-03-05")   # after the close -> next open
    assert trades.loc["SPY", "entry_date"] == pd.Timestamp("2019-03-07")   # date-only -> next session
    assert (trades["exit_reason"] == "exit_after").all()
    events = result.signals[result.signals["decision"] == "event"].set_index("ticker")
    assert events.loc["GLD", "outcome"] == "rejected"
    assert events.loc["NOPE", "outcome"] == "skipped"     # no price data: logged, never silent
    assert result.metrics["data_quality"]["signals_skipped_no_data"] == 1
    assert result.manifest["universe"] == "from_feed:my_signals"
    assert result.manifest["feeds"]["my_signals"]["records"] == 4
    assert any("no price data" in line for line in result.log)

    folder = write_run_folder(result, tmp_path / "runs")
    config = json.loads((folder / "config.json").read_text())
    assert config["fields"]["universe"] == {"value": "from_feed:my_signals", "source": "strategy"}


def test_validation_reports_unknown_and_broken_feeds(tmp_path):
    strategy = tmp_path / "s.py"
    strategy.write_text(STRATEGY.replace('"my_signals"]', '"my_signals", "nope"]'))
    write_feed(tmp_path / "feeds", "my_signals", "kind: event\n", "ticker,available_at,strength\nAAA,2999-01-01,1\n")
    report = validate(strategy, cache_dir=tmp_path, quick=True, feeds_dir=tmp_path / "feeds",
                      plugins_dir=tmp_path / "no_plugins")
    rules = [(i.rule, i.line) for i in report.issues]
    assert rules == [("requires", 9), ("feed-invalid", 9)]
    assert "unknown feeds ['nope']" in report.issues[0].message
    assert "in the future" in report.issues[1].message


def test_events_work_when_price_history_reaches_the_calendars_first_session():
    # A stock listed since the 1960s starts the run's sessions on 1962-01-02.
    def on_bar(self, ctx):
        for e in ctx.events("ev"):
            ctx.order(e.ticker, pct=0.5, at="open")

    fx = make_fixture(prices={"AAA": [10.0] * 5}, start="1962-01-02",
                      events={"ev": [{"ticker": "AAA", "available_at": "1962-01-02 17:00"}]})
    strategy = make_strategy(on_bar, universe="from_feed:ev", requires=["prices", "ev"])
    assert run_on_fixture(strategy, fx).fills["session"].tolist() == [1]

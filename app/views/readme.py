"""Read Me: what Backtester is, how it is built, and how to write a strategy."""

import streamlit as st

# Keep the text to a readable line length on wide screens.
page, _ = st.columns([3, 1])

page.title("Read Me")

# ---------------------------------------------------------------------------
page.header("About Backtester", divider="gray")
page.markdown("""
Backtester answers one question: **would this trading idea have worked?** You describe a
strategy in a short Python file, choose a date range and a list of tickers, and Backtester
replays the market day by day, placing the trades your strategy asks for. You then see how
much it would have made, how much risk it took, and how it compared with simply holding
the benchmark (SPY by default).

**Who it is for.** Anyone who wants to test a rule-based idea for US stocks and ETFs on
daily prices before putting money behind it.

**Why trust the results.** Most backtests look better than reality for three reasons.
Backtester is built to avoid each one:

- **Seeing the future.** A strategy can only read prices up to yesterday's close. Any attempt
  to read today's or later prices stops the run with an error, so the result cannot rely on
  information you would not have had.
- **Free trading.** Orders fill at real open or close prices. You can add commission,
  slippage and a cap on how much of a stock's daily volume you trade.
- **Results you can't reproduce.** Each run saves its settings, a copy of the strategy code and
  every trade to its own folder. Running the same inputs again gives identical results.

Strategy files are also **validated before they run**. Every mistake is reported with its line
and a suggested fix, in a report that can be pasted straight back to whoever wrote the strategy
(a person or Claude).

**How you use it**

1. **Strategies**: every strategy file with its validation status and report. Download the
   authoring kit here to write a new one.
2. **Run**: pick a strategy, dates, tickers and settings, then start the backtest.
3. **Results**: headline numbers, the equity curve against the benchmark, drawdowns, monthly
   returns and the full list of metrics.
4. **Audit**: every trade and every signal. Select a trade to see it on a price chart with its
   entry, exit, stop, target and the event that triggered it. The Signals tab answers "why
   didn't it trade that?" without rerunning anything.
5. **Compare**: 2–4 runs side by side, out-of-sample by default, with overlaid equity curves
   and the settings that differ.
6. **Data**: every feed and its checks, the price cache's coverage, and a form to add a feed by
   uploading a file.

**Guarding against overfitting.** Trying many variants until one looks good is the easiest way
to fool yourself. Set an **in-sample end** date: develop on the data before it and judge only on
the data after it. **Walk-forward** scoring repeats that check in rolling windows to see whether
the edge holds up over time. A **confidence interval** on the mean trade return shows whether a
result could just be a few lucky trades. The Strategies page also counts how many runs each
strategy has had.

**Bring your own data.** Strategies can use more than prices. Drop a CSV of events (filings,
earnings, your own signals) or reference data (sector, shares outstanding) into
`data/feeds/<name>/` with a short `feed.yaml`, and a strategy can trade on it. Each record
counts only from the moment it became knowable, so a feed can't leak future information.

**Scope today.** Long-only US stocks and ETFs on daily bars from Yahoo Finance, plus any feeds.
Orders fill at the open or the close, and exits can be time-based, a stop or a target.
""")

page.subheader("How strategies are measured")
page.markdown("""
Every number comes from the daily equity curve (cash plus holdings, valued at each day's
close) and the trade list. Conventions: 252 trading sessions per year, a risk-free rate of 0,
and the first day's return measured against starting capital. Positions still open at the end
are valued at the last close and counted as trades.

**Benchmark.** Starting capital invested in the benchmark at the first session's open and held.
This is the same as a buy-and-hold run, so the two can be compared directly.
""")
page.markdown("""
| Group | Metric | How it is measured |
|---|---|---|
| Returns | Total return | Final equity ÷ initial capital − 1 |
| | CAGR | (1 + total return) ^ (1 ÷ years) − 1, years = sessions ÷ 252 |
| | Excess return | Strategy total return − benchmark total return |
| | Beta | Covariance of daily returns with the benchmark ÷ benchmark variance |
| | Alpha | (mean daily return − beta × mean benchmark return) × 252 |
| Risk | Volatility | Standard deviation of daily returns × √252 |
| | Sharpe | Mean daily return ÷ its standard deviation × √252 |
| | Sortino | Like Sharpe, but divides by downside deviation (losing days only) |
| | Max drawdown | Largest fall from a previous equity high |
| | Drawdown duration | Sessions from that high until equity recovers (or the run ends) |
| | Calmar | CAGR ÷ the size of the max drawdown |
| Trades | Win rate | Winning trades ÷ all trades |
| | Avg win / avg loss | Average return of winning and of losing trades |
| | Profit factor | Total profit on winners ÷ total loss on losers |
| | Expectancy | Average return per trade |
| | Avg sessions held | Average trading days from entry to exit |
| Exposure | Avg % invested | Average share of equity held in positions |
| | Turnover | (Total traded value ÷ 2) ÷ average equity, per year |
| Data quality | Skip rate | Signals skipped because the ticker had no price that day ÷ all signals |
| Confidence | Mean trade return, 95% interval | Bootstrap: resample the trade returns 10,000 times and take the middle 95% of the means |
""")
page.markdown("""
**Splits and walk-forward.** With an in-sample end date, every metric is also computed
separately for the in-sample and out-of-sample periods. Each period is measured from the equity
the run had at its start, so the two returns compound to the full-period return. Trades count in
the period they were entered. Walk-forward applies the same split in rolling folds (for example
a 3-year window, then the year after it). It reports each fold and the out-of-sample years strung
together.

**Charts.** The equity curve against the benchmark, the drawdown below each previous high, and a
year-by-month grid of returns.

**How trades are simulated**

- The strategy decides before each day's open, using data through the previous close.
- Orders fill at that day's open or close. Within a batch, sells fill before buys so that
  sale proceeds can pay for the purchases.
- Stops and targets are checked against each day's high and low. If the price gaps through
  the level at the open, the fill is at the open.
- Orders sized as a percentage or a dollar value are converted to shares at the fill price.
- Slippage moves every fill against you by the set number of basis points. Commission is per
  share, with an optional minimum per fill.
- If a buy costs more than the cash available, it is either resized to fit or rejected (a
  setting).
- If a ticker stops trading, its position is closed at its last close and flagged.
""")

# ---------------------------------------------------------------------------
page.header("Technical Design and Development", divider="gray")
page.subheader("Architecture")
page.markdown("""
The engine is a plain Python library with no UI code: **a config goes in and a run folder
comes out.** Inside, a run passes through the data layer, the daily cycle, the broker, the
ledger and analytics. The UI only starts runs and reads finished run folders, so either side
can change without touching the other.
""")
page.code("""\
Strategy file (.py) ──┐
Run config ───────────┤
Prices + feeds ───────┴──► Engine ──► Run folder
                             ▲            │
                             │            ▼
                  Streamlit UI: starts runs, reads results
""", language=None)

page.subheader("Code layout")
page.markdown("""
| Path | Responsibility |
|---|---|
| `backtester/data/` | Yahoo Finance download and local cache, price panel, point-in-time view |
| `backtester/feeds/` | Feeds: file feeds, plug-ins, built-ins, feed checks, point-in-time event and reference views |
| `backtester/calendar.py` | NYSE trading sessions (holidays and half days) |
| `backtester/strategy.py` | `Strategy` base class, `Param`, loading and checking strategy files |
| `backtester/context.py` | `ctx`, the strategy's only window onto the market and the account |
| `backtester/engine.py` | The daily cycle, the order queue, trade, order and signal records |
| `backtester/broker.py` | Fills, slippage, commission, liquidity cap, stops and targets |
| `backtester/ledger.py` | Cash, positions and per-trade (lot) accounting |
| `backtester/config.py` | Run settings and where each value came from |
| `backtester/analytics.py` | Metrics, bootstrap intervals, in/out-of-sample splits, walk-forward folds |
| `backtester/compare.py` | Side-by-side metrics, growth curves and settings differences for saved runs |
| `backtester/run.py`, `runfolder.py` | Run a backtest; write and read run folders |
| `backtester/validation.py`, `dryrun.py` | The five-stage strategy validation pipeline |
| `backtester/testing.py` | Fixture helpers for testing strategies on tiny synthetic markets |
| `backtester/kit.py` | Generates the authoring kit (`STRATEGY_API.md`, template, tests) |
| `backtester/cli.py` | The `backtest run`, `validate`, `feeds`, `kit` and `ui` commands |
| `app/` | This Streamlit UI |
| `strategies/` | Strategy files and their optional `test_<name>.py` files |
| `data/feeds/` | File feeds: one folder per feed with `feed.yaml` and data files |
| `feeds/` | Feed plug-ins (Python), for data that must be fetched |
| `kit/` | The generated authoring kit |
| `tests/` | Test suite |
""")

page.subheader("The daily cycle")
page.markdown("""
For each NYSE session in the run, the engine:

1. Closes positions in tickers that have stopped trading.
2. Queues any time-based (`exit_after`) exits that are due.
3. Delivers feed records whose `available_at` is before today's 09:30 ET open, and logs each
   event in the signal audit.
4. Calls the strategy (`on_start` first on day one, then `on_bar`). The strategy sees prices
   through yesterday's close only.
5. Fills open orders at today's open, checks stops and targets against today's range, then
   fills close orders at today's close.
6. Values every holding at today's close and records equity.
""")

page.subheader("Key design choices")
page.markdown("""
- **Lookahead is blocked by the API, not by discipline.** All price reads go through a
  point-in-time view with a cursor at the current session. Reading that session or any later
  one raises `LookaheadError`. Tests confirm that each access path is blocked.
- **Feeds are filtered on when data became knowable.** The engine uses each record's
  `available_at`, never the date the underlying thing happened. Date-only records count as
  published after the close. A `from_feed` universe grows only as records become visible.
  Every feed is checked on load: columns present, time zones set, no future timestamps,
  duplicates dropped, unknown tickers counted. Market cap uses the raw (unadjusted) close,
  because shares outstanding are reported unadjusted.
- **Nothing is skipped silently.** An event for a ticker with no price data is logged with the
  reason, and the skip rate is a headline metric.
- **Splits are slices of one run.** In-sample, out-of-sample and walk-forward results all come
  from a single continuous backtest, scored over different date ranges. No parameters are
  re-optimized between folds (optimization is out of scope for V1). The bootstrap is seeded from
  the run's seed, so results stay deterministic.
- **Adjusted prices.** Prices are adjusted for splits and dividends, so returns include
  dividends. The cache keeps Yahoo's data as fetched and derives adjusted bars when it is read.
- **Careful caching.** Each ticker is stored as Parquet under `data/cache/`. Refreshes are
  incremental and run at most every 12 hours. If Yahoo has re-adjusted past prices, the full
  history is downloaded again. Today's bar is ignored until 18:00 ET, so a partial day is never
  cached.
- **Settings precedence.** Each setting comes from the run form (or CLI), else from the
  strategy, else from the engine default. `config.json` records which level each value came
  from.
- **Lots, not just positions.** Each order with a stop, target or time exit is its own trade.
  Plain buys add to the ticker's open trade, and sells reduce trades oldest first. This keeps a
  monthly rebalance from creating a new trade every month.
- **Determinism.** No clocks, no unseeded randomness and no network calls during the run.
  `ctx.rng` is seeded from the config. `metrics.json` uses sorted keys and fixed rounding, so
  identical runs produce byte-identical files.
""")

page.subheader("Strategy validation")
page.markdown("""
Each strategy file goes through five stages. Validation stops at the first stage that fails,
and every problem in that stage is reported with its file, line, rule and a fix hint.

| Stage | Checks |
|---|---|
| Static | Reads the code without running it: one `Strategy` class, a supported `api_version`, allowed imports only, no file, network, clock or unseeded-random use, no private engine attributes |
| Declarations | The file imports cleanly; name, description, params, `requires` and universe are valid; `on_bar` exists |
| Tests | Runs the strategy's own `test_<name>.py`, if there is one |
| Dry run | 60 sessions on up to 5 tickers from cached prices (synthetic if not cached), in a separate process with a time limit |
| Determinism | A second dry run with a different hash seed must give identical output |

Every backtest also re-runs the static stage first, so a file that breaks the rules can never
run. `STRATEGY_API.md` is generated from the engine's own docstrings and validation rules, and
a test fails if the committed copy falls out of date.
""")

page.subheader("Run folder")
page.markdown("""
Each run writes `runs/<timestamp>-<strategy>/` containing `config.json` (resolved settings and
their sources), `strategy.py` with its SHA-256 hash, `equity`, `trades`, `orders`, `signals` and
`prices` as Parquet files, `metrics.json`, `data_manifest.json` (the exact data used, including
each feed's source files and their hashes) and `log.txt`.
""")

page.subheader("Development approach")
page.markdown("""
- **Tests first.** Each milestone starts from tests that define "done". The suite covers:
  - lookahead
  - fill prices
  - stops and targets
  - time exits
  - accounting invariants (cash + holdings = equity every day; trade P&L = equity change − costs)
  - costs
  - determinism
  - config resolution
  - the data cache
  - run folders
  - strategy validation, fixtures and the authoring kit
  - feeds: event timing, reference values, file and plug-in feeds, feed checks
  - splits, walk-forward folds, bootstrap intervals and run comparison
  - the UI
- **Event-timing tests.** A record available at 17:00 ET, at 10:00 ET or with only a date trades
  at the next session's open, never earlier.
- **Golden snapshots** of reference runs catch unintended behaviour changes.
- **Known-answer test.** Buy-and-hold SPY is checked against Yahoo's own return (slow test, uses
  real data). It matches to within 0.0001%.
- **Tests are checked against planted bugs.** Bugs were deliberately introduced to confirm the
  tests catch them.
- **Decisions are written down.** Every place the spec left open is recorded with its reasoning
  in `DECISIONS.md`.
- **Stack:** Python 3.14, pandas, NumPy, PyArrow, yfinance, exchange_calendars, Streamlit,
  Altair and pytest.
""")
page.code("""\
# Fast tests: synthetic data, no network
.venv\\Scripts\\python -m pytest

# Known-answer test against real SPY data
.venv\\Scripts\\python -m pytest -m slow

# Run a strategy from the command line, scored in and out of sample
.venv\\Scripts\\backtest run strategies/momentum_rotation.py \\
    --start 2015-01-01 --end 2024-12-31 --in-sample-end 2020-12-31
""", language="bash")
page.markdown("""
**Milestones.**
- All five V1 milestones are complete:
  - M1: engine and CLI
  - M2: this UI
  - M3: strategy validation and the authoring kit
  - M4: feeds and the signal audit
  - M5: run comparison, in/out-of-sample splits, walk-forward, bootstrap intervals and the Data
    screen
- Later versions (per the spec): short selling, intraday data, a dataset with delisted stocks,
  and parameter optimization.
""")

# ---------------------------------------------------------------------------
page.header("How to Write an Input Strategy in Python", divider="gray")
page.markdown("""
**Writing with Claude?** Download the authoring kit from the Strategies page and give Claude
`STRATEGY_API.md`. It is the complete contract, generated from the engine itself. Paste any
validation report back to Claude and it has what it needs to fix the file.

**1. Create a file in the `strategies/` folder**, for example `strategies/my_idea.py`. The
file must contain **exactly one** class that subclasses `Strategy`. Reload this app and it
appears on the Strategies and Run pages.

**2. Declare the strategy and write `on_bar`.** Here is a complete example: buy tickers
trading above their moving average, with a stop-loss on each entry.
""")
page.code('''\
from backtester import Param, Strategy


class TrendWithStop(Strategy):
    api_version = 1
    name = "Trend with stop"
    description = "Hold tickers above their moving average, with a stop-loss."
    requires = ["prices"]
    universe = ["SPY", "QQQ", "IWM"]
    params = {
        "ma_days": Param(int, default=50, min=10, max=200,
                         description="Moving-average length in sessions"),
        "stop_pct": Param(float, default=0.08, min=0.01, max=0.5,
                          description="Stop distance below the last close"),
        "size": Param(float, default=0.3, min=0.05, max=0.33,
                      description="Fraction of equity per position"),
    }

    def on_bar(self, ctx):
        # Closes up to yesterday; today's prices are not visible yet.
        closes = ctx.history("close", self.p.ma_days)
        if len(closes) < self.p.ma_days:
            return  # not enough history yet
        for ticker in ctx.universe:
            last = closes[ticker].iloc[-1]
            average = closes[ticker].mean()
            held = ctx.position(ticker).qty > 0
            if not held and last > average:
                ctx.order(ticker, pct=self.p.size, at="open",
                          stop=last * (1 - self.p.stop_pct), tag="trend")
            elif held and last < average:
                ctx.close(ticker, at="open")
''', language="python")

page.subheader("Required class attributes")
page.markdown("""
| Attribute | Meaning |
|---|---|
| `api_version` | Always `1` for now |
| `name` | Short name shown in the UI |
| `description` | One sentence on what the strategy does |
| `requires` | Data needed: `"prices"` plus any feeds, e.g. `["prices", "sample_signals", "market_cap"]` |
| `universe` | Default tickers, e.g. `["SPY", "QQQ"]`, the name of a list in `data/universes/<name>.txt`, or `"from_feed:<feed>"` for every ticker a feed mentions |
| `params` | Tunable settings as `Param(type, default, min=, max=, choices=, description=)`. Types: `int`, `float`, `bool`, `str`. Each one becomes a field on the Run page. Read values with `self.p.<name>` |
""")

page.subheader("Hooks")
page.markdown("""
| Hook | When it runs |
|---|---|
| `on_bar(ctx)` | **Required.** Once per session, before the open. Place orders here |
| `on_start(ctx)` | Once, before the first `on_bar` |
| `on_fill(ctx, fill)` | After each fill. `fill` has `date`, `ticker`, `qty`, `price`, `commission`, `reason` and `tag`. Orders placed here fill next session |
| `on_end(ctx)` | Once, after the last session. Orders placed here are cancelled |
""")

page.subheader("What `ctx` gives you")
page.markdown("""
**Market data** (all through yesterday's close; prices are adjusted)

- `ctx.today`: the current session date.
- `ctx.universe`: the run's tickers.
- `ctx.history(field, n, tickers=None)`: the last `n` bars as a table (rows are dates, columns
  are tickers). The field is one of `open`, `high`, `low`, `close`, `volume` or `dollar_volume`.
- `ctx.price(ticker)`: the last close.
- `ctx.bar(ticker, date)`: one past bar.
- `ctx.is_first_session_of("week" | "month" | "quarter" | "year")`: for scheduled rebalances.

**Feeds** (each feed must be listed in `requires`)

- `ctx.events(name)`: event records that became known since the last session. Each has `id`,
  `ticker`, `available_at` and the feed's columns as attributes (`e.score`).
- `ctx.ref(ticker, field)`: the latest value of a reference field known before today's open,
  e.g. `ctx.ref("AAPL", "market_cap")`.

**Account**

- `ctx.cash` and `ctx.equity`.
- `ctx.position(ticker)`: has `qty`, `avg_price` and `market_value`; `qty` is 0 if the ticker
  is not held.
- `ctx.positions`: all holdings, keyed by ticker.

**Orders** (`at="open"` fills at today's open; `at="close"` fills at today's close)

- `ctx.order(ticker, shares= | pct= | value=, at=, stop=, target=, exit_after=, tag=)`:
  - Give exactly one size: `shares`, `pct` (fraction of equity) or `value` (dollars).
    A negative size sells.
  - `stop` and `target` are price levels.
  - `exit_after=N` sells at the open N sessions after entry.
- `ctx.target_weights({"SPY": 0.6, "TLT": 0.4})`: rebalance to these weights. Holdings
  not listed are sold. Weights must add up to 1 or less.
- `ctx.close(ticker)`: sell the whole position.

**Other tools**

- `ctx.reject(ticker or event, reason)`: record a candidate you skipped and why. Pass the event
  itself, and tag orders with `tag=e.id`, so the Audit page links each event to its outcome.
- `ctx.log(message, **fields)`: write to the run's `log.txt`.
- `ctx.state`: a dict that persists between sessions.
- `ctx.rng`: a seeded random generator.
""")

page.subheader("3. Test it (optional)")
page.markdown("""
Put fixture tests in `strategies/test_<file name>.py`. A fixture is a tiny synthetic market, so
a test can check the logic exactly. Validation runs these tests with pytest.
""")
page.code('''\
from trend_with_stop import TrendWithStop

from backtester.testing import make_fixture, run_on_fixture


def test_buys_when_price_crosses_above_the_average():
    # 50 sessions at 100, then a jump to 110 on session 50.
    fx = make_fixture(prices={"SPY": [100.0] * 50 + [110.0] * 5})
    result = run_on_fixture(TrendWithStop, fx, universe=["SPY"])
    assert result.orders["session"].tolist() == [51]   # bought at session 51's open
''', language="python")
page.markdown("""
- `make_fixture(prices={...})` takes a list of closes per ticker (or a table of open, high, low
  and close). `None` means no bar that day.
- `run_on_fixture(strategy, fixture, params=..., start=..., universe=...)` returns `orders`,
  `rejects`, `fills`, `trades` and `equity` tables. Each table has a `session` column with the
  fixture day number.
""")

page.subheader("4. Validate it")
page.markdown("""
Open the **Strategies** page, or run `backtest validate strategies/my_idea.py`. Fix whatever the
report lists, then revalidate. The Run page won't run a file that fails the basic checks.
""")

page.subheader("Rules")
page.markdown("""
- Use **only `ctx`** to read prices or the account. Don't download data, read files or check
  the clock inside a strategy.
- Imports are limited to `numpy`, `pandas`, `math`, `statistics`, `datetime`, `dataclasses`,
  `typing` and `from backtester import ...`.
- Don't use `open`, `eval`, `exec`, `getattr` or similar built-ins, or attributes that start
  with `_` (except your own `self._name` attributes).
- You cannot see today's prices when deciding. Reading today or later raises `LookaheadError`.
- Use `ctx.rng` for any randomness (`numpy.random` is not allowed), so that runs are
  reproducible.
- Long-only. Sells are capped at what you hold, and weights cannot add up to more than 1.
- Share counts must be whole unless the run turns on fractional shares.
- If a ticker has no price on a day, an order for it that day is skipped and logged.
- Orders last one session. Anything not filled that day is dropped.
""")

page.subheader("Tips")
page.markdown("""
- Start from `strategies/momentum_rotation.py` (monthly rebalance with `target_weights`) or
  `strategies/buy_and_hold.py`.
- Guard against short history at the start of a run: `ctx.history` returns fewer rows when
  less data exists.
- If a run fails, the error appears on the Run page and in the run's `log.txt`.
- `strategies/test_momentum_rotation.py` is a worked example of fixture tests.
- `strategies/event_hold.py` trades on the sample feed in `data/feeds/sample_signals/`. Its
  tests show how to give a fixture events: `make_fixture(prices=..., events={"feed": [...]})`.
- You can also run a strategy from the command line, for example
  `backtest run strategies/my_idea.py --start 2015-01-01 --end 2024-12-31`.
""")

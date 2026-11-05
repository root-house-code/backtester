# Backtester V1 Spec

Oct 31, 2026

## Overview

V1 is a local Python app that runs strategy files against daily market data and event feeds, then shows every signal, trade and metric in a web UI. It is fully deterministic: no LLM is called anywhere in the engine, data pipeline or UI.

**Goals**

- Run any strategy expressible in Python against daily bars and event data, with results auditable trade by trade.
- Make lookahead bias structurally impossible through the API, not a matter of strategy discipline.
- Give strategies a strict, documented contract so Claude can write them reliably and the app can validate them.
- Make iteration fast: change parameters in the UI, rerun, compare runs side by side.

**Non-goals for V1**

- Intraday data, live or paper trading, options and futures.
- Multi-user hosting or sandboxing untrusted code. Only your own strategies run.

## V1 scope

V1 covers long-only US equities on daily bars, plus a general way to bring in any other dataset a strategy needs. No strategy or dataset is built into the design.

**In**

- Daily OHLCV bars for US stocks and ETFs
- Long positions; orders at the session open or close; time-based, stop and target exits
- A feed framework: any dataset keyed by ticker and timestamp can be added, as a Python plug-in or by dropping in a CSV or Parquet file
- Built-in reference data: point-in-time shares outstanding and market cap, since size filters are common
- Static universes (ticker lists) and dynamic ones (every ticker a feed mentions)
- Run config set in the UI: dates, universe, capital, costs, benchmark, strategy parameters
- Strategy and feed validation on load
- Saved runs, run comparison, full trade and signal audit
- In-sample/out-of-sample splits and walk-forward runs

**Out (later versions)**

- Intraday bars and intraday order types
- Short selling (V2), margin and borrow costs
- A dataset that includes delisted stocks (the provider interface is ready for one)
- Parameter optimization sweeps and a vectorized fast path
- A library of pre-built feeds. Feeds get added as strategies need them.

## Architecture

The engine is a plain Python library with no UI code: a run is a config in and a folder out.

```
Market data (prices + feeds) ─┐
Strategy file (.py) ──────────┼─► Engine: Data layer → Daily cycle → Broker simulator
Run config ───────────────────┘          → Portfolio ledger → Analytics ─► Run folder
        ▲                                                                     │
        └──────── writes ──── Streamlit UI ◄──── reads ──────────────────────┘
```

Strategy files and market data feed the engine, which runs top to bottom each session. The UI only writes run configs and reads finished run folders, so it can be replaced without touching the engine.

## Data layer

Prices are built in. Everything else a strategy needs arrives as a **feed** through one interface. Feeds are cached locally as Parquet, and the engine filters them point-in-time, so a new dataset never requires engine changes.

| Kind | What it is | Examples | Strategy reads it with |
| --- | --- | --- | --- |
| Prices | Daily OHLCV per ticker | yfinance in V1 | `ctx.history`, `ctx.price` |
| Event feed | Discrete records that appear at a moment | Insider filings, earnings releases, rating changes, a CSV of your own signals | `ctx.events(name)` |
| Reference series | A per-ticker value that changes over time | Shares outstanding, market cap, sector, index membership | `ctx.ref(ticker, field)` |

**Feed contract.** Every record has a `ticker`, an `available_at` timestamp (when it became knowable, timezone-aware), an optional `entity_id` (such as a CIK or FIGI), and any other columns the feed wants. Two ways to add a feed:

- **File import:** drop a CSV or Parquet file into `data/feeds/<name>/` with a short `feed.yaml` naming its kind and its ticker and timestamp columns. No code needed.
- **Python plug-in:** for data that must be fetched or derived, subclass `Feed`:

```python
class MyFeed(Feed):
    name = "my_feed"
    kind = "event"  # or "reference"
    columns = {"value": float, "source_url": str}

    def fetch(self, start, end, tickers=None) -> pd.DataFrame:
        ...  # must return ticker, available_at, plus declared columns
```

**Feed validation** runs when a feed is added or refreshed: required columns present, timestamps timezone-aware, no timestamps in the future, duplicates flagged, and unknown tickers counted. Results show on the Data screen.

**Rules**

- The engine filters every feed on `available_at`, never on when the underlying thing happened.
- The engine keys everything on an internal `security_id`. In V1 it equals the ticker symbol. Later, a mapping table can use `entity_id` to handle renames and reused symbols without changing strategies or feeds. Records that can't be matched are logged.
- A feed that has dates but no times gets a configurable default time. The default is after the close, so a date-only record can't be acted on the same day.
- Returns use split- and dividend-adjusted prices. The built-in market cap uses the raw close × shares outstanding, because shares are reported unadjusted.
- yfinance is unofficial and rate-limited. The cache means each ticker is fetched once, then refreshed incrementally.
- **Survivorship bias:** Yahoo mostly lacks delisted stocks. The engine never skips silently. Every signal it can't trade is logged with the reason ("no price data"), and the skip rate is a headline metric on each run.
- Each run records a data manifest (feeds, versions, fetch dates, missing tickers), so results can be tied to the exact data behind them.

## Strategy API contract

A strategy is one Python file containing one `Strategy` subclass. It touches the market only through `ctx`, which exposes only what was knowable at that moment.

Two examples show the range the same API covers: one uses prices only, the other trades off any event feed.

```python
from backtester import Strategy, Param

# Prices only: monthly momentum rotation across a fixed list
class MomentumRotation(Strategy):
    api_version = 1
    name = "Momentum rotation"
    description = "Hold the top N tickers by trailing return; rebalance monthly."
    requires = ["prices"]
    universe = ["SPY", "QQQ", "IWM", "EFA", "TLT", "GLD"]
    params = {
        "lookback": Param(int, default=126, min=20, max=252),
        "top_n": Param(int, default=2, min=1, max=6),
    }

    def on_bar(self, ctx):
        if not ctx.is_first_session_of("month"):
            return
        closes = ctx.history("close", self.p.lookback + 1)
        rets = closes.iloc[-1] / closes.iloc[0] - 1
        picks = rets.nlargest(self.p.top_n).index
        ctx.target_weights({t: 1 / self.p.top_n for t in picks}, at="open")


# Event-driven: act on records from any feed, hold N sessions
class EventHold(Strategy):
    api_version = 1
    name = "Event hold"
    description = "Buy at the open after a qualifying event; hold N sessions."
    requires = ["prices", "my_event_feed", "market_cap"]
    universe = "from_feed:my_event_feed"
    params = {
        "hold_days": Param(int, default=30, min=1, max=250),
        "cap_max": Param(float, default=2e9, min=0),
    }

    def on_bar(self, ctx):
        for e in ctx.events("my_event_feed"):
            if ctx.ref(e.ticker, "market_cap") > self.p.cap_max:
                ctx.reject(e, "above cap_max")
                continue
            ctx.order(e.ticker, pct=0.05, at="open",
                      exit_after=self.p.hold_days, tag=e.id)
```

The Form 4 idea would be the second pattern: Form 4 filings become one feed, and the filters (CFO, open-market purchase, quarter month) live in the strategy file, not the app.

**Declarations** (checked on load, used to build the UI)

| Attribute | Purpose |
| --- | --- |
| `api_version` | Contract version; unsupported versions fail loudly |
| `name`, `description` | Shown in the strategy library |
| `requires` | Feeds the engine must load, by name; unknown names fail validation |
| `universe` | Default universe: a ticker list, a named list, or `from_feed:<name>` (every ticker that feed mentions) |
| `params` | Typed parameters with defaults and ranges; become UI fields |

**The `ctx` surface**

| Call | Does |
| --- | --- |
| `ctx.today` | Current session date |
| `ctx.universe` | Tickers in scope this session |
| `ctx.history(field, n, tickers=None)` | Last n completed bars as a DataFrame |
| `ctx.price(ticker)` | Last completed close |
| `ctx.events(name)` | Records from an event feed that became visible since the last session |
| `ctx.ref(ticker, field)` | Point-in-time value from a reference series (market cap, sector, …) |
| `ctx.is_first_session_of(period)` | Calendar helper: week, month, quarter or year |
| `ctx.position(ticker)`, `ctx.positions` | Current holdings |
| `ctx.cash`, `ctx.equity` | Account values |
| `ctx.order(...)` | Queue an order: size by `shares`, `pct` or `value`; `at` open or close; optional `stop`, `target`, `exit_after`, `tag` |
| `ctx.target_weights(dict, at)` | Rebalance to target weights |
| `ctx.close(ticker)` | Exit a position |
| `ctx.reject(subject, reason)` | Record why a candidate was skipped (feeds the signal audit) |
| `ctx.log(msg, **fields)` | Structured log line |
| `ctx.state` | Dict that persists across sessions |

Optional hooks: `on_start(ctx)`, `on_fill(ctx, fill)`, `on_end(ctx)`.

**Rules for strategy code**

- Imports limited to an allowlist: numpy, pandas, math, statistics, datetime, dataclasses, typing.
- No file, network, subprocess or `eval` access.
- No randomness except `ctx.rng`, which is seeded per run.

## Execution and accounting

The strategy makes one decision per session, before the open, and its orders fill at that session's prices. This lets "buy at the open after an event" work exactly, without ever exposing that day's prices early.

**Daily cycle for session t**

1. Advance the clock. Visible to the strategy: bars through the close of t−1, and feed records with `available_at` before 09:30 ET on t.
2. Queue scheduled exits (`exit_after` reached) as open orders.
3. Call `on_bar(ctx)`.
4. Fill open orders at t's open. Check stops and targets against t's high and low. Fill close orders at t's close.
5. Mark the portfolio to t's close and record an equity snapshot.

**Fill rules**

| Case | Fill price |
| --- | --- |
| `at="open"` | Open, adjusted for slippage |
| `at="close"` | Close, adjusted for slippage |
| Stop hit | Stop price, or the open if the price gapped through it |
| Target hit | Target price, or the open if the price gapped through it |
| Stop and target both touched the same day | Assume the stop (conservative) |

**Costs and constraints** (all set in the run config)

- Trades cost nothing by default: no commission and no slippage. Both remain run-config settings, so cost sensitivity can be tested later without code changes.
- Optional liquidity cap, off by default: an order can't exceed a set percent of the ticker's 20-day average dollar volume. When on, larger orders are resized and logged.
- Whole shares only by default. Orders larger than available cash are resized or rejected (configurable) and logged.
- Definitions like "small cap" belong in strategies. The app never classifies stocks.

**Accounting**

- `exit_after` counts trading sessions, not calendar days.
- Positions are held in adjusted-share terms, so splits and dividends need no special handling.
- Quantities are signed from V1, so short selling in V2 is an addition, not a ledger rewrite.
- If a ticker stops trading mid-hold, the position closes at its last close and the trade is flagged.

## Run config and parameter precedence

Every run is fully described by one resolved config, saved with its results. That is what makes runs reproducible and comparable.

**Precedence for any value:** UI override → strategy `Param` default → engine default. The saved config records which level each value came from.

| Field | Engine default |
| --- | --- |
| `start`, `end` | Required |
| `universe` | The strategy's declared universe |
| `initial_capital` | 100,000 USD |
| `commission_per_share`, `commission_min` | 0 |
| `slippage_bps` | 0 |
| `max_adv_pct` | Off |
| `cash_shortfall` | resize |
| `benchmark` | SPY |
| `in_sample_end` | None (no split) |
| `walk_forward` | Off; window and step lengths when on |
| `params` | The strategy's declared defaults |

Costs default to zero. Set them per run to see how sensitive a strategy is to trading costs.

## Strategy authoring and validation

Claude writes strategies in a separate chat. The app's job is to catch every mistake before a full run and report it in a form that can be pasted straight back to Claude.

**Workflow**

1. The app exports an authoring kit: `STRATEGY_API.md` generated from the engine's own docstrings, a strategy template, reference strategies, and the fixture-testing helpers. Regenerated on every API change, so it can't drift from the code.
2. The kit is the knowledge for the strategy-writing Claude chat.
3. You drop the `.py` file (and optional `test_<name>.py`) into the `strategies/` folder; the app picks it up.
4. The validation pipeline runs, and the UI shows a pass or a copyable error report.
5. Errors go back to Claude; repeat until it passes.

**Validation pipeline** (stops at the first failing stage)

| Stage | Checks |
| --- | --- |
| Static | Parses; exactly one `Strategy` subclass; supported `api_version`; imports on the allowlist; no file, network, subprocess or `eval` use |
| Declarations | Name and description present; every param typed, with a default inside its range; `requires` lists only known feeds |
| Strategy tests | Runs the strategy's own fixture tests, if present |
| Dry run | 60 sessions on 5 tickers from cached data; no exceptions; every order well-formed |
| Determinism | Dry run twice; output hashes must match |

Each error names the file, line, rule broken and a one-line fix hint.

**Fixture testing:** `make_fixture(prices=..., events=...)` builds a tiny synthetic market, and `run_on_fixture(strategy, fixture)` returns its orders and rejects. This lets a strategy's logic be checked exactly ("this filing should trigger a buy on day 3") without real data.

The static checks guard against mistakes, not malice. That's adequate because only your own strategies run.

## Run outputs and metrics

Each run writes a self-contained folder under `runs/<timestamp>-<strategy>/`. The UI only reads these folders, so any run can be reopened or compared later.

| File | Contents |
| --- | --- |
| `config.json` | Resolved config, with the source of each value |
| `strategy.py` | Exact code that ran, plus its hash |
| `equity.parquet` | Daily equity, cash, exposure and benchmark value |
| `trades.parquet` | Entry and exit dates and prices, shares, P&L, return, sessions held, max adverse and favorable excursion, exit reason, tag |
| `signals.parquet` | Every candidate the strategy considered: ordered or rejected, with the reason |
| `orders.parquet` | Every order, including resized and rejected ones |
| `metrics.json` | Summary metrics |
| `data_manifest.json` | Data sources, fetch dates, missing tickers |
| `log.txt` | Engine and strategy logs |

**Metrics** (shown for the full period, and in-sample vs. out-of-sample when split)

- **Returns:** total return, CAGR, excess return vs. benchmark, alpha, beta
- **Risk:** volatility, max drawdown and its duration, Sharpe, Sortino, Calmar
- **Trades:** count, win rate, average win and loss, profit factor, expectancy, average sessions held
- **Exposure:** average percent invested, turnover
- **Confidence:** bootstrap 95% interval for mean trade return, so a handful of lucky trades doesn't read as an edge
- **Data quality:** signals skipped for missing data, as a count and a share of all signals

## UI screens

V1 is a local Streamlit app with six screens. Because the engine is a library and runs are folders on disk, moving to FastAPI + React later means rewriting only this layer.

| Screen | Shows | Actions |
| --- | --- | --- |
| Strategy library | Every strategy file, its validation status and API version | Revalidate; copy the error report |
| Run setup | Form auto-built from standard fields plus the strategy's params | Run; save as a preset |
| Results | Equity vs. benchmark, drawdown, metrics, monthly returns heatmap, in/out-of-sample split | Open the audit |
| Trade and signal audit | Filterable trades and signals. Clicking a trade opens a price chart with entry, exit and the triggering event marked | Export CSV |
| Compare runs | 2–4 runs: metrics side by side, overlaid equity curves, config diff | Pick runs |
| Data | Registered feeds and their validation results, cache coverage, missing tickers, last fetch dates | Add or refresh a feed |

The signal audit is the most important screen for iterating. It answers "why didn't it trade that?" without rerunning anything.

## Correctness and testing

The engine is trusted only if its test suite would catch the classic backtest bugs. Each item below is an automated test that runs on every engine change.

- **Known answer:** buy-and-hold SPY with zero costs matches SPY's adjusted return to within 0.01%.
- **Lookahead canary:** a strategy that reads a bar or event not yet visible must raise an error.
- **Event timing:** a fixture event available at 17:00 ET fills at the next session's open. One available at 10:00 ET also fills at the next session's open, never at the open that already happened that morning. A date-only record never trades the same session.
- **Accounting invariants:** cash plus position value equals equity every session. Total trade P&L equals the equity change minus costs.
- **Determinism:** the same config and data produce the same metrics hash.
- **Cost sanity:** a zero-cost run never underperforms the same run with costs.
- **Golden runs:** reference strategies with stored outputs; any change in their results is flagged for review.

**Overfitting guards:** the in/out-of-sample split and walk-forward runs are built in from V1. The strategy library also shows how many runs each strategy has had, as a reminder of how many variants have been tried.

## Milestones

Five milestones, each usable on its own. Prices-only strategies work after M2; event strategies after M4.

1. **Engine core on prices:** yfinance cache, Strategy API, daily cycle, fills, ledger, metrics, run folders, and a CLI runner.
   - Done when: two reference strategies (buy-and-hold, momentum rotation) pass the known-answer, invariant and determinism tests.
2. **Results UI:** Streamlit run setup, results, and trade audit screens.
   - Done when: a run can be launched from the UI and any trade opened on a chart.
3. **Authoring kit and validation:** validation pipeline, fixture helpers, generated `STRATEGY_API.md`, template, strategy library screen.
   - Done when: a deliberately broken strategy produces an error report Claude can fix in one round.
4. **Feed framework:** `Feed` interface, CSV/Parquet import, feed validation, built-in shares outstanding and market cap, `from_feed` universes, signal audit.
   - Done when: a feed added only by dropping in a file drives a strategy end to end, and the event-timing tests pass.
5. **Iteration tools:** compare runs, in/out-of-sample split, walk-forward, bootstrap intervals, data screen.
   - Done when: two variants of one strategy can be compared side by side on out-of-sample results.

## Decisions

All V1 open questions are closed.

| Question | Decision |
| --- | --- |
| Ticker identity | Engine keys on an internal `security_id`, equal to the symbol in V1. Feeds may carry an optional `entity_id` for later mapping. |
| Which feeds to build first | None up front. Every dataset starts as a file import and becomes a plug-in only when it needs regular refreshes or derived fields. |
| Trading costs | Zero by default (no commission, no slippage). The settings stay in the run config. |
| Stock classifications (e.g. small cap) | Defined in strategies, never in the app. |
| Short selling | V2. Signed quantities in the V1 ledger make it an addition. |
| Delisted-stock dataset | Buy only when a strategy survives out-of-sample on Yahoo data and is a candidate to trade. |
| UI framework | Streamlit for V1. After M2, switch to FastAPI + React only if the trade drill-down feels clunky. |

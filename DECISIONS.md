# Decisions

These are the places where the spec left something open. Where there was a choice, the more conservative option was taken. Each entry gives the decision and the reason for it.

## Execution

1. **Known-answer convention.** Buy-and-hold buys at the first session's open and is valued at the last session's close. The SPY reference return in the test uses the same window (adjusted open on day one to adjusted close on the last day). The known-answer test turns on `fractional_shares`, since whole shares leave cash uninvested and the result would miss 0.01% on that alone. Measured gap: under 0.0001%.
2. **The benchmark follows the same convention.** Its value is capital invested at the first session's open, marked at each close, so it is directly comparable to a buy-and-hold run.
3. **Stops and targets are checked on the entry day for `at="open"` entries.** If the entry day's low touches the stop, the trade is stopped out that day. Entries `at="close"` are first checked the next session.
4. **Slippage applies to stop and target fills too**, not only to open and close orders.
5. **`pct`, `value` and `target_weights` orders are sized when they fill.** Shares = notional ÷ fill price (after slippage). Equity is measured at that batch's prices (the open for open orders, the close for close orders). Fixed `shares` orders are never re-sized except for cash or liquidity limits. Without this, a gap down would leave cash idle on every rebalance and a gap up would hit a cash shortfall.
6. **Order of fills within a batch:** explicit sells and engine exits, then target-weight sells (computed after those exits), then all buys, so sale proceeds pay for purchases.
7. **Orders last one session.** Anything not filled is skipped and logged, and there are no good-till-cancelled orders. Engine exits are the exception: an `exit_after` exit that cannot fill because the ticker has no bar that day is retried each session.
8. **Orders placed in `on_fill` fill next session.** Orders placed in `on_end` are cancelled.
9. **`exit_after=N`** exits at the open of the Nth trading session after the entry session, whether the entry was at the open or the close. `sessions_held` is the number of sessions between entry and exit.
10. **A ticker stops trading** when it has no bars after some date while other loaded tickers still do. At the start of the next session its open position is closed at its last close. Commission applies but slippage does not, and the trade is flagged `stopped_trading`. If every ticker's data ends at the same date, nothing is treated as stopped; the run end is clipped to the last data date instead, with a warning in the log.
11. **Trade records.** Each order with exit rules (stop, target or `exit_after`) opens its own trade. Plain buys add to the ticker's open plain trade (scaling in). Sells without a specific trade target reduce trades oldest first. A trade is complete when its shares reach zero. This keeps monthly rebalancing from creating a new "trade" each month.
12. **Long-only.** A sell larger than the position is reduced to the position size and logged. Stops, targets and `exit_after` are rejected on sell orders.
13. **Liquidity cap (`max_adv_pct`)** is given in percent (5 means 5%) and applies to buys only. It needs 20 completed sessions of dollar volume; without them the order is rejected rather than allowed through uncapped.
14. **Commission** per fill is `max(commission_per_share × shares, commission_min)`, charged only when commission is configured.
15. **Cash tolerance.** A buy may exceed cash by up to $0.000001 to absorb floating-point rounding.

## Strategy API

16. **Added `ctx.bar(ticker, date)`**, a date-based read of one completed bar. The spec's lookahead canary requires a way for a strategy to *ask* for a bar that is not yet visible; `ctx.bar` raises `LookaheadError` for today or any later date. `ctx.history` and `ctx.price` go through the same guard.
17. **Lookahead is blocked at the `ctx` API.** Python cannot hide private attributes, so a strategy could still dig into `ctx._engine`. The M3 static checks should reject any access to underscore attributes.
18. **`ctx.history`** returns NaN where a ticker had no bar and returns fewer rows when history is short. **`ctx.price`** carries the last known close forward.
19. **Price history before `start` is visible** (bars before the run starts are already in the past), so lookback strategies have data from the first session. The cache always holds each ticker's full history.
20. **`is_first_session_of`** compares today with the previous NYSE session, even when that session is before the run start. A run starting mid-month does not count its first day as the first of the month.
21. **`pct`** must be in [-1, 1], and **`target_weights`** must be ≥ 0 and sum to at most 1 (no leverage in V1).
22. **`ctx.universe`** is the static run universe in V1. Dynamic universes arrive with feeds in M4.
23. **Universe names** (a string `universe` that is not `from_feed:`) are read from `data/universes/<name>.txt`, one ticker per line.

## Config

24. **Source labels** are `override`, `strategy` and `engine`. "Override" covers the CLI now and the UI in M2.
25. **Added engine settings** beyond the spec table: `fractional_shares` (default off; the spec says whole shares by default) and `seed` (default 0, seeds `ctx.rng`).
26. **Setting `in_sample_end` or `walk_forward` fails loudly** until M5, rather than being silently ignored.

## Data

27. **The cache stores Yahoo's columns as fetched.** These are the split-adjusted OHLC, the adjusted close, volume, dividends and splits. Adjusted bars are derived when the cache is read (OHLC × adjusted close ÷ close). The raw close is rebuilt from split events.
28. **Refresh is incremental with a safety check.** Each refresh re-fetches the last ~10 days and compares them with the cache. If Yahoo has re-adjusted history (overlapping rows changed, or a new dividend or split appeared), the full history is fetched again so adjusted prices stay consistent.
29. **Rate limiting.** Each ticker is refreshed at most every 12 hours. Today's bar is ignored until 18:00 ET, so a partial intraday bar is never cached. `--no-refresh` runs from the cache only.
30. **Dollar volume** (for the liquidity cap) is the split-adjusted close × split-adjusted volume, which equals the raw dollar volume.

## Metrics

31. **Return conventions.** Returns are daily, from the equity curve, and the first day is measured against initial capital. Annualization uses 252 sessions per year, and the risk-free rate is 0 for Sharpe, Sortino and alpha.
32. **Trade metrics include positions still open at the end**, valued at the last close and flagged `is_open`. Average win and loss are trade returns; the `_pnl` variants are in dollars.
33. **Drawdown duration** is counted in sessions from the peak before the deepest drawdown to the recovery, or to the end of the run if equity never recovered.
34. **Turnover** is annualized: (total traded value ÷ 2) ÷ average equity ÷ years.
35. **`metrics.json`** rounds floats to 10 significant digits and sorts keys, so identical runs give byte-identical files. It contains no timestamps or paths.

## UI (M2)

36. **The UI launches runs through the engine library and then reads the run back from its folder**, never from memory. Pages show only what is on disk, so any saved run can be reopened later.
37. **The run form sends only the values that differ from their defaults.** Everything else is recorded in `config.json` as a strategy or engine default, so the source labels stay accurate.
38. **Added `prices.parquet` to the run folder.** It holds adjusted OHLC for every traded ticker over the run, plus 60 sessions before it. Trade charts are drawn from it, so they always show the same prices the run traded at, even after Yahoo re-adjusts history.
39. **Added `stop` and `target` columns to `trades.parquet`**, so the trade chart can draw those levels.
40. **Scaled-in trades** are marked on the chart at their first entry date and average entry price.
41. **Run presets ("save as a preset") are deferred.** They are listed for Run setup in the spec but are not needed for M2's done-when, and leaving them out keeps the first UI minimal.

## Validation and the authoring kit (M3)

42. **The import allowlist is exactly the spec's list** (numpy, pandas, math, statistics, datetime, dataclasses, typing), plus `__future__`. `backtester` may be imported only as `from backtester import ...`, and `numpy.random` is banned in favour of `ctx.rng`. Widening the list later is one constant in `validation.py`.
43. **Static rules beyond the spec.** These close the `ctx._engine` gap from decision 17:
    - No private attribute access, except the strategy's own `self._name` attributes and `super().__init__`. Dunder attributes are banned outright.
    - `getattr`, `setattr`, `delattr`, `vars`, `globals` and `locals` are banned, since they can reach the same attributes by name.
    - Wall-clock calls (`.now()`, `.today()`, `.utcnow()`) are banned, as are pandas `read_*` and DataFrame `to_*` file methods.
44. **Every issue in a failing stage is reported, not just the first**, so one round of fixes clears the stage. The pipeline still stops at the first failing stage, as the spec says.
45. **Where validation runs.**
    - Every run of a strategy file (CLI or UI) applies the static stage first and refuses the run if it fails.
    - The Run page also checks declarations and hides the Run button for a failing file.
    - The full pipeline runs on the Strategies page and through `backtest validate`.
46. **Dry run data.**
    - The dry run uses the last 60 cached sessions for the first five tickers of the strategy's default universe, with default params and the first ticker as benchmark.
    - If any of those tickers is not cached, all of them get deterministic synthetic prices, and the report says so.
    - Validation never fetches data, so it works offline.
47. **Dry runs run in child processes** with a 180-second timeout, so a runaway strategy cannot hang the app. The two runs use different `PYTHONHASHSEED` values, so the determinism stage also catches dependence on string-hash or set ordering. Both start in parallel with the tests stage.
48. **"Every order well-formed."** Malformed order arguments already raise `OrderError` at the call, which the dry run reports with its line. The dry run also fails orders for tickers outside the universe, pointing at the first line that names the ticker.
49. **Strategy tests** are `test_<name>.py` next to the strategy, run by pytest in a subprocess from the strategies folder. The test file imports the strategy by module name (`from my_strategy import MyStrategy`). Without a test file, the stage is skipped. pytest is now a core dependency.
50. **Fixtures.**
    - `make_fixture` takes a list of closes (flat bars) or an OHLC frame per ticker. `None` or NaN means no bar that session.
    - Sessions follow the NYSE calendar from `start`.
    - `events=` raises until feeds arrive in M4.
    - `run_on_fixture` returns frames with a `session` number column. By default, the universe is every fixture ticker and the benchmark is the first one.
51. **STRATEGY_API.md is generated** into `kit/` along with the template and its tests, and committed. A test fails if the committed files differ from what the generator produces now. That is how the spec's "regenerated on every API change" is enforced. The Strategies page offers the kit as a zip.
52. **Strategy library.**
    - Validation results are cached in memory by the contents of the strategy file and its test file. Revalidate forces a rerun.
    - The Runs column counts saved runs by strategy file name.
    - Files starting with `test_` or `_` are not strategies.
53. **The momentum rotation strategy got fixture tests** (`strategies/test_momentum_rotation.py`). They are a worked example for the kit and exercise the tests stage.

## Feeds (M4)

54. **When a record becomes visible.**
    - A record becomes visible at the first session whose 09:30 ET open is strictly after its `available_at`, as the spec's "before 09:30 ET on t" requires. Half-day sessions still open at 09:30.
    - Records from before the previous session's open are old news. They count for `ctx.ref` and for `from_feed` universes, but are never delivered by `ctx.events`.
55. **Timestamps.**
    - Date-only values get the feed's `default_time` (18:00 unless set) in its `timezone` (US/Eastern unless set), so they can never trade the same session.
    - Values with a time but no UTC offset need an explicit `timezone:`; otherwise the feed fails validation. This is the spec's "timestamps timezone-aware" check.
    - Naive midnight datetimes in Parquet files count as date-only.
56. **Feed checks.**
    - A feed that fails its checks (missing columns, missing or unreadable values, no time zone, future timestamps) stops the run with the check report.
    - Exact duplicate records are dropped with a warning.
    - Tickers without cached prices are counted as a warning, because they may be fetched on the first run.
57. **Record ids** come from `id_column` if the feed has one. Otherwise each id is a hash of the record's contents, which stays the same if the file is reordered.
58. **Signal audit.**
    - Every record of a required event feed is logged as a signal (`decision` "event") on the session it is delivered, whether or not the strategy reads it.
    - An event's outcome comes from the order or reject that used its id as the tag: the order's status, "rejected" with the reason, or "no action". Reject rows now carry the outcome "rejected".
    - The skip-rate metric counts orders and rejects only, not events.
59. **`from_feed` universes.**
    - `ctx.universe` is every ticker in records visible so far, in order of first mention. Tickers the feed mentions later never appear early.
    - Prices are loaded for every ticker mentioned before the run's end.
    - A feed ticker with no price source is logged and its orders are skipped. A static universe still fails on a missing ticker, as before.
60. **`ctx.ref` fields** are the value columns of the required reference feeds. A name two feeds share must be written `feed.column`. `market_cap` is the last visible raw close times the latest visible shares outstanding, and NaN until both are known.
61. **`shares_outstanding`** comes from yfinance `get_shares_full`. Yahoo gives no publication time, so each value counts as known at 18:00 ET on its date, which is an approximation. Values are cached per ticker under `data/cache/shares/` and refreshed at most every 12 hours.
62. **Feed caching.**
    - File feeds are cached as normalized Parquet under `data/cache/feeds/`. The cache is keyed by `feed.yaml` plus the data files' names, sizes and modification times.
    - Plug-in feeds are cached with their fetch time and end date. They are fetched again when the cache is older than 12 hours or a run needs a later end.
    - Offline runs and validation use the cache only, and fail with a hint if it does not cover the run.
63. **Plug-ins live in `feeds/`**, one `Feed` subclass per file. They fetch data, so the strategy static rules do not apply to them. Feed names must be unique across file, plug-in and built-in feeds, and `prices` is reserved.
64. **Dry runs with event feeds.**
    - The 60-session window ends five sessions after the latest event, capped at the last completed session.
    - A `from_feed` strategy is tested on the five tickers with the most records in that window, and event records are cut down to the chosen tickers.
    - The declarations stage also checks that every required file feed passes its data checks.
65. **UI.**
    - The Trades page is now **Audit**, with Trades and Signals tabs (filters and CSV export on both).
    - The trade chart marks when a trade's triggering event became known, linked by tag.
    - The Data screen stays in M5, where the milestone list puts it. `backtest feeds` is how to check feeds in M4.
66. **Fixtures** take `events=` and `refs=`. Supplying `shares_outstanding` also makes `market_cap` work.
67. **Worked example.** `data/feeds/sample_signals/` (made-up data) and `strategies/event_hold.py`, with fixture tests, are the reference for event strategies and the done-when demonstration.

## Iteration tools (M5)

68. **Splits and walk-forward are slices of one continuous run** (chosen over fresh runs per fold). Each slice is measured from the equity, and benchmark value, the run had just before it, so the in-sample and out-of-sample returns compound exactly to the full-period return. A position held across a boundary carries over, as it would in real trading.
69. **Trades belong to the slice they were entered in**, with their full result even if they exit later. Fills and signals belong to the slice of their date.
70. **`in_sample_end`** is the last in-sample date. It must fall on or after the start and before the end, so both periods have sessions.
71. **`walk_forward` is rolling, with no parameter optimization** (which V1 rules out). It is set as `"window,step"` lengths such as `2y,6m`, `13w` or `90d`, measured in calendar time.
    - Fold k's in-sample window starts k steps after the first session. Its out-of-sample period is the step that follows, and the last step is cut at the run's end.
    - The summary strings the out-of-sample steps together and reports the share of folds with positive out-of-sample returns, plus mean Sharpe in-sample and out-of-sample.
    - A walk-forward that fits no complete fold logs a warning instead of failing.
72. **The confidence interval** is a percentile bootstrap: 10,000 resamples of the mean trade return, at the 95% level. It is seeded from the run's seed, so it is deterministic. It needs at least two trades, is computed for every period, and is reported in `metrics.json` under `confidence`. Golden snapshots were regenerated for this new key; no existing values changed.
73. **Compare** works only from run folders.
    - Pick 2–4 runs and a period. Out-of-sample is the default whenever any run has a split. Runs without the chosen period show "–".
    - Equity curves are rescaled to $1 at the start of the period.
    - "Settings that differ" flattens each run's resolved config and params, plus the strategy file and a short hash of its code, and lists only the keys whose values differ.
74. **Data page.**
    - Lists every feed with its check results and last fetch, plus the price cache's coverage per ticker, including tickers Yahoo has no data for.
    - "Add a feed" checks an uploaded file with the chosen settings before writing anything. Only a file that passes is saved to `data/feeds/<name>/`, with a generated `feed.yaml`.
    - Refresh re-reads a file feed or fetches a plug-in feed again.
75. **Page order:** Strategies, Run, Results, Audit, Compare, Data, Instructions, Read Me. Run stays the default page.

## Writing strategies with Claude

76. **`CLAUDE.md` and the `/new-strategy` skill** (`.claude/skills/new-strategy/`) encode the authoring loop:
    - agree a brief with the user;
    - write fixture tests before code;
    - validate until clean;
    - develop on in-sample data only;
    - make one out-of-sample run, with the user's go-ahead.
77. **Development runs end at the in-sample end date** (`--end`) instead of using `--in-sample-end`, so out-of-sample results are never computed, let alone seen, while the strategy is being tuned.
78. **Each strategy has a research log** at `strategies/<name>.md`. It records the brief, a failure criterion set before any results, the fixed dates and costs, every variant with its reason, and whether out-of-sample has been viewed. Only `*.py` files there are treated as strategies.
79. **The benchmark is tradable.** `ctx.benchmark` names the run's benchmark ticker. Its prices are always loaded, so a strategy may trade it even when it isn't in the universe, for example to keep idle cash in the index it is judged against. The dry run sets the benchmark to one of the universe's tickers, so its order check is unchanged.
80. **A strategy can state what it tests** in an optional `hypothesis` class attribute: one or two plain sentences with the specifics, taken from the brief. The Results page shows it under the strategy's name.
    - It lives in the strategy file, so each run folder's saved `strategy.py` carries the version that ran. The page reads it from that copy without importing it (`runfolder.strategy_hypothesis`, an AST read).
    - Runs saved before their strategy had one fall back to the current strategy file, so older runs show it too.
    - It is optional: validation only checks that it is a string when present. The engine and run outputs are unchanged.
81. **Runs can be sorted** in the Run box on Results and Audit, by Newest (the default) or a full-period headline number: total return, CAGR, return vs benchmark, Sharpe, Sortino, max drawdown (shallowest first) or trades. Best comes first; runs without the number go last. The label gains the sorted-by value unless it already shows it. The choice is kept across pages, and re-sorting keeps the run being viewed.

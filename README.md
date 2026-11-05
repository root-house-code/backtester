# Backtester

A deterministic daily-bar backtesting engine. See `SPEC.md` for the design and `DECISIONS.md` for choices made where the spec was open.

## Setup

```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

## Run a strategy

```
.venv\Scripts\backtest run strategies/momentum_rotation.py --start 2015-01-01 --end 2024-12-31
```

Options: `--tickers SPY,QQQ`, `--capital 100000`, `--param lookback=126` (repeatable), `--benchmark`, `--commission-per-share`, `--commission-min`, `--slippage-bps`, `--max-adv-pct`, `--cash-shortfall resize|reject`, `--fractional`, `--seed`, `--in-sample-end 2020-12-31`, `--walk-forward 3y,1y`, `--no-refresh`.

Each run writes a folder under `runs/`, holding the config, the strategy code, equity, trades, signals, orders, metrics, the data manifest and the log.

## Guard against overfitting

- `--in-sample-end DATE` scores the run separately before and after that date. Develop the strategy on the in-sample period and judge it on the out-of-sample one.
- `--walk-forward 3y,1y` scores the run in rolling folds: each 3-year in-sample window, then the year after it.
- Every run reports a bootstrap 95% confidence interval for the mean trade return, so a handful of lucky trades doesn't read as an edge.
- The Compare page puts 2–4 runs side by side (out-of-sample by default) and shows which settings differ. The Strategies page counts how many runs each strategy has had.

## Write and validate a strategy

```
.venv\Scripts\backtest kit                                   # writes kit/: STRATEGY_API.md, a template and its tests
.venv\Scripts\backtest validate strategies/my_strategy.py    # full validation report
```

Give `kit/STRATEGY_API.md` to whoever writes the strategy (it is the complete contract). Save the strategy to `strategies/`, with optional fixture tests in `strategies/test_<name>.py`. With Claude Code, `/new-strategy <idea>` runs the whole loop (brief, tests first, validation, in-sample tuning, one out-of-sample run) and keeps a research log at `strategies/<name>.md`; `CLAUDE.md` holds the rules. Validation runs five stages (static checks, declarations, the strategy's tests, a 60-session dry run and a determinism check) and prints a report with the file, line, rule and fix for each problem.

## Add data with feeds

Anything beyond prices arrives as a feed. To add one, drop CSV or Parquet files into `data/feeds/<name>/` next to a `feed.yaml` (see `data/feeds/sample_signals/` for an example). A strategy names the feed in `requires`, then reads it with `ctx.events(name)` for event feeds or `ctx.ref(ticker, field)` for reference feeds. `universe = "from_feed:<name>"` trades every ticker the feed mentions. Data that has to be fetched goes in a Python plug-in in `feeds/` instead. Built-in feeds: `shares_outstanding` and `market_cap`.

```
.venv\Scripts\backtest feeds              # list every feed and check its data
.venv\Scripts\backtest feeds refresh NAME # fetch a plug-in feed again
```

Every record has an `available_at` time. A record becomes visible to the strategy at the first session that opens after it, so nothing can be acted on before it was knowable. `strategies/event_hold.py` is a worked example.

## UI

```
.venv\Scripts\backtest ui
```

This opens the Streamlit app in your browser. **Strategies** lists every strategy file with its validation status and report, and offers the authoring kit. **Run** launches a backtest and **Results** shows its metrics and charts. **Audit** lists every trade and every signal (feed events, orders and rejects, each with its outcome), with CSV export; select a trade to see it on a price chart. **Compare** puts 2–4 runs side by side. **Data** shows every feed and its checks, the price cache's coverage, and lets you add a feed by uploading a file. **Instructions** walks through using Backtester step by step. **Read Me** explains the project, how strategies are measured, the technical design, and how to write a strategy.

## Tests

```
.venv\Scripts\python -m pytest            # fast tests, synthetic data, no network
.venv\Scripts\python -m pytest -m slow    # known-answer test against real SPY data
```

To regenerate the golden snapshots after an intended change, run `UPDATE_GOLDEN=1 pytest tests/test_golden.py`, then review the diff.

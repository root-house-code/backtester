---
name: new-strategy
description: Write, test, validate and evaluate a new Backtester strategy from a trading idea, following an in-sample / out-of-sample discipline. Use when the user asks for a new strategy file, describes a trading idea to backtest, or types /new-strategy.
argument-hint: "<trading idea in plain words>"
---

# New strategy

Turn the user's idea into a validated strategy in `strategies/`, develop it on in-sample data only, then judge it once out-of-sample. The idea: $ARGUMENTS

Commands below use Git Bash paths (`.venv/Scripts/backtest`). Run them from the project root.

## 1. Brief

Fill in the brief from what the user said. For anything essential that's missing (marked *), ask the user in **one** round of questions, offering a sensible default for each. Don't invent the hypothesis or the rules.

| Field | What it holds | Default if not given |
|---|---|---|
| Name* | snake_case file name; check `strategies/` for a clash | from the idea |
| Hypothesis* | why the edge should exist: who is on the other side, what behaviour or flow it exploits | none, ask |
| Universe* | ticker list or `from_feed:<feed>` | none, ask |
| Data | `prices` plus any feeds; run `backtest feeds check <name>` and read the feed's `feed.yaml` | prices only |
| Entry* / Exit* | exact rules, with the sessions they read and when orders fill (open or close) | none, ask |
| Sizing | per position, max positions, what happens when cash runs short | equal weight |
| Params | each with default, min and max. Keep to 3 or fewer | |
| Dates | start, in-sample end, final end | 2010-01-01, 2019-12-31, the last complete session |
| Costs | slippage and commission, fixed for all runs | `--slippage-bps 5` |
| Fails if | what would show the idea doesn't work, decided **now**, before any results | out-of-sample Sharpe below 0.5, or the mean-trade 95% CI includes zero |

Check that the start date leaves enough history for the longest lookback, and that the universe has prices that far back (the Data page, or `data/cache/`).

Show the brief to the user and get a yes before writing code: the rules become the tests. Then create the research log `strategies/<name>.md`:

```markdown
# <Strategy name>

## Brief
<the table above, filled in>

## Fixed settings
Start <date> · In-sample end <date> · Final end <date> · Costs <flags>
Out-of-sample viewed: no

## Variants
| # | Date | Change and reason (written before running) | CAGR | Sharpe | Max DD | Trades | Mean-trade CI | Run folder |
|---|------|--------------------------------------------|------|--------|--------|--------|---------------|------------|
```

## 2. Write it, tests first

1. Read **all** of `kit/STRATEGY_API.md`, then the closest reference strategy and its test (`strategies/momentum_rotation.py` for price-based rotation, `strategies/event_hold.py` for feed events).
2. Write `strategies/test_<name>.py` with one fixture test per rule in the brief:
   - the entry fires when it should, and doesn't when it shouldn't;
   - the exit;
   - sizing;
   - too little history (no orders);
   - for feed strategies, the timing of when an event becomes visible.

   Keep fixtures tiny and hand-computable, and note the expected numbers in comments.
3. Write `strategies/<name>.py`. Set `description` to one sentence that states the rule. Set `hypothesis` to the brief's hypothesis in one or two plain sentences: the question being tested, with its specifics (tickers, signal, hold) and why it should work. The Results page shows it under the strategy's name. Use `ctx.reject(subject, reason)` for every candidate the strategy deliberately passes on, so the Signals audit explains it.

## 3. Validate until clean

Run `.venv/Scripts/backtest validate strategies/<name>.py`, fix what it reports, and rerun until it passes.

- Fix the strategy, not the rule. Edit a test only if it misstates the brief, and tell the user when you do.
- If the same failure survives three attempts, stop and show the user the report.

## 4. In-sample runs

```text
.venv/Scripts/backtest run strategies/<name>.py --start <start> --end <in-sample end> <costs> [--param k=v] [--walk-forward 3y,1y]
```

- **Never** pass an `--end` after the in-sample end, and don't open other full-period runs of this strategy in `runs/`. Walk-forward inside the in-sample period is fine.
- After each run, check that it behaved as intended before looking at returns:
  - the trade count and holding period are plausible;
  - the skip rate is near zero;
  - exposure matches the sizing.

  Skim the trades and signals in the run folder, for example `.venv/Scripts/python -c "import pandas as pd; print(pd.read_parquet('runs/<folder>/signals.parquet').head(40).to_string())"` (also `trades.parquet`, `orders.parquet`; the full metrics are in `metrics.json`). A surprise here is a bug, not a result.
- Add a row to the Variants table for every run, with the run folder name.

## 5. Variants

Try a variant only when there's a reason for it.

- Write the change and its reason in the log **before** running it.
- Prefer `--param` overrides to code edits.
- Don't grid-search parameters.
- After 5 variants, stop and check in with the user. Each extra variant makes the best result less trustworthy.

## 6. The out-of-sample run (once)

Ask the user before running this. It can only be done honestly once.

```text
.venv/Scripts/backtest run strategies/<name>.py --start <start> --end <final end> --in-sample-end <in-sample end> --walk-forward 3y,1y <costs> [chosen params]
```

1. In the log, set `Out-of-sample viewed: <date>, run <folder>`, and add the final row.
2. Report to the user, plainly and briefly:
   - In-sample against out-of-sample: CAGR, Sharpe, max drawdown and trades, plus the benchmark over the same period.
   - Walk-forward: the share of positive folds, and mean out-of-sample Sharpe against in-sample.
   - The mean-trade 95% CI, and whether it includes zero.
   - How many variants were tried.
   - **Verdict against the brief's "Fails if" line.** A strategy that fails it, fails, even if it looks good elsewhere.
3. Point the user at the Results and Compare pages for the charts.

Any change after this point is a new experiment. Log it as "after out-of-sample", and tell the user that the out-of-sample result no longer counts as unseen.

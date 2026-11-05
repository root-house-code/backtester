# Backtester

Deterministic daily-bar backtester for US stocks and ETFs. `SPEC.md` is the design, `DECISIONS.md` records every choice the spec left open (add to it when you make one), `README.md` covers usage.

## Commands

Windows, Python 3.14 venv in `.venv`. In Git Bash use forward slashes (`.venv/Scripts/backtest`); in PowerShell, backslashes.

```text
.venv/Scripts/python -m pytest                      # fast tests, no network
.venv/Scripts/python -m pytest -m slow              # known-answer test on real SPY data
.venv/Scripts/backtest validate strategies/X.py     # full validation report (exit code 1 on failure)
.venv/Scripts/backtest run strategies/X.py --start 2010-01-01 --end 2019-12-31 [--param k=v] [--slippage-bps 5]
.venv/Scripts/backtest feeds [check|refresh] [names]
.venv/Scripts/backtest kit                          # regenerate kit/ after changing docstrings or validator rules
.venv/Scripts/backtest ui                           # Streamlit app on localhost:8501
```

## Layout

- `backtester/`: engine, ctx API, validation, feeds, analytics, CLI. `app/`: Streamlit UI (views in `app/views/`).
- `strategies/<name>.py` plus `test_<name>.py` (fixture tests) and `<name>.md` (research log, see below).
- `kit/STRATEGY_API.md`: the complete strategy contract, generated from code. Never edit `kit/` by hand; a drift test fails if it is stale.
- `data/feeds/<name>/`: file feeds (`feed.yaml` plus CSV/Parquet). `runs/`: one folder per run (git-ignored).

## Conventions

- The UI stays minimalist: plain Streamlit, no decoration.
- After an intended change to engine output, regenerate golden snapshots with `UPDATE_GOLDEN=1 pytest tests/test_golden.py` and review the diff.
- Commit only when asked.

## Writing or changing strategies

Use `/new-strategy` for a new strategy. Whether new or existing, these rules apply:

- **Read `kit/STRATEGY_API.md` first.** It is the contract; do not guess the ctx API from memory.
- **Tests before code.** Encode each rule of the strategy as a fixture test (`make_fixture` / `run_on_fixture`) in `strategies/test_<name>.py`, then write the strategy.
- **Validate until clean** with `backtest validate`. Fix the strategy, not the validator. Change a test only if it misstates the rule, and say so.
- **Every strategy has a research log** at `strategies/<name>.md` holding its brief, a fixed in-sample end date and a table of every variant run. When changing a strategy that has no log, create one and ask the user for the in-sample end before running anything.
- **Develop on in-sample data only.** Development runs use `--end <in-sample end>`, so out-of-sample results are never computed. Do not read full-period or split runs of the strategy in `runs/`.
- **One out-of-sample run, with the user's go-ahead.** Run `--end <final end> --in-sample-end <in-sample end> --walk-forward 3y,1y`, record in the log that out-of-sample was viewed, and report it plainly against the brief's failure criterion. Any change after that is flagged in the log as made after seeing out-of-sample.
- **Keep the in-sample end date and costs fixed** for the life of the strategy. Moving them to improve results is overfitting.

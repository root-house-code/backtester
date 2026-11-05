"""Run folders: what the UI reads back, and the chart series built from them."""

from pathlib import Path

import pandas as pd
import pytest
from helpers import STRATEGIES, make_strategy, rotation_frames, run, ts

from backtester.analytics import drawdown_series, monthly_returns
from backtester.run import write_run_folder
from backtester.runfolder import list_runs, load_run
from backtester.testing import make_bars


@pytest.fixture(scope="module")
def momentum():
    return run(STRATEGIES / "momentum_rotation.py", rotation_frames(), "2015-01-01", "2019-12-31",
               benchmark="SPY")


def test_round_trip(tmp_path, momentum):
    folder = write_run_folder(momentum, tmp_path)
    data = load_run(folder)
    assert data.strategy_name == "Momentum rotation"
    assert data.field("initial_capital") == 100_000
    assert data.metrics == momentum.metrics
    pd.testing.assert_frame_equal(data.trades, momentum.trades)
    assert list(data.equity.index) == list(momentum.equity.index)


def test_prices_saved_for_every_traded_ticker(momentum):
    assert set(momentum.prices["ticker"]) == set(momentum.trades["ticker"])
    first = momentum.prices["date"].min()
    assert first < ts("2015-01-02")  # context before the run for charts


def test_trades_carry_stop_and_target():
    def on_bar(self, ctx):
        if ctx.today == ts("2024-03-04"):
            ctx.order("AAA", shares=10, stop=90, target=120)

    res = run(make_strategy(on_bar), {"AAA": make_bars("2024-03-01", "2024-03-15")}, "2024-03-01", "2024-03-15")
    assert res.trades.iloc[0][["stop", "target"]].tolist() == [90.0, 120.0]


def test_list_runs_newest_first(tmp_path, momentum):
    from datetime import datetime

    a = write_run_folder(momentum, tmp_path, now=datetime(2026, 1, 1, 9, 0, 0))
    b = write_run_folder(momentum, tmp_path, now=datetime(2026, 1, 2, 9, 0, 0))
    (tmp_path / "not-a-run").mkdir()
    assert [r.folder for r in list_runs(tmp_path)] == [b, a]


def test_run_label_shows_when_what_and_the_changed_params(tmp_path):
    from datetime import datetime

    res = run(STRATEGIES / "momentum_rotation.py", rotation_frames(), "2015-01-01", "2019-12-31",
              benchmark="SPY", params={"lookback": 63})
    write_run_folder(res, tmp_path, now=datetime(2026, 1, 2, 9, 30, 5))
    label = list_runs(tmp_path)[0].label
    ret = f"{res.metrics['returns']['total_return']:+.1%}"
    # The folder name's slug repeats the strategy, so only its timestamp is shown; top_n kept its default.
    assert label == f"20260102-093005 · Momentum rotation · lookback=63 · 2015-01-02 → 2019-12-31 · {ret}"


def summary(name, **metrics):
    from backtester.runfolder import RunSummary

    return RunSummary(folder=Path(f"2026010{name}-090000-s"), strategy="S", start="2020-01-02", end="2020-12-31",
                      total_return=metrics.get("total_return"), metrics=metrics)


def test_runs_sort_best_first_by_any_headline_metric():
    from backtester.runfolder import NEWEST, sort_runs

    # Newest first, as list_runs gives them. C has no Sharpe (e.g. a run too short to compute one).
    runs = [summary("3", total_return=0.10, sharpe=0.5, max_drawdown=-0.30),
            summary("2", total_return=0.30, sharpe=None, max_drawdown=-0.10),
            summary("1", total_return=0.20, sharpe=0.9, max_drawdown=-0.50)]
    ids = lambda rs: [r.run_id[7] for r in rs]  # noqa: E731
    assert ids(sort_runs(runs, NEWEST)) == ["3", "2", "1"]
    assert ids(sort_runs(runs, "total_return")) == ["2", "1", "3"]
    assert ids(sort_runs(runs, "sharpe")) == ["1", "3", "2"]        # missing values go last
    assert ids(sort_runs(runs, "max_drawdown")) == ["2", "3", "1"]  # shallowest drawdown first


def test_a_sorted_runs_label_shows_the_value_it_is_sorted_by():
    run = summary("1", total_return=0.2, sharpe=0.912, max_drawdown=-0.335, trades=37)
    assert run.label_for("sharpe") == run.label + " · Sharpe 0.91"
    assert run.label_for("max_drawdown") == run.label + " · Max drawdown -33.5%"
    assert run.label_for("trades") == run.label + " · Trades 37"
    assert run.label_for("total_return") == run.label            # already in the label
    assert run.label_for("newest") == run.label


def test_the_hypothesis_is_read_from_the_strategy_source_without_importing_it():
    from backtester.runfolder import strategy_hypothesis

    source = ('import nothing_that_exists\n\nclass S(Strategy):\n    name = "S"\n'
              '    hypothesis = ("Does A beat B? Holds A "\n                  "for 20 sessions.")\n')
    assert strategy_hypothesis(source) == "Does A beat B? Holds A for 20 sessions."
    assert strategy_hypothesis('class S(Strategy):\n    name = "S"\n') == ""
    assert strategy_hypothesis("not python (") == ""


def test_monthly_returns_compound_to_total(momentum):
    eq = momentum.equity["equity"]
    table = monthly_returns(eq, 100_000)
    compounded = (1 + table.drop(columns="Year").stack()).prod() - 1
    assert compounded == pytest.approx(momentum.metrics["returns"]["total_return"], rel=1e-9)
    assert (1 + table["Year"]).prod() - 1 == pytest.approx(compounded, rel=1e-9)


def test_drawdown_series_matches_metric(momentum):
    dd = drawdown_series(momentum.equity["equity"], 100_000)
    assert dd.max() <= 0
    assert dd.min() == pytest.approx(momentum.metrics["risk"]["max_drawdown"], rel=1e-9)

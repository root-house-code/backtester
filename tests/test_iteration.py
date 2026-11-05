"""Iteration tools: in/out-of-sample splits, walk-forward folds, bootstrap
intervals, run comparison and feed management."""

import json

import numpy as np
import pandas as pd
import pytest
from helpers import STRATEGIES, rotation_frames, run

from backtester.analytics import bootstrap_mean_ci, slice_metrics, walk_forward, walk_forward_folds
from backtester.compare import (
    FULL,
    IN_SAMPLE,
    OUT_OF_SAMPLE,
    config_diff,
    growth_curves,
    metrics_table,
)
from backtester.feeds import FeedRegistry
from backtester.feeds.manage import add_file_feed, feed_overview, price_cache_coverage
from backtester.run import metrics_json, write_run_folder
from backtester.runfolder import load_run
from backtester.testing import nyse_sessions

MOMENTUM = STRATEGIES / "momentum_rotation.py"


def momentum(**overrides):
    config = {"start": "2014-01-01", "end": "2019-12-31", "in_sample_end": "2017-12-31", **overrides}
    return run(MOMENTUM, rotation_frames(), config.pop("start"), config.pop("end"), **config)


# --- bootstrap ----------------------------------------------------------------------

def test_bootstrap_is_seeded_and_brackets_the_mean():
    values = np.random.default_rng(3).normal(0.01, 0.05, 80)
    a, b = bootstrap_mean_ci(values, seed=1), bootstrap_mean_ci(values, seed=1)
    assert a == b
    assert a["ci_low"] < a["mean"] < a["ci_high"]
    assert bootstrap_mean_ci(values, seed=2) != a
    # The interval is roughly mean ± 1.96 standard errors.
    se = values.std(ddof=1) / np.sqrt(len(values))
    assert a["ci_high"] - a["ci_low"] == pytest.approx(2 * 1.96 * se, rel=0.1)


def test_bootstrap_needs_two_trades():
    assert bootstrap_mean_ci([0.05], seed=0)["ci_low"] is None
    assert bootstrap_mean_ci([], seed=0)["mean"] is None
    same = bootstrap_mean_ci([0.02] * 5, seed=0)
    assert same["ci_low"] == pytest.approx(0.02) and same["ci_high"] == pytest.approx(0.02)


# --- slices and the in/out-of-sample split --------------------------------------------

def test_a_slice_is_measured_from_the_equity_just_before_it():
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    equity = pd.DataFrame({"equity": [100.0, 110.0, 121.0, 121.0], "benchmark": [100.0, 100.0, 105.0, 105.0],
                           "exposure": 1.0}, index=idx)
    trades = pd.DataFrame({"entry_date": idx[[0, 2]], "pnl": [1.0, -1.0], "return": [0.1, -0.1],
                           "pnl_net": [1.0, -1.0], "is_open": [False, False], "sessions_held": [1, 1]})
    empty_fills = pd.DataFrame(columns=["date", "qty", "price", "commission"])
    empty_signals = pd.DataFrame(columns=["date", "decision", "outcome", "outcome_reason"])
    m = slice_metrics(equity, trades, empty_fills, empty_signals, "2024-01-04", "2024-01-05", 100.0)
    assert m["period"] == {"start": "2024-01-04", "end": "2024-01-05", "sessions": 2}
    assert m["returns"]["total_return"] == pytest.approx(0.1)            # 121 / 110 - 1
    assert m["returns"]["benchmark_total_return"] == pytest.approx(0.05)  # 105 / 100 - 1
    assert m["trades"]["count"] == 1 and m["trades"]["win_rate"] == 0.0  # only the trade entered here


def test_split_halves_compound_to_the_whole_run():
    m = momentum().metrics
    ins, oos = m["split"]["in_sample"], m["split"]["out_of_sample"]
    assert ins["period"]["end"] == "2017-12-29" and oos["period"]["start"] == "2018-01-02"
    assert ins["period"]["sessions"] + oos["period"]["sessions"] == m["period"]["sessions"]
    growth = (1 + ins["returns"]["total_return"]) * (1 + oos["returns"]["total_return"])
    assert growth - 1 == pytest.approx(m["returns"]["total_return"], rel=1e-8)
    assert ins["trades"]["count"] + oos["trades"]["count"] == m["trades"]["count"]
    assert "confidence" in oos


def test_split_runs_are_deterministic(tmp_path):
    a = write_run_folder(momentum(walk_forward="2y,1y"), tmp_path / "a")
    b = write_run_folder(momentum(walk_forward="2y,1y"), tmp_path / "b")
    assert (a / "metrics.json").read_bytes() == (b / "metrics.json").read_bytes()


# --- walk-forward ---------------------------------------------------------------------

def test_walk_forward_folds_roll_by_step():
    dates = nyse_sessions("2015-01-01", "2019-06-28")
    folds = walk_forward_folds(dates, "2y", "1y")
    spans = [(f["in_sample"][0].date().isoformat(), f["in_sample"][1].date().isoformat(),
              f["out_of_sample"][0].date().isoformat(), f["out_of_sample"][1].date().isoformat()) for f in folds]
    assert spans == [
        ("2015-01-02", "2016-12-30", "2017-01-03", "2017-12-29"),
        ("2016-01-04", "2017-12-29", "2018-01-02", "2018-12-31"),
        ("2017-01-03", "2018-12-31", "2019-01-02", "2019-06-28"),   # last step cut at the run's end
    ]


def test_walk_forward_summary_strings_the_out_of_sample_steps_together():
    result = momentum(walk_forward="2y,1y")
    wf = result.metrics["walk_forward"]
    assert wf["summary"]["folds"] == len(wf["folds"]) == 4
    first_oos = pd.Timestamp(wf["folds"][0]["out_of_sample"]["period"]["start"])
    eq = result.equity["equity"]
    expected = eq.iloc[-1] / eq[eq.index < first_oos].iloc[-1] - 1
    assert wf["summary"]["oos_total_return"] == pytest.approx(expected, rel=1e-8)


def test_walk_forward_longer_than_the_run_has_no_folds():
    result = momentum(walk_forward="10y,1y")
    assert result.metrics["walk_forward"]["folds"] == []
    assert any("no complete fold" in line for line in result.log)


# --- comparing runs (done-when: two variants side by side on out-of-sample results) ------

def test_two_variants_compared_side_by_side_out_of_sample(tmp_path):
    folders = [write_run_folder(momentum(params={"lookback": lb}), tmp_path / "runs") for lb in (63, 126)]
    runs = [load_run(f) for f in folders]

    table = metrics_table(runs, OUT_OF_SAMPLE)
    assert table.shape[1] == 2
    assert (table.loc["Start"] == "2018-01-02").all()
    for col, r in zip(table.columns, runs):
        oos = r.metrics["split"]["out_of_sample"]
        assert table.loc["CAGR", col] == f"{oos['returns']['cagr']:.2%}"
        assert table.loc["Sharpe", col] == f"{oos['risk']['sharpe']:,.2f}"
    assert table.loc["CAGR"].nunique() == 2   # the variants really differ

    diff = config_diff(runs)
    assert list(diff.index) == ["params.lookback"]
    assert diff.loc["params.lookback"].tolist() == ["63", "126"]

    curves = growth_curves(runs, OUT_OF_SAMPLE)
    assert curves.groupby("run")["date"].min().eq(pd.Timestamp("2018-01-02")).all()
    full = growth_curves(runs, FULL)
    assert full.groupby("run")["date"].min().eq(pd.Timestamp("2014-01-02")).all()
    assert metrics_table(runs, IN_SAMPLE).loc["End"].eq("2017-12-29").all()


def test_runs_without_a_split_show_dashes_out_of_sample(tmp_path):
    with_split = load_run(write_run_folder(momentum(), tmp_path / "runs"))
    without = load_run(write_run_folder(momentum(in_sample_end=None), tmp_path / "runs"))
    table = metrics_table([with_split, without], OUT_OF_SAMPLE)
    assert table.iloc[:, 1].eq("–").all()
    assert growth_curves([with_split, without], OUT_OF_SAMPLE)["run"].nunique() == 1


# --- feed management (Data page) --------------------------------------------------------

CSV = b"symbol,when,score\nAAA,2024-01-03 17:00,0.9\nBBB,2024-01-04,0.2\n"


def test_add_file_feed_checks_then_writes(tmp_path):
    feeds = tmp_path / "feeds"
    v, folder = add_file_feed(feeds, "screener", "signals.csv", CSV, "event", "symbol", "when")
    assert v.ok and folder == feeds / "screener"
    assert (folder / "signals.csv").read_bytes() == CSV
    data = FeedRegistry(feeds, None, tmp_path / "cache").load("screener")
    assert data.records["ticker"].tolist() == ["AAA", "BBB"]
    assert data.records["available_at"].iloc[0] == pd.Timestamp("2024-01-03 22:00", tz="UTC")


@pytest.mark.parametrize("name,data,error", [
    ("Bad Name", CSV, "lowercase"),
    ("market_cap", CSV, "already exists"),
    ("future", b"symbol,when\nAAA,2999-01-01\n", "in the future"),
    ("cols", b"ticker,when\nAAA,2024-01-03\n", "missing ticker column 'symbol'"),
])
def test_add_file_feed_writes_nothing_when_checks_fail(tmp_path, name, data, error):
    v, folder = add_file_feed(tmp_path / "feeds", name, "x.csv", data, "event", "symbol", "when")
    assert folder is None and not v.ok
    assert any(error in e for e in v.errors), v.errors
    assert not (tmp_path / "feeds" / name).exists()


def test_feed_overview_and_price_cache_coverage(tmp_path):
    add_file_feed(tmp_path / "feeds", "screener", "s.csv", CSV, "event", "symbol", "when")
    overview = feed_overview(FeedRegistry(tmp_path / "feeds", None, tmp_path / "cache")).set_index("feed")
    assert overview.loc["screener", "status"] == "OK" and overview.loc["screener", "records"] == 2
    assert set(overview.index) == {"screener", "market_cap", "shares_outstanding"}

    prices = tmp_path / "cache" / "prices"
    prices.mkdir(parents=True)
    (prices / "SPY.json").write_text(json.dumps({"first": "1993-01-29", "last": "2024-12-31", "rows": 8000,
                                                 "fetched_at": "2026-11-01T00:00:00+00:00", "missing": False}))
    (prices / "ZZZZ.json").write_text(json.dumps({"missing": True, "fetched_at": "2026-11-01T00:00:00+00:00"}))
    cov = price_cache_coverage(tmp_path / "cache").set_index("ticker")
    assert cov.loc["SPY", "rows"] == 8000 and not cov.loc["SPY", "missing"]
    assert cov.loc["ZZZZ", "missing"]


def test_metrics_json_with_splits_is_valid_json():
    m = momentum(walk_forward="2y,1y").metrics
    assert json.loads(metrics_json(m))["split"]["in_sample_end"] == "2017-12-31"

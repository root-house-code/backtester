"""Determinism: the same config and data produce identical metrics."""

import hashlib

from helpers import STRATEGIES, make_strategy, rotation_frames, run

from backtester.run import metrics_json, write_run_folder


def test_same_inputs_same_metrics_hash():
    frames = rotation_frames()
    a = run(STRATEGIES / "momentum_rotation.py", frames, "2015-01-01", "2019-12-31", benchmark="SPY")
    b = run(STRATEGIES / "momentum_rotation.py", frames, "2015-01-01", "2019-12-31", benchmark="SPY")
    ha = hashlib.sha256(metrics_json(a.metrics).encode()).hexdigest()
    hb = hashlib.sha256(metrics_json(b.metrics).encode()).hexdigest()
    assert ha == hb
    assert a.trades.equals(b.trades)


def test_run_folders_have_byte_identical_metrics(tmp_path):
    frames = rotation_frames()
    paths = []
    for _ in range(2):
        res = run(STRATEGIES / "momentum_rotation.py", frames, "2015-01-01", "2019-12-31",
                  benchmark="SPY")
        paths.append(write_run_folder(res, tmp_path))
    assert paths[0] != paths[1]
    assert (paths[0] / "metrics.json").read_bytes() == (paths[1] / "metrics.json").read_bytes()


def test_rng_is_seeded_per_run():
    draws = []

    def on_bar(self, ctx):
        draws.append(float(ctx.rng.random()))

    frames = {"AAA": rotation_frames()["SPY"]}
    run(make_strategy(on_bar), frames, "2015-01-01", "2015-03-31")
    first = list(draws)
    draws.clear()
    run(make_strategy(on_bar), frames, "2015-01-01", "2015-03-31")
    assert draws == first
    draws.clear()
    run(make_strategy(on_bar), frames, "2015-01-01", "2015-03-31", seed=7)
    assert draws != first

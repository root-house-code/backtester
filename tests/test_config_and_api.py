"""Config precedence, strategy loading, and ctx behaviors not covered elsewhere."""

import pytest
from helpers import STRATEGIES, make_strategy, rotation_frames, run, ts

from backtester import ConfigError, OrderError, Param, Strategy, StrategyError
from backtester.config import resolve_config
from backtester.strategy import load_strategy_file
from backtester.testing import make_bars

MOMENTUM = load_strategy_file(STRATEGIES / "momentum_rotation.py")


def test_precedence_override_then_strategy_then_engine():
    cfg = resolve_config(MOMENTUM, {"start": "2020-01-01", "end": "2020-12-31",
                                    "params": {"lookback": "60"}, "slippage_bps": 3})
    assert cfg.params["lookback"] == 60 and cfg.param_sources["lookback"] == "override"
    assert cfg.params["top_n"] == 2 and cfg.param_sources["top_n"] == "strategy"
    assert cfg["slippage_bps"] == 3.0 and cfg.sources["slippage_bps"] == "override"
    assert cfg["initial_capital"] == 100_000 and cfg.sources["initial_capital"] == "engine"
    assert cfg["universe"][0] == "SPY" and cfg.sources["universe"] == "strategy"


@pytest.mark.parametrize("overrides, message", [
    ({"params": {"lookback": 5}}, "below min"),
    ({"params": {"lookback": "abc"}}, "not a valid int"),
    ({"params": {"nope": 1}}, "unknown strategy params"),
    ({"cash_shortfall": "maybe"}, "cash_shortfall"),
    ({"bogus": 1}, "unknown config fields"),
    ({"in_sample_end": "2021-06-30"}, "before end"),
    ({"in_sample_end": "2020-12-31"}, "before end"),
    ({"in_sample_end": "2019-06-30"}, "on or after start"),
    ({"walk_forward": "2y"}, "window,step"),
    ({"walk_forward": "2y,6x"}, "walk_forward step"),
    ({"walk_forward": {"window": "1y"}}, "window and a step"),
])
def test_bad_config_fails_loudly(overrides, message):
    with pytest.raises(ConfigError, match=message):
        resolve_config(MOMENTUM, {"start": "2020-01-01", "end": "2020-12-31", **overrides})


def test_unsupported_api_version_fails(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text(
        "from backtester import Strategy\n"
        "class Bad(Strategy):\n"
        "    api_version = 2\n    name = 'x'\n    description = 'x'\n"
        "    def on_bar(self, ctx): pass\n"
    )
    with pytest.raises(StrategyError, match="api_version"):
        load_strategy_file(f)


def test_two_strategy_classes_fail(tmp_path):
    body = "    api_version = 1\n    name = 'x'\n    description = 'x'\n    def on_bar(self, ctx): pass\n"
    f = tmp_path / "two.py"
    f.write_text("from backtester import Strategy\nclass A(Strategy):\n" + body + "class B(Strategy):\n" + body)
    with pytest.raises(StrategyError, match="exactly one"):
        load_strategy_file(f)


def test_param_default_outside_range_fails():
    with pytest.raises(StrategyError):
        Param(int, default=500, min=1, max=10)


def test_target_weights_closes_unlisted_holdings():
    frames = {t: make_bars("2024-03-01", "2024-03-29", price=50.0) for t in ("AAA", "BBB")}

    def on_bar(self, ctx):
        if ctx.today == ts("2024-03-04"):
            ctx.target_weights({"AAA": 0.5, "BBB": 0.5})
        if ctx.today == ts("2024-03-11"):
            ctx.target_weights({"AAA": 1.0})

    res = run(make_strategy(on_bar, universe=["AAA", "BBB"]), frames, "2024-03-01", "2024-03-29")
    bbb = res.trades[res.trades["ticker"] == "BBB"].iloc[0]
    assert bbb["exit_date"] == ts("2024-03-11") and bbb["exit_reason"] == "rebalance"
    assert res.equity.loc[ts("2024-03-11"), "exposure"] == pytest.approx(1.0, abs=1e-3)


def test_liquidity_cap_resizes_order():
    frames = {"AAA": make_bars("2024-02-01", "2024-03-29", price=100.0, volume=1_000)}  # $100k/day

    def on_bar(self, ctx):
        if ctx.today == ts("2024-03-04"):
            ctx.order("AAA", value=50_000)

    res = run(make_strategy(on_bar), frames, "2024-02-01", "2024-03-29", max_adv_pct=10)
    assert res.fills.iloc[0]["qty"] == 100  # 10% of $100k ADV at $100
    assert res.orders.iloc[0]["status"] == "resized"


def test_on_fill_called_and_orders_from_it_fill_next_session():
    frames = {"AAA": make_bars("2024-03-01", "2024-03-15", price=100.0)}
    seen = []

    def on_fill(self, ctx, fill):
        seen.append((fill.date, fill.qty))
        if fill.qty > 0:
            ctx.close("AAA")

    def on_bar(self, ctx):
        if ctx.today == ts("2024-03-04"):
            ctx.order("AAA", shares=5)

    run(make_strategy(on_bar, on_fill=on_fill), frames, "2024-03-01", "2024-03-15")
    assert seen == [(ts("2024-03-04"), 5.0), (ts("2024-03-05"), -5.0)]


@pytest.mark.parametrize("kwargs", [
    dict(),
    dict(shares=1, pct=0.1),
    dict(shares=1.5),
    dict(pct=2.0),
    dict(shares=1, at="midday"),
    dict(shares=1, stop=110, target=100),
    dict(shares=-1, exit_after=3),
    dict(shares=1, exit_after=0),
])
def test_malformed_orders_raise(kwargs):
    def on_bar(self, ctx):
        ctx.order("AAA", **kwargs)

    with pytest.raises(OrderError):
        run(make_strategy(on_bar), {"AAA": make_bars("2024-03-01", "2024-03-08")}, "2024-03-01", "2024-03-08")


def test_is_first_session_of():
    hits = {"week": [], "month": [], "quarter": [], "year": []}

    def on_bar(self, ctx):
        for period in hits:
            if ctx.is_first_session_of(period):
                hits[period].append(ctx.today)

    run(make_strategy(on_bar), {"AAA": make_bars("2023-12-20", "2024-04-10")}, "2023-12-20", "2024-04-10")
    assert hits["year"] == [ts("2024-01-02")]
    assert hits["quarter"] == [ts("2024-01-02"), ts("2024-04-01")]
    assert ts("2024-02-01") in hits["month"] and ts("2023-12-01") not in hits["month"]
    assert ts("2024-01-16") in hits["week"]  # Tuesday after MLK day


def test_signals_record_rejections():
    def on_bar(self, ctx):
        if ctx.today == ts("2024-03-04"):
            ctx.reject("AAA", "too expensive")

    res = run(make_strategy(on_bar), {"AAA": make_bars("2024-03-01", "2024-03-08")}, "2024-03-01", "2024-03-08")
    row = res.signals.iloc[0]
    assert row["decision"] == "reject" and row["reason"] == "too expensive"

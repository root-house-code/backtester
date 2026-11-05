"""UI smoke tests with Streamlit's AppTest, on a synthetic price cache (no network)."""

import sys
from datetime import date

import pandas as pd
import pytest
from helpers import ROOT, ROTATION_TICKERS, rotation_frames

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from backtester.data.yfinance_provider import YFinanceProvider  # noqa: E402
from backtester.ids import SecurityId  # noqa: E402

APP = str(ROOT / "app" / "main.py")
sys.path.insert(0, str(ROOT / "app"))
from charts import trade_chart, trade_window  # noqa: E402


@pytest.fixture
def ui_env(tmp_path, monkeypatch):
    """A price cache holding synthetic bars, an empty runs folder, and fresh UI modules."""
    cache = tmp_path / "cache"
    provider = YFinanceProvider(cache_dir=cache)
    for ticker, df in rotation_frames().items():
        raw = df.assign(adj_close=df["close"], dividends=0.0, splits=0.0)
        provider._save(SecurityId(ticker), raw, full=True)
    monkeypatch.setenv("BACKTESTER_CACHE_DIR", str(cache))
    monkeypatch.setenv("BACKTESTER_RUNS_DIR", str(tmp_path / "runs"))
    for mod in ("common", "charts"):
        sys.modules.pop(mod, None)
    yield tmp_path / "runs"
    for mod in ("common", "charts"):
        sys.modules.pop(mod, None)


def launch_run(strategy="momentum_rotation", **params):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.selectbox[0].select(ROOT / "strategies" / f"{strategy}.py").run()
    at.date_input(key=f"{strategy}:start").set_value(date(2015, 1, 1))
    at.date_input(key=f"{strategy}:end").set_value(date(2019, 12, 31))
    at.checkbox(key=f"{strategy}:refresh").set_value(False)
    for name, value in params.items():
        at.number_input(key=f"{strategy}:param:{name}").set_value(value)
    at.button[0].click().run()
    return at


def test_run_page_launches_a_run_and_shows_results(ui_env):
    at = launch_run(lookback=63)
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    runs = list(ui_env.iterdir())
    assert len(runs) == 1
    config = (runs[0] / "config.json").read_text()
    assert '"lookback": {\n      "value": 63,\n      "source": "override"' in config
    assert '"top_n": {\n      "value": 2,\n      "source": "strategy"' in config
    # Landed on the Results page for that run.
    assert at.title[0].value == "Momentum rotation"
    assert at.markdown[0].value.startswith("Worked example: do recent winners keep winning?")
    assert [m.label for m in at.metric][:2] == ["Total return", "CAGR"]


def test_results_shows_the_current_hypothesis_for_runs_saved_without_one(ui_env):
    launch_run()
    saved = next(ui_env.iterdir()) / "strategy.py"
    saved.write_text("\n".join(line for line in saved.read_text().splitlines()
                               if "hypothesis" not in line and "EFA, TLT" not in line))
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.switch_page("views/results.py").run()
    assert not at.exception, at.exception
    assert at.markdown[0].value.startswith("Worked example: do recent winners keep winning?")


def test_results_and_trades_pages_render(ui_env):
    launch_run()
    for page in ("views/results.py", "views/trades.py"):
        at = AppTest.from_file(APP, default_timeout=60)
        at.run()
        at.switch_page(page).run()
        assert not at.exception, (page, at.exception)
        assert not at.error, (page, [e.value for e in at.error])
    assert at.title[0].value == "Audit"
    assert len(at.dataframe) == 2  # trades and signals tabs


def test_empty_runs_folder_shows_hint(ui_env):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.switch_page("views/results.py").run()
    assert not at.exception
    assert "No runs yet" in at.info[0].value


def test_strategies_page_lists_and_validates_strategy_files(ui_env):
    launch_run()
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    at.switch_page("views/strategies.py").run()
    assert not at.exception, at.exception
    table = at.dataframe[0].value
    assert not any(f.startswith("test_") for f in table["File"])  # no test files
    # The user's own strategies sit alongside these; check the reference ones.
    reference = table[table["File"].isin(["buy_and_hold.py", "event_hold.py", "momentum_rotation.py"])]
    assert reference["File"].tolist() == ["buy_and_hold.py", "event_hold.py", "momentum_rotation.py"]
    assert reference["Validation"].tolist() == ["Passed", "Passed", "Passed"]
    assert reference["Runs"].tolist() == [0, 0, 1]
    assert at.code[0].value.startswith("Validation passed: buy_and_hold.py")


def test_run_page_blocks_a_strategy_that_fails_validation(ui_env, tmp_path, monkeypatch):
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "peek.py").write_text(
        "from backtester import Strategy\n\n\nclass Peek(Strategy):\n    api_version = 1\n"
        "    name = 'Peek'\n    description = 'Reaches into the engine.'\n    universe = ['SPY']\n\n"
        "    def on_bar(self, ctx):\n        engine = ctx._engine\n")
    monkeypatch.setenv("BACKTESTER_STRATEGIES_DIR", str(strategies))
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    assert "fails validation" in at.error[0].value
    assert "peek.py:11  private-access" in at.code[0].value
    assert len(at.button) == 0  # no Run button


def test_event_strategy_runs_from_the_ui_and_its_signals_are_audited(ui_env):
    # The sample feed's signals are from 2024; give the synthetic cache prices for that year.
    provider = YFinanceProvider(cache_dir=ui_env.parent / "cache")
    for ticker, df in rotation_frames("2023-01-01", "2024-12-31").items():
        provider._save(SecurityId(ticker), df.assign(adj_close=df["close"], dividends=0.0, splits=0.0), full=True)
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    at.selectbox[0].select(ROOT / "strategies" / "event_hold.py").run()
    assert at.text_input(key="event_hold:tickers").value == "from_feed:sample_signals"
    at.date_input(key="event_hold:start").set_value(date(2024, 1, 2))
    at.date_input(key="event_hold:end").set_value(date(2024, 12, 31))
    at.checkbox(key="event_hold:refresh").set_value(False)
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    at.switch_page("views/trades.py").run()
    assert not at.exception, at.exception
    signals = at.dataframe[1].value
    assert (signals["decision"] == "event").sum() == 20
    assert "20 feed events" in " ".join(c.value for c in at.caption)


def launch_split_run(lookback):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.selectbox[0].select(ROOT / "strategies" / "momentum_rotation.py").run()
    at.date_input(key="momentum_rotation:start").set_value(date(2015, 1, 1))
    at.date_input(key="momentum_rotation:end").set_value(date(2019, 12, 31))
    at.date_input(key="momentum_rotation:ise").set_value(date(2017, 12, 31))
    at.text_input(key="momentum_rotation:wf").set_value("2y,1y")
    at.checkbox(key="momentum_rotation:refresh").set_value(False)
    at.number_input(key="momentum_rotation:param:lookback").set_value(lookback)
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    return at


def test_results_page_shows_the_split_and_walk_forward(ui_env):
    at = launch_split_run(126)
    assert at.radio[0].options == ["Full period", "In-sample", "Out-of-sample"]
    full_cagr = at.metric[1].value
    at.radio[0].set_value("out_of_sample").run()
    assert not at.exception, at.exception
    assert at.metric[1].value != full_cagr
    assert any(s.value == "Walk-forward" for s in at.subheader)
    folds = at.dataframe[0].value
    assert folds["Fold"].tolist() == [1, 2, 3]


def test_results_runs_can_be_sorted_by_a_metric_and_keep_the_chosen_run(ui_env):
    from backtester.runfolder import list_runs, sort_runs

    launch_run(lookback=63)
    launch_run(lookback=126)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.switch_page("views/results.py").run()
    box = {s.label: s for s in at.selectbox}
    assert box["Sort by"].value == "newest"
    chosen = box["Run"].value
    box["Sort by"].select("sharpe").run()
    assert not at.exception, at.exception
    box = {s.label: s for s in at.selectbox}
    assert box["Run"].options == [r.label_for("sharpe") for r in sort_runs(list_runs(ui_env), "sharpe")]
    assert box["Run"].value == chosen   # re-sorting doesn't switch the run being viewed


def test_compare_page_puts_two_variants_side_by_side(ui_env):
    launch_split_run(63)
    launch_split_run(126)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.switch_page("views/compare.py").run()
    assert not at.exception, at.exception
    from backtester.runfolder import list_runs
    at.multiselect[0].set_value([str(s.folder) for s in list_runs(ui_env)][:2]).run()
    assert not at.exception, at.exception
    assert at.radio[0].value == "out_of_sample"
    table = at.dataframe[0].value
    assert table.shape[1] == 2 and table.loc["Start"].eq("2018-01-02").all()
    assert at.dataframe[1].value.index.tolist() == ["params.lookback"]


def test_data_page_lists_feeds_and_the_price_cache(ui_env):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.switch_page("views/data.py").run()
    assert not at.exception, at.exception
    feeds = at.dataframe[0].value
    assert "sample_signals" in feeds["feed"].tolist()
    assert feeds.set_index("feed").loc["sample_signals", "status"] == "OK"
    cache = at.dataframe[1].value
    assert set(ROTATION_TICKERS) <= set(cache["ticker"])


def test_readme_page_renders(ui_env):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.switch_page("views/readme.py").run()
    assert not at.exception, at.exception
    headers = [h.value for h in at.header]
    assert headers == ["About Backtester", "Technical Design and Development",
                       "How to Write an Input Strategy in Python"]


def test_instructions_page_renders(ui_env):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.switch_page("views/instructions.py").run()
    assert not at.exception, at.exception
    headers = [h.value for h in at.header]
    assert headers[0] == "1. Start the app"
    assert "4. Run a backtest" in headers


def test_results_metrics_have_help_text(ui_env):
    at = launch_run()
    assert all(m.help for m in at.metric[:6]), [m.label for m in at.metric[:6] if not m.help]


def test_every_trade_can_be_charted(ui_env):
    at = launch_run()
    from backtester.runfolder import list_runs, load_run

    data = load_run(list_runs(ui_env)[0].folder)
    assert len(data.trades) > 5
    for _, trade in data.trades.iterrows():
        window = trade_window(data.prices, trade)
        assert window["date"].min() <= trade["entry_date"]
        if not trade["is_open"]:
            assert window["date"].max() >= trade["exit_date"]
        assert trade_chart(data.prices, trade) is not None


def test_trade_chart_draws_stop_and_target_lines():
    prices = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=60), "ticker": "AAA",
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
    })
    trade = pd.Series({
        "ticker": "AAA", "entry_date": prices["date"][30], "entry_price": 100.0,
        "exit_date": prices["date"][40], "exit_price": 110.0, "is_open": False,
        "stop": 95.0, "target": 110.0,
    })
    spec = trade_chart(prices, trade).to_dict()
    assert len(spec["layer"]) == 3  # price line, markers, stop/target rules
    assert ROTATION_TICKERS  # helpers import sanity

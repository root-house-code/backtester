"""Run setup: pick a strategy, set the run config, run it."""

from datetime import date, timedelta

import streamlit as st
from common import CACHE_DIR, CURRENT_RUN, RUNS_DIR, STRATEGIES_DIR, feed_registry, strategy_files, validation

from backtester.config import ENGINE_DEFAULTS, resolve_universe, universe_feed
from backtester.data.yfinance_provider import YFinanceProvider
from backtester.errors import BacktesterError, ConfigError
from backtester.run import run_backtest, write_run_folder
from backtester.strategy import load_strategy_file

st.title("Run")

files = strategy_files()
if not files:
    st.info(f"No strategy files in {STRATEGIES_DIR}.")
    st.stop()

path = st.selectbox("Strategy", files, format_func=lambda p: p.stem)
quick = validation(path, quick=True)
if quick.failed_stage is not None:
    st.error("This strategy fails validation. Copy the report below back to whoever wrote it.")
    st.code(quick.text(), language=None)
    st.stop()
try:
    cls = load_strategy_file(path)
except BacktesterError as exc:
    st.error(str(exc))
    st.stop()
st.caption(cls.description)

if universe_feed(cls.universe) is not None:
    default_universe = cls.universe.strip()
else:
    try:
        default_universe = resolve_universe(cls.universe)
    except ConfigError:
        default_universe = []

key = path.stem  # widget keys per strategy, so switching strategy resets the form


def param_widget(name, p):
    label = name if not p.description else f"{name} — {p.description}"
    k = f"{key}:param:{name}"
    if p.choices is not None:
        return st.selectbox(label, p.choices, index=list(p.choices).index(p.default), key=k)
    if p.type is bool:
        return st.checkbox(label, value=p.default, key=k)
    if p.type is int:
        return int(st.number_input(label, value=p.default, min_value=p.min, max_value=p.max, step=1, key=k))
    if p.type is float:
        return float(st.number_input(
            label, value=float(p.default),
            min_value=None if p.min is None else float(p.min),
            max_value=None if p.max is None else float(p.max), key=k))
    return st.text_input(label, value=p.default, key=k)


with st.form(f"run:{key}"):
    c1, c2 = st.columns(2)
    start = c1.date_input("Start", value=date(2015, 1, 1), min_value=date(1962, 1, 2), key=f"{key}:start")
    end = c2.date_input("End", value=date.today() - timedelta(days=1), min_value=date(1962, 1, 2), key=f"{key}:end")
    default_text = default_universe if isinstance(default_universe, str) else ", ".join(default_universe)
    tickers_text = st.text_input("Tickers", value=default_text, key=f"{key}:tickers",
                                 help='A comma-separated list, or "from_feed:<feed>" for every ticker a feed mentions.')
    capital = st.number_input("Capital ($)", value=ENGINE_DEFAULTS["initial_capital"], min_value=1.0,
                              step=10_000.0, key=f"{key}:capital")

    params = {name: param_widget(name, p) for name, p in cls.params.items()}

    with st.expander("Advanced"):
        a1, a2 = st.columns(2)
        benchmark = a1.text_input("Benchmark", value=ENGINE_DEFAULTS["benchmark"], key=f"{key}:benchmark")
        slippage = a2.number_input("Slippage (bps)", value=0.0, min_value=0.0, key=f"{key}:slippage")
        commission = a1.number_input("Commission per share ($)", value=0.0, min_value=0.0, format="%.4f",
                                     key=f"{key}:commission")
        commission_min = a2.number_input("Minimum commission ($)", value=0.0, min_value=0.0, key=f"{key}:cmin")
        adv = a1.number_input("Liquidity cap (% of 20-day dollar volume, 0 = off)", value=0.0,
                              min_value=0.0, max_value=100.0, key=f"{key}:adv")
        shortfall = a2.selectbox("When cash is short", ["resize", "reject"], key=f"{key}:shortfall")
        seed = a1.number_input("Random seed", value=0, step=1, key=f"{key}:seed")
        fractional = a2.checkbox("Fractional shares", value=False, key=f"{key}:fractional")
        refresh = a2.checkbox("Refresh prices from Yahoo", value=True, key=f"{key}:refresh")
        st.caption("Overfitting checks")
        o1, o2 = st.columns(2)
        in_sample_end = o1.date_input(
            "In-sample end (optional)", value=None, min_value=date(1962, 1, 2), key=f"{key}:ise",
            help="Score the run separately before and after this date. Build on the in-sample period, "
                 "judge on the out-of-sample one.")
        walk = o2.text_input(
            "Walk-forward window,step (optional)", value="", key=f"{key}:wf", placeholder="e.g. 2y,6m",
            help="Score the run in rolling folds: each in-sample window, then the step after it. "
                 "Lengths like 2y, 6m, 13w or 90d.")

    submitted = st.form_submit_button("Run", type="primary")

if submitted:
    # Send only values that differ from their defaults, so the saved config
    # records them as overrides and everything else as strategy/engine defaults.
    overrides = {"start": start.isoformat(), "end": end.isoformat()}
    if universe_feed(tickers_text) is not None:
        tickers = tickers_text.strip()
    else:
        tickers = [t.strip().upper() for t in tickers_text.replace(";", ",").split(",") if t.strip()]
    if tickers != default_universe:
        overrides["universe"] = tickers
    changed_params = {n: v for n, v in params.items() if v != cls.params[n].default}
    if changed_params:
        overrides["params"] = changed_params
    candidates = {
        "initial_capital": float(capital),
        "benchmark": benchmark.strip().upper(),
        "slippage_bps": float(slippage),
        "commission_per_share": float(commission),
        "commission_min": float(commission_min),
        "max_adv_pct": float(adv) if adv > 0 else None,
        "cash_shortfall": shortfall,
        "seed": int(seed),
        "fractional_shares": bool(fractional),
    }
    for field, value in candidates.items():
        if value != ENGINE_DEFAULTS[field]:
            overrides[field] = value
    if in_sample_end is not None:
        overrides["in_sample_end"] = in_sample_end.isoformat()
    if walk.strip():
        overrides["walk_forward"] = walk.strip()

    try:
        with st.spinner("Running…"):
            provider = YFinanceProvider(cache_dir=CACHE_DIR, refresh=refresh)
            result = run_backtest(path, overrides, provider=provider, feeds=feed_registry(refresh))
            folder = write_run_folder(result, RUNS_DIR)
    except BacktesterError as exc:
        st.error(str(exc))
    except Exception as exc:  # errors raised by strategy code
        st.error(f"The strategy raised an error: {exc!r}")
        st.exception(exc)
    else:
        st.session_state[CURRENT_RUN] = str(folder)
        st.switch_page("views/results.py")

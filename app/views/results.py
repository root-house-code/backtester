"""Results: headline numbers, equity vs benchmark, drawdown, monthly returns, splits, all metrics."""

import pandas as pd
import streamlit as st
from charts import drawdown_chart, equity_chart, monthly_heatmap
from common import fmt, hypothesis, run_picker, universe_label

from backtester.analytics import drawdown_series, monthly_returns
from backtester.compare import FULL, IN_SAMPLE, OUT_OF_SAMPLE, PERIOD_LABELS, period_metrics

run = run_picker()
if run is None:
    st.stop()

full = run.metrics
capital = run.field("initial_capital")
benchmark = run.field("benchmark")
split = full.get("split")

st.title(run.strategy_name)
testing = hypothesis(run)
if testing:
    st.markdown(testing.replace("$", r"\$"))  # plain text: a $ would otherwise start a formula
st.caption(f"{full['period']['start']} → {full['period']['end']} · {universe_label(run.field('universe'))} · "
           f"{run.folder.name}")

period = FULL
if split:
    period = st.radio("Period", [FULL, IN_SAMPLE, OUT_OF_SAMPLE], format_func=PERIOD_LABELS.get,
                      horizontal=True, label_visibility="collapsed", key=f"period:{run.folder.name}")
m = period_metrics(run, period) or full
r, risk, trades, dq = m["returns"], m["risk"], m["trades"], m["data_quality"]
if period != FULL:
    st.caption(f"{PERIOD_LABELS[period]}: {m['period']['start']} → {m['period']['end']} "
               f"({m['period']['sessions']} sessions). Trades count in the period they were entered.")

cols = st.columns(6)
excess = r["excess_return"]
cols[0].metric("Total return", fmt("total_return", r["total_return"]),
               delta=None if excess is None else f"{excess:+.1%} vs {benchmark}",
               help="Overall gain or loss on starting capital. "
                    "Final equity ÷ initial capital − 1. The arrow shows the difference from the benchmark.")
cols[1].metric("CAGR", fmt("cagr", r["cagr"]),
               help="Compound annual growth rate: the steady yearly return that gives the same result. "
                    "(1 + total return) ^ (1 / years) − 1, with 252 sessions per year.")
cols[2].metric("Sharpe", fmt("sharpe", risk["sharpe"]),
               help="Return per unit of risk. Mean daily return ÷ standard deviation of daily returns "
                    "× √252, with a risk-free rate of 0. Above 1 is good.")
cols[3].metric("Max drawdown", fmt("max_drawdown", risk["max_drawdown"]),
               help="Largest fall in equity from a previous high, measured from the peak "
                    "to the lowest point before recovery.")
cols[4].metric("Trades", trades["count"],
               help="Number of trades, counting positions still open at the end (valued at the last close). "
                    "Rebalancing an existing position adds to its trade instead of starting a new one.")
cols[5].metric("Skipped signals", fmt("skip_rate", dq["skip_rate"]),
               help="Share of strategy signals that could not trade because the ticker had no price data "
                    "that day. Skipped ÷ total signals.")

ci = (m.get("confidence") or {}).get("mean_trade_return")
if ci and ci.get("ci_low") is not None:
    st.caption(
        f"Mean trade return {ci['mean']:+.2%} · 95% confidence interval {ci['ci_low']:+.2%} to "
        f"{ci['ci_high']:+.2%} ({ci['n']} trades, bootstrap). "
        + ("The interval includes zero, so the edge may be luck." if ci["ci_low"] <= 0 <= ci["ci_high"]
           else "The interval excludes zero.")
    )

eq = run.equity
split_date = pd.Timestamp(split["in_sample_end"]) if split else None
st.altair_chart(equity_chart(eq, benchmark, split_date), width="stretch")
st.altair_chart(drawdown_chart(drawdown_series(eq["equity"], capital), split_date), width="stretch")
if split:
    st.caption("The dashed line marks the end of the in-sample period.")

wf = full.get("walk_forward")
if wf and wf.get("folds"):
    st.subheader("Walk-forward")
    s = wf["summary"]
    st.caption(
        f"{s['folds']} folds · {wf['window']} in-sample window, {wf['step']} out-of-sample step · "
        f"out-of-sample periods together: {fmt('total_return', s['oos_total_return'])} total, "
        f"{fmt('cagr', s['oos_cagr'])} a year · positive in {fmt('win_rate', s['oos_positive_share'])} of folds · "
        f"mean Sharpe {fmt('sharpe', s['mean_is_sharpe'])} in-sample vs {fmt('sharpe', s['mean_oos_sharpe'])} out"
    )
    rows = []
    for f in wf["folds"]:
        ins, oos = f["in_sample"], f["out_of_sample"]
        rows.append({
            "Fold": f["fold"],
            "In-sample": f"{ins['period']['start']} → {ins['period']['end']}",
            "Out-of-sample": f"{oos['period']['start']} → {oos['period']['end']}",
            "IS CAGR": ins["returns"]["cagr"],
            "OOS CAGR": oos["returns"]["cagr"],
            "IS Sharpe": ins["risk"]["sharpe"],
            "OOS Sharpe": oos["risk"]["sharpe"],
            "OOS max drawdown": oos["risk"]["max_drawdown"],
            "OOS trades": oos["trades"]["count"],
        })
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        column_config={k: st.column_config.NumberColumn(format="percent")
                       for k in ("IS CAGR", "OOS CAGR", "OOS max drawdown")} | {
            k: st.column_config.NumberColumn(format="%.2f") for k in ("IS Sharpe", "OOS Sharpe")},
    )

st.subheader("Monthly returns")
st.altair_chart(monthly_heatmap(monthly_returns(eq["equity"], capital)), width="stretch")

with st.expander(f"All metrics ({PERIOD_LABELS[period].lower()})"):
    groups = [("Returns", r), ("Risk", risk), ("Trades", trades), ("Exposure", m["exposure"]),
              ("Data quality", dq)]
    cols = st.columns(len(groups))
    for col, (title, values) in zip(cols, groups):
        col.markdown(f"**{title}**")
        col.dataframe(
            pd.DataFrame({"value": {k: fmt(k, v) for k, v in values.items()}}),
            width="stretch",
        )

st.page_link("views/trades.py", label="Open the audit →")

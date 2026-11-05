"""Compare runs: metrics side by side, overlaid equity curves, and the settings that differ."""

import streamlit as st
from charts import growth_chart
from common import RUNS_DIR, load

from backtester.compare import (
    FULL,
    IN_SAMPLE,
    OUT_OF_SAMPLE,
    PERIOD_LABELS,
    config_diff,
    growth_curves,
    has_split,
    metrics_table,
)
from backtester.runfolder import list_runs

st.title("Compare")

summaries = list_runs(RUNS_DIR)
if len(summaries) < 2:
    st.info("Compare needs at least two saved runs. Start them on the Run page.")
    st.stop()

labels = {str(s.folder): s.label for s in summaries}
chosen = st.multiselect("Runs (2–4)", list(labels), format_func=labels.get, max_selections=4,
                        placeholder="Choose 2 to 4 runs")
if len(chosen) < 2:
    st.caption("Pick two to four runs. Give them an in-sample end on the Run page (Advanced) to compare "
               "them out-of-sample.")
    st.stop()

runs = [load(folder) for folder in chosen]
split_runs = [has_split(r) for r in runs]
options = [OUT_OF_SAMPLE, IN_SAMPLE, FULL] if any(split_runs) else [FULL]
period = st.radio("Period", options, format_func=PERIOD_LABELS.get, horizontal=True)
if period != FULL and not all(split_runs):
    st.caption("Runs without an in-sample/out-of-sample split show – for this period.")

st.dataframe(metrics_table(runs, period), width="stretch")

curves = growth_curves(runs, period)
if not curves.empty:
    st.altair_chart(growth_chart(curves), width="stretch")
    st.caption("Each run starts at $1 at the beginning of the period.")

st.subheader("Settings that differ")
diff = config_diff(runs)
if diff.empty:
    st.caption("The runs have identical settings.")
else:
    st.dataframe(diff, width="stretch")

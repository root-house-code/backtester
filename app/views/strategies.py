"""Strategy library: every strategy file, its validation status, and the authoring kit."""

import io
import zipfile

import pandas as pd
import streamlit as st
from common import RUNS_DIR, STRATEGIES_DIR, revalidate, strategy_files, validation

from backtester.kit import kit_files
from backtester.runfolder import list_runs
from backtester.validation import test_file_for

st.title("Strategies")

files = strategy_files()
if not files:
    st.info(f"No strategy files in {STRATEGIES_DIR}. Download the authoring kit below to start one.")

runs = list_runs(RUNS_DIR)
reports = {}
with st.spinner("Validating strategies…"):
    for path in files:
        reports[path] = validation(path)

if files:
    st.dataframe(
        pd.DataFrame([{
            "Strategy": reports[p].strategy_name or p.stem,
            "File": p.name,
            "API": reports[p].api_version,
            "Validation": reports[p].status_label(),
            "Tests": "yes" if test_file_for(p).is_file() else "–",
            "Runs": sum(r.strategy_file == p.name for r in runs),
        } for p in files]),
        hide_index=True, width="stretch",
    )
    st.caption("Runs counts every saved run of the file: a reminder of how many variants have been tried.")

    path = st.selectbox("Report", files, format_func=lambda p: p.name)
    report = reports[path]
    st.code(report.text(), language=None)
    if st.button("Revalidate"):
        revalidate(path)
        st.rerun()

st.divider()
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
    for name, text in kit_files().items():
        zf.writestr(f"backtester_kit/{name}", text)
st.download_button("Download the authoring kit", buf.getvalue(), file_name="backtester_kit.zip",
                   mime="application/zip")
st.caption("STRATEGY_API.md (the full strategy contract), a strategy template and its fixture tests. "
           f"Save new strategies to {STRATEGIES_DIR.name}/ and they appear here.")

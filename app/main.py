"""Backtester UI. Launch with:  backtest ui   (or: streamlit run app/main.py)"""

import streamlit as st

st.set_page_config(page_title="Backtester", layout="wide")

st.navigation([
    st.Page("views/strategies.py", title="Strategies"),
    st.Page("views/run.py", title="Run", default=True),
    st.Page("views/results.py", title="Results"),
    st.Page("views/trades.py", title="Audit"),
    st.Page("views/compare.py", title="Compare"),
    st.Page("views/data.py", title="Data"),
    st.Page("views/instructions.py", title="Instructions"),
    st.Page("views/readme.py", title="Read Me"),
]).run()

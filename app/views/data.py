"""Data: feeds and their checks, price cache coverage, and adding a feed from a file."""

import streamlit as st
from common import CACHE_DIR, FEEDS_DIR, feed_registry

from backtester.errors import BacktesterError
from backtester.feeds.manage import add_file_feed, feed_overview, price_cache_coverage, read_upload

st.title("Data")

# --- feeds --------------------------------------------------------------------
st.subheader("Feeds")
registry = feed_registry(refresh=False)
try:
    feeds = feed_overview(registry)
except BacktesterError as exc:
    st.error(str(exc))
    feeds = None

def short_time(value) -> str:
    """ISO timestamps as "YYYY-MM-DD HH:MM" (UTC); blanks for missing values."""
    return "" if value is None or value != value else str(value).replace("T", " ")[:16]


if feeds is not None and len(feeds):
    shown = feeds[["feed", "kind", "origin", "status", "records", "tickers", "first", "last", "warnings",
                   "fetched_at"]].copy()
    for col in ("first", "last", "fetched_at"):
        shown[col] = shown[col].map(short_time)
    for col in ("records", "tickers"):
        shown[col] = shown[col].map(lambda v: "" if v is None or v != v else str(int(v)))
    st.dataframe(
        shown,
        hide_index=True, width="stretch",
        column_config={"feed": "Feed", "kind": "Kind", "origin": "Source", "status": "Checks",
                       "records": "Records", "tickers": "Tickers", "first": "First available (UTC)",
                       "last": "Last available (UTC)", "warnings": "Warnings", "fetched_at": "Last fetch (UTC)"},
    )
    name = st.selectbox("Check report", feeds["feed"].tolist())
    row = feeds.set_index("feed").loc[name]
    if row["description"]:
        st.caption(row["description"])
    st.code(row["report"], language=None)
    if row["origin"] in ("plugin", "file"):
        label = "Fetch again" if row["origin"] == "plugin" else "Re-read files"
        if st.button(label, key=f"refresh:{name}"):
            try:
                with st.spinner(f"Refreshing {name}…"):
                    data = feed_registry(refresh=True).load(name, check=False)
                st.success(f"{name}: {len(data.records)} records.")
            except BacktesterError as exc:
                st.error(str(exc))

# --- add a feed -----------------------------------------------------------------
with st.expander("Add a feed from a file"):
    upload = st.file_uploader("CSV or Parquet file", type=["csv", "parquet"])
    if upload is not None:
        try:
            columns = list(read_upload(upload.name, upload.getvalue()).columns)
        except Exception as exc:
            st.error(f"Cannot read {upload.name}: {exc}")
            columns = []
        if columns:
            with st.form("add_feed"):
                c1, c2 = st.columns(2)
                feed_name = c1.text_input("Feed name", help="Lowercase letters, digits and _. Strategies use it "
                                                            "in requires.")
                kind = c2.selectbox("Kind", ["event", "reference"],
                                    help="event: records that appear at a moment (read with ctx.events). "
                                         "reference: a per-ticker value over time (read with ctx.ref).")
                ticker_col = c1.selectbox("Ticker column", columns,
                                          index=columns.index("ticker") if "ticker" in columns else 0)
                time_col = c2.selectbox("Time column (when each record became known)", columns,
                                        index=columns.index("available_at") if "available_at" in columns
                                        else min(1, len(columns) - 1))
                timezone = c1.text_input("Time zone for times without an offset", value="America/New_York")
                default_time = c2.text_input("Time for date-only values", value="18:00",
                                             help="After the close by default, so a date-only record can't "
                                                  "trade the same day.")
                description = st.text_input("Description (optional)")
                if st.form_submit_button("Check and add", type="primary"):
                    known = set(price_cache_coverage(CACHE_DIR)["ticker"])
                    validation, folder = add_file_feed(
                        FEEDS_DIR, feed_name, upload.name, upload.getvalue(), kind, ticker_col, time_col,
                        timezone.strip() or None, default_time.strip(), description.strip(), known)
                    if folder is None:
                        st.error("The feed was not added. Fix these and try again.")
                    else:
                        st.success(f"Added {feed_name} in {folder}. Strategies can now require it.")
                    st.code(validation.text(), language=None)

# --- price cache ------------------------------------------------------------------
st.subheader("Price cache")
coverage = price_cache_coverage(CACHE_DIR)
if coverage.empty:
    st.caption("No prices cached yet. They are fetched from Yahoo the first time a run needs them.")
else:
    missing = coverage[coverage["missing"]]
    st.caption(f"{len(coverage)} tickers in {CACHE_DIR}"
               + (f" · {len(missing)} have no data on Yahoo: {', '.join(missing['ticker'])}" if len(missing) else ""))
    coverage = coverage.assign(fetched_at=coverage["fetched_at"].map(short_time))
    st.dataframe(coverage, hide_index=True, width="stretch",
                 column_config={"ticker": "Ticker", "first": "First", "last": "Last", "rows": "Sessions",
                                "fetched_at": "Last fetch (UTC)", "missing": "No data"})

"""Audit: every trade and every signal. Select a trade to see it on a price chart."""

import pandas as pd
import streamlit as st
from charts import trade_chart
from common import run_picker

run = run_picker()
if run is None:
    st.stop()

st.title("Audit")
trades_tab, signals_tab = st.tabs(["Trades", "Signals"])

# --- trades -----------------------------------------------------------------
with trades_tab:
    trades = run.trades
    if trades.empty:
        st.info("This run made no trades.")
    else:
        c1, c2, c3 = st.columns(3)
        tickers = c1.multiselect("Ticker", sorted(trades["ticker"].unique()))
        outcome = c2.selectbox("Result", ["All", "Winners", "Losers", "Open"])
        reasons = c3.multiselect("Exit reason", sorted(trades["exit_reason"].dropna().unique()))

        view = trades
        if tickers:
            view = view[view["ticker"].isin(tickers)]
        if outcome == "Winners":
            view = view[view["pnl"] > 0]
        elif outcome == "Losers":
            view = view[view["pnl"] < 0]
        elif outcome == "Open":
            view = view[view["is_open"]]
        if reasons:
            view = view[view["exit_reason"].isin(reasons)]
        view = view.reset_index(drop=True)

        columns = ["ticker", "entry_date", "entry_price", "exit_date", "exit_price", "shares", "return",
                   "pnl_net", "sessions_held", "exit_reason", "tag"]
        if trades["tag"].isna().all():
            columns.remove("tag")
        selected = st.dataframe(
            view[columns],
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            key=f"trades:{run.folder.name}",
            column_config={
                "ticker": "Ticker",
                "entry_date": st.column_config.DateColumn("Entry"),
                "entry_price": st.column_config.NumberColumn("Entry price", format="%.2f"),
                "exit_date": st.column_config.DateColumn("Exit"),
                "exit_price": st.column_config.NumberColumn("Exit price", format="%.2f"),
                "shares": st.column_config.NumberColumn("Shares", format="%g"),
                "return": st.column_config.NumberColumn("Return", format="percent"),
                "pnl_net": st.column_config.NumberColumn("P&L", format="dollar"),
                "sessions_held": st.column_config.NumberColumn("Sessions"),
                "exit_reason": "Exit reason",
                "tag": "Tag",
            },
        )
        st.download_button("Export CSV", view.to_csv(index=False), file_name=f"{run.folder.name}-trades.csv",
                           mime="text/csv", key="export:trades")

        rows = selected.selection.rows
        if not rows:
            st.caption("Select a trade to see it on a chart.")
        else:
            trade = view.iloc[rows[0]]
            exit_label = "open" if trade["is_open"] else f"{trade['exit_date']:%Y-%m-%d} ({trade['exit_reason']})"
            st.subheader(f"{trade['ticker']} · {trade['entry_date']:%Y-%m-%d} → {exit_label}")
            signals = run.signals
            event_time = None
            if pd.notna(trade["tag"]) and "event_id" in signals:
                match = signals[signals["event_id"] == trade["tag"]]
                if len(match):
                    event_time = match["available_at"].iloc[0]
            chart = trade_chart(run.prices, trade, event_time)
            if chart is None:
                st.warning("No price data saved for this ticker in the run folder.")
            else:
                st.altair_chart(chart, width="stretch")
            caption = (f"Return {trade['return']:.2%} · {trade['sessions_held']} sessions · "
                       f"worst {trade['mae']:.2%} · best {trade['mfe']:.2%} while held")
            if event_time is not None:
                caption += f" · triggered by {match['feed'].iloc[0]} event {trade['tag']}"
            st.caption(caption)

# --- signals ----------------------------------------------------------------
with signals_tab:
    signals = run.signals
    if signals.empty:
        st.info("This run recorded no signals.")
    else:
        if "feed" not in signals:  # run folders from before feeds existed
            signals = signals.assign(feed=None, event_id=None, available_at=pd.NaT)
        events = signals[signals["decision"] == "event"]
        if len(events):
            counts = events["outcome"].fillna("no action").value_counts()
            traded = int(counts.drop(["rejected", "no action"], errors="ignore").sum())
            st.caption(f"{len(events)} feed events: {traded} traded, {int(counts.get('rejected', 0))} rejected, "
                       f"{int(counts.get('no action', 0))} no action.")

        c1, c2, c3 = st.columns(3)
        decisions = c1.multiselect("Type", sorted(signals["decision"].dropna().unique()))
        tickers = c2.multiselect("Ticker", sorted(signals["ticker"].dropna().unique()), key="signals:ticker")
        outcomes = c3.multiselect("Outcome", sorted(signals["outcome"].dropna().unique()))
        view = signals
        if decisions:
            view = view[view["decision"].isin(decisions)]
        if tickers:
            view = view[view["ticker"].isin(tickers)]
        if outcomes:
            view = view[view["outcome"].isin(outcomes)]

        # One readable row per signal: orders show their kind, rejects and events their reason.
        detail = view["outcome_reason"].astype(object).where(view["outcome_reason"].notna(), "")
        is_order = view["decision"] == "order"
        kind = view["reason"].astype(object).where(view["reason"].notna(), "")
        detail = detail.where(~is_order, kind + (": " + detail).where(detail != "", ""))
        shown = pd.DataFrame({
            "date": view["date"],
            "ticker": view["ticker"],
            "decision": view["decision"],
            "feed": view["feed"].astype(object).where(view["feed"].notna(), ""),
            "outcome": view["outcome"].astype(object).where(view["outcome"].notna(), ""),
            "detail": detail,
            "id": view["event_id"].astype(object).where(view["event_id"].notna(), view["tag"]).fillna(""),
        })
        if (shown["feed"] == "").all():
            shown = shown.drop(columns="feed")
        if (shown["id"] == "").all():
            shown = shown.drop(columns="id")
        st.dataframe(
            shown.reset_index(drop=True),
            hide_index=True,
            width="stretch",
            column_config={
                "date": st.column_config.DateColumn("Session"),
                "ticker": "Ticker",
                "decision": "Type",
                "feed": "Feed",
                "outcome": "Outcome",
                "detail": "Detail",
                "id": "Event / tag",
            },
        )
        st.download_button("Export CSV", view.to_csv(index=False), file_name=f"{run.folder.name}-signals.csv",
                           mime="text/csv", key="export:signals")
        st.caption("Events are feed records delivered to the strategy. Their outcome links to the order or "
                   "reject that used the event's id as its tag.")

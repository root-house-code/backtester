"""Altair charts for the UI. Pure functions: data in, chart out."""

from __future__ import annotations

import altair as alt
import numpy as np
import pandas as pd

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Year"]
ENTRY, EXIT, MARKED = "Entry", "Exit", "Open (marked)"


def _split_rule(split_date) -> alt.Chart:
    df = pd.DataFrame([{"date": pd.Timestamp(split_date), "label": f"In-sample ends {pd.Timestamp(split_date):%Y-%m-%d}"}])
    return alt.Chart(df).mark_rule(strokeDash=[5, 4], color="#757575").encode(
        x="date:T", tooltip=[alt.Tooltip("label:N", title="Split")])


def _with_split(chart: alt.Chart, split_date, height: int) -> alt.Chart:
    if split_date is None:
        return chart.properties(height=height)
    return alt.layer(chart, _split_rule(split_date)).properties(height=height)


def equity_chart(equity: pd.DataFrame, benchmark: str, split_date=None) -> alt.Chart:
    df = equity[["equity", "benchmark"]].rename(columns={"equity": "Strategy", "benchmark": benchmark})
    df = df.reset_index().melt("date", var_name="series", value_name="value").dropna()
    chart = alt.Chart(df).mark_line(strokeWidth=1.5).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%Y", tickCount="year")),
        y=alt.Y("value:Q", title="Equity ($)", scale=alt.Scale(zero=False)),
        color=alt.Color("series:N", title=None, legend=alt.Legend(orient="top-left"),
                        sort=["Strategy", benchmark]),
        tooltip=[alt.Tooltip("date:T"), alt.Tooltip("series:N"), alt.Tooltip("value:Q", format=",.0f")],
    )
    return _with_split(chart, split_date, 320)


def drawdown_chart(drawdown: pd.Series, split_date=None) -> alt.Chart:
    df = drawdown.rename("drawdown").reset_index()
    chart = alt.Chart(df).mark_area(opacity=0.5, line=True).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%Y", tickCount="year")),
        y=alt.Y("drawdown:Q", title="Drawdown", axis=alt.Axis(format="%")),
        tooltip=[alt.Tooltip("date:T"), alt.Tooltip("drawdown:Q", format=".1%")],
    )
    return _with_split(chart, split_date, 160)


def growth_chart(curves: pd.DataFrame) -> alt.Chart:
    """Growth of $1 for several runs (from backtester.compare.growth_curves)."""
    return alt.Chart(curves).mark_line(strokeWidth=1.5).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%Y", tickCount="year")),
        y=alt.Y("growth:Q", title="Growth of $1", scale=alt.Scale(zero=False)),
        color=alt.Color("run:N", title=None, legend=alt.Legend(orient="top-left", labelLimit=400)),
        tooltip=[alt.Tooltip("date:T"), alt.Tooltip("run:N"), alt.Tooltip("growth:Q", format=".3f")],
    ).properties(height=340)


def monthly_heatmap(table: pd.DataFrame) -> alt.Chart:
    """Year x month returns; ``table`` comes from analytics.monthly_returns."""
    t = table.copy()
    t.columns = MONTHS
    df = t.reset_index().melt("year", var_name="month", value_name="ret").dropna()
    df["year"] = df["year"].astype(str)
    limit = float(np.nanmax(np.abs(df["ret"]))) if len(df) else 0.1
    base = alt.Chart(df).encode(
        x=alt.X("month:O", sort=MONTHS, title=None, axis=alt.Axis(orient="top", labelAngle=0)),
        y=alt.Y("year:O", title=None),
    )
    cells = base.mark_rect().encode(
        color=alt.Color("ret:Q", legend=None,
                        scale=alt.Scale(scheme="redyellowgreen", domain=[-limit, limit], domainMid=0)),
        tooltip=[alt.Tooltip("year:O"), alt.Tooltip("month:O"), alt.Tooltip("ret:Q", format=".2%")],
    )
    labels = base.mark_text(fontSize=11).encode(text=alt.Text("ret:Q", format=".1%"))
    return (cells + labels).properties(height=max(60, 26 * df["year"].nunique()))


def trade_window(prices: pd.DataFrame, trade: pd.Series, context: int = 20) -> pd.DataFrame:
    """Closes for the trade's ticker from ``context`` sessions before entry to after exit."""
    px = prices[prices["ticker"] == trade["ticker"]].sort_values("date").reset_index(drop=True)
    if px.empty:
        return px
    dates = px["date"].to_numpy()
    entry_i = int(np.searchsorted(dates, np.datetime64(trade["entry_date"])))
    exit_date = trade["exit_date"]
    exit_i = len(px) - 1 if pd.isna(exit_date) else int(np.searchsorted(dates, np.datetime64(exit_date)))
    return px.iloc[max(0, entry_i - context): min(len(px), exit_i + context + 1)]


def trade_chart(prices: pd.DataFrame, trade: pd.Series, event_time=None) -> alt.Chart | None:
    """Closing-price line with entry/exit markers, stop/target lines and, when
    the trade came from a feed event, a dashed line when that event became known."""
    window = trade_window(prices, trade)
    if window.empty:
        return None
    is_open = bool(trade["is_open"])
    exit_date = window["date"].iloc[-1] if is_open else trade["exit_date"]
    marks = pd.DataFrame([
        {"date": trade["entry_date"], "price": trade["entry_price"], "event": ENTRY},
        {"date": exit_date, "price": trade["exit_price"], "event": MARKED if is_open else EXIT},
    ])
    events = [ENTRY, MARKED if is_open else EXIT]
    shapes = {ENTRY: "triangle-up", EXIT: "triangle-down", MARKED: "circle"}
    colors = {ENTRY: "#2e7d32", EXIT: "#c62828", MARKED: "#757575"}
    y = alt.Y("close:Q", title="Close (adjusted)", scale=alt.Scale(zero=False))
    layers = [
        alt.Chart(window).mark_line(strokeWidth=1.5, color="#4c78a8").encode(
            x=alt.X("date:T", title=None), y=y,
            tooltip=[alt.Tooltip("date:T"), alt.Tooltip("close:Q", format=",.2f")],
        ),
        alt.Chart(marks).mark_point(filled=True, size=140, opacity=1).encode(
            x="date:T",
            y=alt.Y("price:Q"),
            shape=alt.Shape("event:N", title=None, legend=alt.Legend(orient="top-left"),
                            scale=alt.Scale(domain=events, range=[shapes[e] for e in events])),
            color=alt.Color("event:N", title=None,
                            scale=alt.Scale(domain=events, range=[colors[e] for e in events])),
            tooltip=[alt.Tooltip("event:N"), alt.Tooltip("date:T"), alt.Tooltip("price:Q", format=",.2f")],
        ),
    ]
    levels = [(name, trade.get(col)) for name, col in (("Stop", "stop"), ("Target", "target"))]
    levels = pd.DataFrame([{"level": n, "price": v} for n, v in levels if v is not None and pd.notna(v)])
    if not levels.empty:
        layers.append(alt.Chart(levels).mark_rule(strokeDash=[4, 4], color="#757575").encode(
            y="price:Q", tooltip=[alt.Tooltip("level:N"), alt.Tooltip("price:Q", format=",.2f")],
        ))
    if event_time is not None and pd.notna(event_time):
        known = pd.Timestamp(event_time).tz_convert("America/New_York")
        ev = pd.DataFrame([{"date": known.tz_localize(None), "label": f"Event known {known:%Y-%m-%d %H:%M} ET"}])
        layers.append(alt.Chart(ev).mark_rule(strokeDash=[2, 3], color="#6a3d9a").encode(
            x="date:T", tooltip=[alt.Tooltip("label:N", title="Event")],
        ))
    return alt.layer(*layers).properties(height=320)

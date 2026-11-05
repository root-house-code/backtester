"""Comparing saved runs: metrics side by side, equity curves, and the settings that differ.

Works only from run folders (``RunData``), like the rest of the UI.
"""

from __future__ import annotations

import pandas as pd

from .runfolder import RunData

FULL, IN_SAMPLE, OUT_OF_SAMPLE = "full", "in_sample", "out_of_sample"
PERIODS = (OUT_OF_SAMPLE, IN_SAMPLE, FULL)
PERIOD_LABELS = {FULL: "Full period", IN_SAMPLE: "In-sample", OUT_OF_SAMPLE: "Out-of-sample"}

#: (section, key, label) for the comparison table, in display order.
COMPARE_ROWS = [
    ("period", "start", "Start"),
    ("period", "end", "End"),
    ("returns", "total_return", "Total return"),
    ("returns", "cagr", "CAGR"),
    ("returns", "excess_return", "Excess return vs benchmark"),
    ("risk", "volatility", "Volatility"),
    ("risk", "sharpe", "Sharpe"),
    ("risk", "sortino", "Sortino"),
    ("risk", "max_drawdown", "Max drawdown"),
    ("risk", "calmar", "Calmar"),
    ("trades", "count", "Trades"),
    ("trades", "win_rate", "Win rate"),
    ("trades", "profit_factor", "Profit factor"),
    ("trades", "expectancy", "Mean trade return"),
    ("confidence", "mean_trade_return", "Mean trade return, 95% CI"),
    ("exposure", "avg_pct_invested", "Avg % invested"),
    ("data_quality", "skip_rate", "Skipped signals"),
]
PERCENT = {"total_return", "cagr", "excess_return", "volatility", "max_drawdown", "win_rate",
           "expectancy", "avg_pct_invested", "skip_rate"}


def period_metrics(run: RunData, period: str) -> dict | None:
    """The run's metrics for a period; None if the run has no such split."""
    m = run.metrics
    if period == FULL:
        return m
    split = m.get("split")
    return split.get(period) if split else None


def has_split(run: RunData) -> bool:
    return bool(run.metrics.get("split"))


def run_label(run: RunData) -> str:
    """Strategy name plus the run folder's timestamp, e.g. "Momentum rotation · 20261101-121914"."""
    stamp = run.folder.name[:15]
    return f"{run.strategy_name} · {stamp}"


def unique_labels(runs: list[RunData]) -> list[str]:
    labels = [run_label(r) for r in runs]
    seen: dict[str, int] = {}
    out = []
    for lab in labels:
        seen[lab] = seen.get(lab, 0) + 1
        out.append(lab if labels.count(lab) == 1 else f"{lab} ({seen[lab]})")
    return out


def _fmt(key, value) -> str:
    if value is None:
        return "–"
    if isinstance(value, dict):  # confidence interval
        lo, hi = value.get("ci_low"), value.get("ci_high")
        return "–" if lo is None else f"{lo:+.2%} to {hi:+.2%}"
    if isinstance(value, float):
        return f"{value:.2%}" if key in PERCENT else f"{value:,.2f}"
    return str(value)


def metrics_table(runs: list[RunData], period: str) -> pd.DataFrame:
    """One row per metric, one column per run, formatted for display."""
    labels = unique_labels(runs)
    data = {}
    for run, label in zip(runs, labels):
        m = period_metrics(run, period)
        data[label] = [_fmt(key, (m or {}).get(section, {}).get(key)) if m else "–" for section, key, _ in COMPARE_ROWS]
    return pd.DataFrame(data, index=[label for _, _, label in COMPARE_ROWS])


def period_bounds(run: RunData, period: str):
    m = period_metrics(run, period)
    if not m:
        return None
    return pd.Timestamp(m["period"]["start"]), pd.Timestamp(m["period"]["end"])


def growth_curves(runs: list[RunData], period: str) -> pd.DataFrame:
    """Growth of 1 for each run over the period, starting from the equity just before it.

    Long format: date, run, growth. Runs without the period are left out.
    """
    frames = []
    for run, label in zip(runs, unique_labels(runs)):
        bounds = period_bounds(run, period)
        if bounds is None:
            continue
        eq = run.equity["equity"]
        before = eq[eq.index < bounds[0]]
        base = float(before.iloc[-1]) if len(before) else float(run.field("initial_capital"))
        part = eq[(eq.index >= bounds[0]) & (eq.index <= bounds[1])]
        frames.append(pd.DataFrame({"date": part.index, "run": label, "growth": part.to_numpy() / base}))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "run", "growth"])


def _flat_config(run: RunData) -> dict:
    out = {}
    for key, entry in run.config.get("fields", {}).items():
        value = entry["value"]
        if isinstance(value, list):
            value = ", ".join(value)
        elif isinstance(value, dict) and set(value) == {"window", "step"}:
            value = f"{value['window']},{value['step']}"
        out[key] = value
    for key, entry in run.config.get("params", {}).items():
        out[f"params.{key}"] = entry["value"]
    strat = run.config.get("strategy", {})
    out["strategy"] = strat.get("name")
    out["strategy file"] = strat.get("file")
    out["strategy code"] = (strat.get("sha256") or "")[:12]
    return out


def config_diff(runs: list[RunData]) -> pd.DataFrame:
    """Settings whose values differ between the runs (all runs as columns)."""
    flats = [_flat_config(r) for r in runs]
    keys = sorted(set().union(*flats))
    rows = {}
    for key in keys:
        values = [f.get(key) for f in flats]
        if len({repr(v) for v in values}) > 1:
            rows[key] = ["–" if v is None else str(v) for v in values]
    return pd.DataFrame.from_dict(rows, orient="index", columns=unique_labels(runs))

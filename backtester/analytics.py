"""Summary metrics for a finished run.

Conventions (also in DECISIONS.md):
- Daily returns come from the equity curve; the first is measured against
  initial capital. Annualization uses 252 sessions per year.
- The risk-free rate is 0 for Sharpe, Sortino and alpha.
- Trade metrics include positions still open at the end, marked at the last close.
- Turnover is annualized: (total traded value / 2) / average equity / years.
- Confidence: a seeded percentile bootstrap (10,000 resamples) of the mean trade return.
- Splits and walk-forward folds are slices of one continuous run, each measured
  from the equity it had just before the slice; trades belong to the slice
  they were entered in.
"""

from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd

SESSIONS_PER_YEAR = 252
ROUND_DIGITS = 10


def _f(x) -> float | None:
    """JSON-safe float rounded to a fixed number of significant digits."""
    if x is None:
        return None
    x = float(x)
    if not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return float(f"{x:.{ROUND_DIGITS}g}")


def _returns(values: pd.Series, initial: float) -> pd.Series:
    prev = values.shift(1)
    prev.iloc[0] = initial
    return values / prev - 1


def _drawdown(equity: pd.Series, initial: float) -> dict:
    peak = np.maximum.accumulate(np.concatenate([[initial], equity.to_numpy()]))[1:]
    dd = equity.to_numpy() / peak - 1
    if len(dd) == 0 or dd.min() >= 0:
        return {"max_drawdown": 0.0, "max_drawdown_duration_sessions": 0,
                "max_drawdown_peak": None, "max_drawdown_trough": None, "max_drawdown_recovery": None}
    trough = int(dd.argmin())
    dates = equity.index
    vals = equity.to_numpy()
    # Peak: last session at or before the trough where equity was at its running high.
    peak_val = peak[trough]
    peak_idx = None
    for k in range(trough, -1, -1):
        if vals[k] >= peak_val:
            peak_idx = k
            break
    recovery_idx = None
    for k in range(trough + 1, len(vals)):
        if vals[k] >= peak_val:
            recovery_idx = k
            break
    start = peak_idx if peak_idx is not None else -1   # -1 = initial capital before the first session
    end = recovery_idx if recovery_idx is not None else len(vals) - 1
    return {
        "max_drawdown": dd.min(),
        "max_drawdown_duration_sessions": int(end - start),
        "max_drawdown_peak": dates[peak_idx].date().isoformat() if peak_idx is not None else "start",
        "max_drawdown_trough": dates[trough].date().isoformat(),
        "max_drawdown_recovery": dates[recovery_idx].date().isoformat() if recovery_idx is not None else None,
    }


BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_LEVEL = 0.95


def bootstrap_mean_ci(values, seed: int, resamples: int = BOOTSTRAP_RESAMPLES,
                      level: float = BOOTSTRAP_LEVEL) -> dict:
    """Percentile bootstrap interval for the mean of ``values`` (e.g. trade returns).

    Resamples with replacement from a generator seeded by ``seed``, so the same
    run always gives the same interval. Needs at least two values.
    """
    x = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    out = {"n": int(len(x)), "mean": float(x.mean()) if len(x) else None,
           "ci_low": None, "ci_high": None, "level": level, "resamples": resamples}
    if len(x) < 2:
        return out
    rng = np.random.default_rng(np.random.SeedSequence([int(seed), 0xB007]))
    means = np.empty(resamples)
    chunk = max(1, min(resamples, 5_000_000 // len(x)))
    for lo in range(0, resamples, chunk):
        hi = min(resamples, lo + chunk)
        means[lo:hi] = x[rng.integers(0, len(x), size=(hi - lo, len(x)))].mean(axis=1)
    tail = (1 - level) / 2 * 100
    out["ci_low"], out["ci_high"] = (float(v) for v in np.percentile(means, [tail, 100 - tail]))
    return out


def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame, fills: pd.DataFrame,
                    signals: pd.DataFrame, initial_capital: float, bench_initial: float | None = None,
                    seed: int = 0) -> dict:
    """Summary metrics. ``initial_capital`` (and ``bench_initial`` for the
    benchmark) is the value just before the first session in ``equity``, so a
    slice of a run is measured from where it started."""
    eq = equity["equity"]
    n = len(eq)
    years = n / SESSIONS_PER_YEAR if n else float("nan")
    r = _returns(eq, initial_capital)
    bench_initial = initial_capital if bench_initial is None else bench_initial

    total_return = eq.iloc[-1] / initial_capital - 1 if n else float("nan")
    cagr = (1 + total_return) ** (1 / years) - 1 if n and total_return > -1 else float("nan")

    bench = equity["benchmark"] if "benchmark" in equity else pd.Series(dtype=float)
    has_bench = len(bench) == n and n > 0 and bench.notna().all() and np.isfinite(bench_initial)
    b_total = b_cagr = alpha = beta = None
    if has_bench:
        rb = _returns(bench, bench_initial)
        b_total = bench.iloc[-1] / bench_initial - 1
        b_cagr = (1 + b_total) ** (1 / years) - 1 if b_total > -1 else float("nan")
        var_b = rb.var(ddof=1)
        if n > 1 and var_b > 0:
            beta = r.cov(rb, ddof=1) / var_b
            alpha = (r.mean() - beta * rb.mean()) * SESSIONS_PER_YEAR

    vol = r.std(ddof=1) * math.sqrt(SESSIONS_PER_YEAR) if n > 1 else float("nan")
    sharpe = r.mean() / r.std(ddof=1) * math.sqrt(SESSIONS_PER_YEAR) if n > 1 and r.std(ddof=1) > 0 else float("nan")
    downside = math.sqrt((np.minimum(r.to_numpy(), 0) ** 2).mean()) if n else float("nan")
    sortino = r.mean() / downside * math.sqrt(SESSIONS_PER_YEAR) if downside and downside > 0 else float("nan")
    dd = _drawdown(eq, initial_capital)
    calmar = cagr / abs(dd["max_drawdown"]) if dd["max_drawdown"] < 0 else float("nan")

    # Trades
    t = trades
    count = len(t)
    wins = t[t["pnl"] > 0]
    losses = t[t["pnl"] < 0]
    gross_win = wins["pnl"].sum()
    gross_loss = -losses["pnl"].sum()
    trade_metrics = {
        "count": count,
        "closed": int((~t["is_open"]).sum()) if count else 0,
        "open_at_end": int(t["is_open"].sum()) if count else 0,
        "win_rate": len(wins) / count if count else None,
        "avg_win": wins["return"].mean() if len(wins) else None,
        "avg_loss": losses["return"].mean() if len(losses) else None,
        "avg_win_pnl": wins["pnl"].mean() if len(wins) else None,
        "avg_loss_pnl": losses["pnl"].mean() if len(losses) else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "expectancy": t["return"].mean() if count else None,
        "expectancy_pnl": t["pnl_net"].mean() if count else None,
        "avg_sessions_held": t["sessions_held"].mean() if count else None,
        "total_commission": fills["commission"].sum() if len(fills) else 0.0,
    }

    traded_value = (fills["qty"].abs() * fills["price"]).sum() if len(fills) else 0.0
    turnover = (traded_value / 2) / eq.mean() / years if n and eq.mean() > 0 else float("nan")

    # Orders and rejects count as signals; feed events are logged for the audit but are not decisions.
    decisions = signals[signals["decision"] != "event"] if len(signals) else signals
    total_signals = len(decisions)
    skipped = int(((decisions["outcome"] == "skipped") & (decisions["outcome_reason"] == "no price data")).sum()) if total_signals else 0

    metrics = {
        "period": {
            "start": eq.index[0].date().isoformat() if n else None,
            "end": eq.index[-1].date().isoformat() if n else None,
            "sessions": n,
        },
        "returns": {
            "total_return": total_return,
            "cagr": cagr,
            "benchmark_total_return": b_total,
            "benchmark_cagr": b_cagr,
            "excess_return": (total_return - b_total) if b_total is not None else None,
            "excess_cagr": (cagr - b_cagr) if b_cagr is not None else None,
            "alpha": alpha,
            "beta": beta,
            "final_equity": eq.iloc[-1] if n else None,
        },
        "risk": {
            "volatility": vol,
            "sharpe": sharpe,
            "sortino": sortino,
            "calmar": calmar,
            **dd,
        },
        "trades": trade_metrics,
        "exposure": {
            "avg_pct_invested": equity["exposure"].mean() if n else None,
            "turnover": turnover,
        },
        "data_quality": {
            "signals_total": total_signals,
            "signals_skipped_no_data": skipped,
            "skip_rate": skipped / total_signals if total_signals else 0.0,
        },
        "confidence": {
            "mean_trade_return": bootstrap_mean_ci(t["return"].tolist() if count else [], seed),
        },
    }
    return _clean(metrics)


# --- splits and walk-forward ----------------------------------------------------

def slice_metrics(equity: pd.DataFrame, trades: pd.DataFrame, fills: pd.DataFrame, signals: pd.DataFrame,
                  start, end, initial_capital: float, seed: int = 0) -> dict | None:
    """Metrics for the sessions in [start, end] of a run, measured from the
    equity (and benchmark value) the run had just before ``start``.

    Trades belong to the slice they were entered in, with their full result
    even if they exit after it. Fills and signals belong to the slice of their date.
    """
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    part = equity[(equity.index >= start) & (equity.index <= end)]
    if part.empty:
        return None
    first, last = part.index[0], part.index[-1]
    before = equity[equity.index < first]
    init = float(before["equity"].iloc[-1]) if len(before) else initial_capital
    b_init = float(before["benchmark"].iloc[-1]) if len(before) and "benchmark" in before else initial_capital

    def within(df, col):
        return df[(df[col] >= first) & (df[col] <= last)] if len(df) else df

    return compute_metrics(part, within(trades, "entry_date"), within(fills, "date"), within(signals, "date"),
                           init, bench_initial=b_init, seed=seed)


def in_out_of_sample(equity, trades, fills, signals, in_sample_end, initial_capital, seed=0) -> dict:
    """In-sample: sessions up to and including ``in_sample_end``; out-of-sample: the rest."""
    ise = pd.Timestamp(in_sample_end)
    after = equity.index[equity.index > ise]
    return {
        "in_sample_end": ise.date().isoformat(),
        "in_sample": slice_metrics(equity, trades, fills, signals, equity.index[0], ise, initial_capital, seed),
        "out_of_sample": (slice_metrics(equity, trades, fills, signals, after[0], equity.index[-1],
                                        initial_capital, seed) if len(after) else None),
    }


_LENGTH = re.compile(r"^\s*(\d+)\s*([ymwd])\s*$")


def parse_length(text: str) -> pd.DateOffset:
    """``"2y"``, ``"6m"``, ``"13w"`` or ``"90d"`` as a calendar offset."""
    m = _LENGTH.match(str(text).lower())
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"{text!r} is not a length like 2y, 6m, 13w or 90d")
    n, unit = int(m.group(1)), m.group(2)
    return pd.DateOffset(**{{"y": "years", "m": "months", "w": "weeks", "d": "days"}[unit]: n})


def walk_forward_folds(dates: pd.DatetimeIndex, window: str, step: str) -> list[dict]:
    """Rolling folds over a run's sessions.

    Fold k's in-sample window starts ``k`` steps after the first session and
    lasts ``window``; its out-of-sample period is the ``step`` after that
    (the last one is cut at the run's end). Folds stop when there is no
    out-of-sample session left.
    """
    w, s = parse_length(window), parse_length(step)
    first, last = dates[0], dates[-1]
    folds = []
    k = 0
    while True:
        is_start = first + s * k if k else first
        is_end = is_start + w          # exclusive
        oos_end = is_end + s           # exclusive
        ins = dates[(dates >= is_start) & (dates < is_end)]
        oos = dates[(dates >= is_end) & (dates < oos_end)]
        if is_end > last or len(oos) == 0:
            break
        if len(ins):
            folds.append({"fold": len(folds) + 1, "in_sample": (ins[0], ins[-1]), "out_of_sample": (oos[0], oos[-1])})
        k += 1
    return folds


def walk_forward(equity, trades, fills, signals, window: str, step: str, initial_capital, seed=0) -> dict:
    """Score a run fold by fold, plus a summary of the out-of-sample steps strung together."""
    folds = []
    oos_growth, oos_sessions = 1.0, 0
    for f in walk_forward_folds(equity.index, window, step):
        ins = slice_metrics(equity, trades, fills, signals, *f["in_sample"], initial_capital, seed)
        oos = slice_metrics(equity, trades, fills, signals, *f["out_of_sample"], initial_capital, seed)
        folds.append({"fold": f["fold"], "in_sample": ins, "out_of_sample": oos})
        oos_growth *= 1 + oos["returns"]["total_return"]
        oos_sessions += oos["period"]["sessions"]
    sharpe = [f["out_of_sample"]["risk"]["sharpe"] for f in folds if f["out_of_sample"]["risk"]["sharpe"] is not None]
    is_sharpe = [f["in_sample"]["risk"]["sharpe"] for f in folds if f["in_sample"]["risk"]["sharpe"] is not None]
    positive = [f["out_of_sample"]["returns"]["total_return"] > 0 for f in folds]
    years = oos_sessions / SESSIONS_PER_YEAR if oos_sessions else float("nan")
    summary = {
        "folds": len(folds),
        "oos_total_return": oos_growth - 1 if folds else None,
        "oos_cagr": oos_growth ** (1 / years) - 1 if folds and oos_growth > 0 else None,
        "oos_positive_share": sum(positive) / len(positive) if positive else None,
        "mean_oos_sharpe": float(np.mean(sharpe)) if sharpe else None,
        "mean_is_sharpe": float(np.mean(is_sharpe)) if is_sharpe else None,
    }
    return _clean({"window": window, "step": step, "summary": summary, "folds": folds})


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return _f(obj)
    return obj


# --- series for charts ------------------------------------------------------

def drawdown_series(equity: pd.Series, initial: float) -> pd.Series:
    """Drawdown from the running peak at each session (0 at a new high, negative below it)."""
    peak = np.maximum.accumulate(np.concatenate([[initial], equity.to_numpy(dtype=float)]))[1:]
    return pd.Series(equity.to_numpy(dtype=float) / peak - 1, index=equity.index, name="drawdown")


def monthly_returns(equity: pd.Series, initial: float) -> pd.DataFrame:
    """Calendar-month returns as a year x month table (columns 1-12), plus a "Year" column.

    Each month compounds from the previous month's last equity value; the first
    month starts from initial capital. Months outside the run are NaN.
    """
    month_end = equity.groupby([equity.index.year, equity.index.month]).last()
    prev = month_end.shift(1)
    prev.iloc[0] = initial
    monthly = (month_end / prev - 1).unstack()
    monthly = monthly.reindex(columns=range(1, 13))
    year_end = equity.groupby(equity.index.year).last()
    prev_year = year_end.shift(1)
    prev_year.iloc[0] = initial
    monthly["Year"] = year_end / prev_year - 1
    monthly.index.name = "year"
    monthly.columns.name = None
    return monthly

"""Paths and helpers shared by the UI pages."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import streamlit as st

from backtester.feeds import FeedRegistry
from backtester.runfolder import NEWEST, SORTS, RunData, list_runs, load_run, sort_runs, strategy_hypothesis
from backtester.validation import ValidationReport, test_file_for, validate

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = Path(os.environ.get("BACKTESTER_RUNS_DIR", ROOT / "runs"))
STRATEGIES_DIR = Path(os.environ.get("BACKTESTER_STRATEGIES_DIR", ROOT / "strategies"))
CACHE_DIR = Path(os.environ.get("BACKTESTER_CACHE_DIR", ROOT / "data" / "cache"))
FEEDS_DIR = Path(os.environ.get("BACKTESTER_FEEDS_DIR", ROOT / "data" / "feeds"))
PLUGINS_DIR = Path(os.environ.get("BACKTESTER_PLUGINS_DIR", ROOT / "feeds"))

CURRENT_RUN = "current_run"
RUN_SORT = "run_sort"

PERCENT_KEYS = {
    "total_return", "cagr", "benchmark_total_return", "benchmark_cagr", "excess_return",
    "excess_cagr", "alpha", "volatility", "max_drawdown", "win_rate", "avg_win", "avg_loss",
    "expectancy", "avg_pct_invested", "skip_rate",
}


def strategy_files() -> list[Path]:
    """Strategy files in the strategies folder (test files excluded)."""
    return sorted(p for p in STRATEGIES_DIR.glob("*.py") if not p.name.startswith(("test_", "_")))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


@st.cache_data(show_spinner=False, max_entries=200)
def _validate(path: str, file_hash: str, test_hash: str, quick: bool, nonce: int) -> ValidationReport:
    return validate(path, cache_dir=CACHE_DIR, quick=quick, feeds_dir=FEEDS_DIR, plugins_dir=PLUGINS_DIR)


def feed_registry(refresh: bool = True) -> FeedRegistry:
    return FeedRegistry(FEEDS_DIR, PLUGINS_DIR, CACHE_DIR, refresh=refresh)


def universe_label(universe) -> str:
    return universe if isinstance(universe, str) else ", ".join(universe)


def validation(path: Path, quick: bool = False) -> ValidationReport:
    """Validation report for a strategy file, cached until the file or its tests change."""
    nonce = st.session_state.get(f"revalidate:{path}", 0)
    return _validate(str(path), _digest(path), _digest(test_file_for(path)), quick, nonce)


def revalidate(path: Path) -> None:
    key = f"revalidate:{path}"
    st.session_state[key] = st.session_state.get(key, 0) + 1


@st.cache_data(show_spinner=False)
def _load(folder: str, stamp: float) -> RunData:
    return load_run(folder)


def hypothesis(run: RunData) -> str:
    """What the run's strategy tests: from the copy saved with the run, or else from the current
    strategy file (so runs made before a hypothesis was written still show one)."""
    if run.hypothesis:
        return run.hypothesis
    path = STRATEGIES_DIR / run.strategy_file if run.strategy_file else None
    if path is None or not path.is_file():
        return ""
    return strategy_hypothesis(path.read_text(encoding="utf-8"))


def load(folder: str) -> RunData:
    stamp = (Path(folder) / "metrics.json").stat().st_mtime
    return _load(folder, stamp)


def run_picker() -> RunData | None:
    """Select box of finished runs, shared across pages. Returns the chosen run. It sits at the top
    of the page, full width, rather than in the sidebar, so each run's label fits."""
    runs = list_runs(RUNS_DIR)
    if not runs:
        st.info("No runs yet. Start one on the Run page.")
        return None
    pick, order = st.columns([5, 1])
    sorts = [NEWEST, *SORTS]
    saved = st.session_state.get(RUN_SORT, NEWEST)
    sort = order.selectbox("Sort by", sorts, index=sorts.index(saved) if saved in sorts else 0,
                           format_func=lambda k: "Newest" if k == NEWEST else SORTS[k][0])
    st.session_state[RUN_SORT] = sort
    runs = sort_runs(runs, sort)
    folders = [str(r.folder) for r in runs]
    labels = {str(r.folder): r.label_for(sort) for r in runs}
    current = st.session_state.get(CURRENT_RUN)
    index = folders.index(current) if current in folders else 0
    choice = pick.selectbox("Run", folders, index=index, format_func=labels.get)
    st.session_state[CURRENT_RUN] = choice
    return load(choice)


def fmt(key: str, value) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        if key in PERCENT_KEYS:
            return f"{value:.2%}"
        return f"{value:,.2f}"
    return str(value)

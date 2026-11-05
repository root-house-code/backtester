"""Read finished run folders. The UI uses only this to get results."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class RunSummary:
    folder: Path
    strategy: str
    start: str | None
    end: str | None
    total_return: float | None
    strategy_file: str | None = None
    params: dict = field(default_factory=dict)   # the parameters the run overrode
    metrics: dict = field(default_factory=dict)  # full-period headline numbers, keyed as in SORTS

    @property
    def run_id(self) -> str:
        """The folder's timestamp, e.g. ``20261102-213144`` (the rest of the name repeats the strategy)."""
        return "-".join(self.folder.name.split("-")[:2])

    @property
    def label(self) -> str:
        """One line that tells runs apart: when, what, which parameters were changed, dates, return."""
        ret = "n/a" if self.total_return is None else f"{self.total_return:+.1%}"
        parts = [self.run_id, self.strategy]
        if self.params:
            parts.append(", ".join(f"{k}={_param_text(v)}" for k, v in self.params.items()))
        parts += [f"{self.start} → {self.end}", ret]
        return " · ".join(parts)

    def label_for(self, sort: str) -> str:
        """The label, plus the value being sorted by when the label doesn't already show it."""
        if sort not in SORTS or sort == "total_return":
            return self.label
        name, kind, _ = SORTS[sort]
        return f"{self.label} · {name} {_metric_text(self.metrics.get(sort), kind)}"


# Run orderings: key -> (name, how to show the value, True if bigger is better). Max drawdown is
# negative, so bigger (closer to zero) is better too.
NEWEST = "newest"
SORTS = {
    "total_return": ("Total return", "pct", True),
    "cagr": ("CAGR", "pct", True),
    "excess_return": ("Return vs benchmark", "pct", True),
    "sharpe": ("Sharpe", "num", True),
    "sortino": ("Sortino", "num", True),
    "max_drawdown": ("Max drawdown", "pct", True),
    "trades": ("Trades", "int", True),
}


def sort_runs(runs: list[RunSummary], by: str) -> list[RunSummary]:
    """Runs ordered best first by one of SORTS (``NEWEST`` keeps folder order). Runs without the
    value go last, newest first among themselves."""
    if by not in SORTS:
        return list(runs)
    sign = -1 if SORTS[by][2] else 1
    have = [r for r in runs if r.metrics.get(by) is not None]
    missing = [r for r in runs if r.metrics.get(by) is None]
    return sorted(have, key=lambda r: sign * r.metrics[by]) + missing


def _metric_text(value, kind: str) -> str:
    if value is None:
        return "n/a"
    return {"pct": f"{value:+.1%}", "num": f"{value:.2f}", "int": f"{int(value)}"}[kind]


def _headline(metrics: dict) -> dict:
    returns, risk = metrics.get("returns", {}), metrics.get("risk", {})
    return {
        "total_return": returns.get("total_return"),
        "cagr": returns.get("cagr"),
        "excess_return": returns.get("excess_return"),
        "sharpe": risk.get("sharpe"),
        "sortino": risk.get("sortino"),
        "max_drawdown": risk.get("max_drawdown"),
        "trades": metrics.get("trades", {}).get("count"),
    }


def _param_text(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


@dataclass
class RunData:
    folder: Path
    config: dict
    metrics: dict
    manifest: dict
    equity: pd.DataFrame
    trades: pd.DataFrame
    signals: pd.DataFrame
    orders: pd.DataFrame
    prices: pd.DataFrame
    log: str
    strategy_source: str

    def field(self, name: str):
        """A resolved config value, e.g. ``run.field("initial_capital")``."""
        return self.config["fields"][name]["value"]

    @property
    def strategy_name(self) -> str:
        return self.config.get("strategy", {}).get("name", self.folder.name)

    @property
    def strategy_file(self) -> str | None:
        return self.config.get("strategy", {}).get("file")

    @property
    def hypothesis(self) -> str:
        """The strategy's ``hypothesis`` as saved with the run ("" if it had none then)."""
        return strategy_hypothesis(self.strategy_source)


def strategy_hypothesis(source: str) -> str:
    """The ``hypothesis = "..."`` string in a strategy file's class body, read without importing it."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            target = stmt.target if isinstance(stmt, ast.AnnAssign) else (
                stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else None)
            value = getattr(stmt, "value", None)
            if (isinstance(target, ast.Name) and target.id == "hypothesis"
                    and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return " ".join(value.value.split())
    return ""


def is_run_folder(path: Path) -> bool:
    return path.is_dir() and (path / "metrics.json").is_file() and (path / "config.json").is_file()


def list_runs(runs_dir: str | Path) -> list[RunSummary]:
    """Finished runs, newest first (folder names start with a timestamp)."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    out = []
    for folder in sorted((p for p in runs_dir.iterdir() if is_run_folder(p)), key=lambda p: p.name, reverse=True):
        try:
            metrics = json.loads((folder / "metrics.json").read_text())
            config = json.loads((folder / "config.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append(RunSummary(
            folder=folder,
            strategy=config.get("strategy", {}).get("name", folder.name),
            start=metrics.get("period", {}).get("start"),
            end=metrics.get("period", {}).get("end"),
            total_return=metrics.get("returns", {}).get("total_return"),
            strategy_file=config.get("strategy", {}).get("file"),
            params={k: v.get("value") for k, v in config.get("params", {}).items()
                    if v.get("source") == "override"},
            metrics=_headline(metrics),
        ))
    return out


def _parquet(folder: Path, name: str) -> pd.DataFrame:
    path = folder / f"{name}.parquet"
    return pd.read_parquet(path) if path.is_file() else pd.DataFrame()


def load_run(folder: str | Path) -> RunData:
    folder = Path(folder)
    if not is_run_folder(folder):
        raise FileNotFoundError(f"not a run folder: {folder}")
    equity = _parquet(folder, "equity")
    if "date" in equity:
        equity = equity.set_index("date")
    return RunData(
        folder=folder,
        config=json.loads((folder / "config.json").read_text()),
        metrics=json.loads((folder / "metrics.json").read_text()),
        manifest=json.loads((folder / "data_manifest.json").read_text())
        if (folder / "data_manifest.json").is_file() else {},
        equity=equity,
        trades=_parquet(folder, "trades"),
        signals=_parquet(folder, "signals"),
        orders=_parquet(folder, "orders"),
        prices=_parquet(folder, "prices"),
        log=(folder / "log.txt").read_text(encoding="utf-8") if (folder / "log.txt").is_file() else "",
        strategy_source=(folder / "strategy.py").read_text(encoding="utf-8")
        if (folder / "strategy.py").is_file() else "",
    )

"""Run config resolution.

Precedence for any value: override (CLI, later the UI) -> strategy declaration
-> engine default. The resolved config records which level each value came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .errors import ConfigError, StrategyError
from .strategy import Strategy

ENGINE_DEFAULTS: dict[str, Any] = {
    "initial_capital": 100_000.0,
    "commission_per_share": 0.0,
    "commission_min": 0.0,
    "slippage_bps": 0.0,
    "max_adv_pct": None,          # percent of 20-day average dollar volume; None = off
    "cash_shortfall": "resize",   # or "reject"
    "benchmark": "SPY",
    "in_sample_end": None,        # M5
    "walk_forward": None,         # M5
    "fractional_shares": False,
    "seed": 0,
}

OVERRIDE_KEYS = {"start", "end", "universe", "params", *ENGINE_DEFAULTS}

DEFAULT_UNIVERSES_DIR = Path("data") / "universes"

ENGINE = "engine"
STRATEGY = "strategy"
OVERRIDE = "override"


@dataclass
class ResolvedConfig:
    """A fully resolved run config. ``sources`` names the level each value came from."""

    values: dict[str, Any]
    sources: dict[str, str]
    params: dict[str, Any]
    param_sources: dict[str, str]
    strategy: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key):
        return self.values[key]

    @property
    def start(self) -> pd.Timestamp:
        return pd.Timestamp(self.values["start"])

    @property
    def end(self) -> pd.Timestamp:
        return pd.Timestamp(self.values["end"])

    def to_dict(self) -> dict:
        def entry(v, s):
            return {"value": v, "source": s}

        return {
            "strategy": self.strategy,
            "fields": {k: entry(self.values[k], self.sources[k]) for k in sorted(self.values)},
            "params": {k: entry(self.params[k], self.param_sources[k]) for k in sorted(self.params)},
        }


def _date(value, key) -> str:
    try:
        return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a date, got {value!r}") from None


FROM_FEED = "from_feed:"


def universe_feed(universe) -> str | None:
    """The feed name for a ``"from_feed:<name>"`` universe, else None."""
    if isinstance(universe, str) and universe.strip().startswith(FROM_FEED):
        name = universe.strip()[len(FROM_FEED):].strip()
        if not name:
            raise ConfigError('from_feed universe needs a feed name, e.g. "from_feed:my_events"')
        return name
    return None


def resolve_universe(universe, universes_dir: Path | None = None) -> list[str]:
    """A static universe as a ticker list. ``from_feed:`` universes are resolved
    by the run once the feed is loaded (see ``universe_feed``)."""
    if isinstance(universe, str):
        if universe_feed(universe) is not None:
            raise ConfigError(f"{universe!r} is resolved from its feed when the run starts")
        path = (universes_dir or DEFAULT_UNIVERSES_DIR) / f"{universe}.txt"
        if not path.is_file():
            raise ConfigError(f"unknown universe list {universe!r} (looked for {path})")
        lines = path.read_text().splitlines()
        universe = [ln.split("#")[0].strip() for ln in lines]
    tickers = []
    for t in universe:
        if not isinstance(t, str) or not t.strip():
            continue
        t = t.strip().upper()
        if t not in tickers:
            tickers.append(t)
    if not tickers:
        raise ConfigError("universe is empty")
    return tickers


def resolve_config(
    strategy_cls: type[Strategy],
    overrides: dict[str, Any],
    universes_dir: Path | None = None,
) -> ResolvedConfig:
    """Resolve a run config from overrides, strategy declarations and engine defaults."""
    unknown = set(overrides) - OVERRIDE_KEYS
    if unknown:
        raise ConfigError(f"unknown config fields: {sorted(unknown)}")

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for key in ("start", "end"):
        if overrides.get(key) is None:
            raise ConfigError(f"{key} is required")
        values[key] = _date(overrides[key], key)
        sources[key] = OVERRIDE
    if values["start"] > values["end"]:
        raise ConfigError("start must be on or before end")

    if overrides.get("universe") is not None:
        universe, sources["universe"] = overrides["universe"], OVERRIDE
    else:
        universe, sources["universe"] = strategy_cls.universe, STRATEGY
    feed = universe_feed(universe)
    if feed is not None:
        if feed not in strategy_cls.requires:
            raise ConfigError(f"universe from_feed:{feed} needs {feed!r} in the strategy's requires")
        values["universe"] = f"{FROM_FEED}{feed}"
    else:
        values["universe"] = resolve_universe(universe, universes_dir)

    for key, default in ENGINE_DEFAULTS.items():
        if overrides.get(key) is not None:
            values[key], sources[key] = overrides[key], OVERRIDE
        else:
            values[key], sources[key] = default, ENGINE
    _check_engine_values(values)

    params, param_sources = {}, {}
    given = dict(overrides.get("params") or {})
    extra = set(given) - set(strategy_cls.params)
    if extra:
        raise ConfigError(f"unknown strategy params: {sorted(extra)}")
    for name, p in strategy_cls.params.items():
        try:
            if name in given:
                params[name], param_sources[name] = p.coerce(given[name], name), OVERRIDE
            else:
                params[name], param_sources[name] = p.default, STRATEGY
        except StrategyError as exc:
            raise ConfigError(str(exc)) from None

    return ResolvedConfig(values, sources, params, param_sources)


def _check_engine_values(v: dict) -> None:
    try:
        v["initial_capital"] = float(v["initial_capital"])
        v["commission_per_share"] = float(v["commission_per_share"])
        v["commission_min"] = float(v["commission_min"])
        v["slippage_bps"] = float(v["slippage_bps"])
        v["seed"] = int(v["seed"])
        if v["max_adv_pct"] is not None:
            v["max_adv_pct"] = float(v["max_adv_pct"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid numeric config value: {exc}") from None
    if v["initial_capital"] <= 0:
        raise ConfigError("initial_capital must be positive")
    for key in ("commission_per_share", "commission_min", "slippage_bps"):
        if v[key] < 0:
            raise ConfigError(f"{key} must be >= 0")
    if v["max_adv_pct"] is not None and not 0 < v["max_adv_pct"] <= 100:
        raise ConfigError("max_adv_pct must be in (0, 100]")
    if v["cash_shortfall"] not in ("resize", "reject"):
        raise ConfigError("cash_shortfall must be 'resize' or 'reject'")
    v["fractional_shares"] = bool(v["fractional_shares"])
    v["benchmark"] = str(v["benchmark"]).strip().upper()
    if v["in_sample_end"] is not None:
        ise = _date(v["in_sample_end"], "in_sample_end")
        if not v["start"] <= ise < v["end"]:
            raise ConfigError(f"in_sample_end {ise} must be on or after start ({v['start']}) and before end "
                              f"({v['end']}), so both periods have sessions")
        v["in_sample_end"] = ise
    if v["walk_forward"] is not None:
        v["walk_forward"] = parse_walk_forward(v["walk_forward"])


def parse_walk_forward(value) -> dict:
    """``{"window": "2y", "step": "6m"}`` from that dict or from ``"2y,6m"``."""
    from .analytics import parse_length

    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 2:
            raise ConfigError(f'walk_forward must be "window,step" such as "2y,6m", got {value!r}')
        value = {"window": parts[0], "step": parts[1]}
    if not isinstance(value, dict) or set(value) != {"window", "step"}:
        raise ConfigError(f'walk_forward must have a window and a step, e.g. {{"window": "2y", "step": "6m"}}; got {value!r}')
    out = {}
    for key in ("window", "step"):
        text = str(value[key]).strip().lower()
        try:
            parse_length(text)
        except ValueError as exc:
            raise ConfigError(f"walk_forward {key}: {exc}") from None
        out[key] = text
    return out

"""The Strategy base class, typed parameters, and strategy-file loading."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .errors import StrategyError

SUPPORTED_API_VERSIONS = frozenset({1})
# Built-in data. Other feed names are checked against the feed registry when a run loads them.
BUILTIN_FEEDS = frozenset({"prices", "shares_outstanding", "market_cap"})

_PARAM_TYPES = (int, float, bool, str)


class Param:
    """A typed strategy parameter. Each one becomes a run-config field and a UI field.

    Args:
        type: One of ``int``, ``float``, ``bool`` or ``str``.
        default: Value used when the run config does not override it. Must be
            inside ``[min, max]`` and in ``choices`` when those are given.
        min: Smallest allowed value (numbers only), inclusive.
        max: Largest allowed value (numbers only), inclusive.
        choices: Allowed values, for a fixed set of options.
        description: One line shown next to the field in the UI.

    Example:
        ``"lookback": Param(int, default=126, min=20, max=252)``
    """

    def __init__(self, type, default, min=None, max=None, choices=None, description: str = ""):
        if type not in _PARAM_TYPES:
            raise StrategyError(f"Param type must be one of int, float, bool, str; got {type!r}")
        self.type = type
        self.min = min
        self.max = max
        self.choices = tuple(choices) if choices is not None else None
        self.description = description
        self.default = self.coerce(default, name="default")

    def coerce(self, value: Any, name: str = "value") -> Any:
        """Convert ``value`` (possibly a string from the CLI) to this param's type and check its range."""
        try:
            if self.type is bool:
                if isinstance(value, str):
                    low = value.strip().lower()
                    if low in ("1", "true", "yes", "on"):
                        value = True
                    elif low in ("0", "false", "no", "off"):
                        value = False
                    else:
                        raise ValueError(value)
                elif not isinstance(value, bool):
                    raise ValueError(value)
            elif self.type is int:
                if isinstance(value, bool):
                    raise ValueError(value)
                if isinstance(value, float):
                    if not value.is_integer():
                        raise ValueError(value)
                    value = int(value)
                value = int(value)
            elif self.type is float:
                if isinstance(value, bool):
                    raise ValueError(value)
                value = float(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            raise StrategyError(
                f"param {name} {value!r} is not a valid {self.type.__name__}"
            ) from None
        if self.min is not None and value < self.min:
            raise StrategyError(f"param {name} {value!r} is below min {self.min!r}")
        if self.max is not None and value > self.max:
            raise StrategyError(f"param {name} {value!r} is above max {self.max!r}")
        if self.choices is not None and value not in self.choices:
            raise StrategyError(f"param {name} {value!r} is not one of {list(self.choices)!r}")
        return value

    def describe(self) -> dict:
        return {
            "type": self.type.__name__,
            "default": self.default,
            "min": self.min,
            "max": self.max,
            "choices": list(self.choices) if self.choices is not None else None,
            "description": self.description,
        }


class ParamValues:
    """Read-only resolved parameter values, accessed as attributes: ``self.p.lookback``."""

    def __init__(self, values: dict):
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(f"no parameter named {name!r}") from None

    def __setattr__(self, name, value):
        raise AttributeError("strategy parameters are read-only")

    def __getitem__(self, name):
        return self._values[name]

    def as_dict(self) -> dict:
        return dict(self._values)

    def __repr__(self):
        return f"ParamValues({dict(self._values)!r})"


class Strategy:
    """Base class for strategies. A strategy file contains exactly one subclass.

    Required class attributes:
        api_version: Strategy contract version. Only ``1`` is supported.
        name: Short name shown in the strategy library.
        description: One sentence on what the strategy does.
        requires: Data the engine must load, by name: ``"prices"`` plus any
            feeds, e.g. ``["prices", "my_events", "market_cap"]``.
        universe: Default tickers: a list such as ``["SPY", "QQQ"]``, the
            name of a ticker list in ``data/universes/<name>.txt``, or
            ``"from_feed:<name>"`` for every ticker a feed has mentioned so far
            (the feed must be in ``requires``).
        params: Dict of ``Param`` declarations. Resolved values are available
            as ``self.p.<name>``.

    Optional class attributes:
        hypothesis: What the strategy tests, in one or two plain sentences:
            the idea with its specifics (tickers, signal, hold) and why it
            should work. Shown under the strategy's name on the Results page.

    Hooks (all receive the point-in-time context ``ctx``):
        on_start(ctx): Once, before the first ``on_bar``.
        on_bar(ctx): Once per session, before the open. Place orders here.
        on_fill(ctx, fill): After each fill. Orders placed here fill next session.
        on_end(ctx): Once, after the last session is marked.

    The strategy must touch the market only through ``ctx``, and must not use
    randomness other than ``ctx.rng``.
    """

    api_version: int | None = None
    name: str = ""
    description: str = ""
    hypothesis: str = ""
    requires: list = ["prices"]
    universe: list | str = []
    params: dict = {}

    def __init__(self, params: dict | None = None):
        #: Resolved parameter values, e.g. ``self.p.lookback``.
        self.p = ParamValues(params or {})

    def on_start(self, ctx) -> None:
        """Called once before the first session's ``on_bar``."""

    def on_bar(self, ctx) -> None:
        """Called once per session, before the open. Must be overridden."""
        raise NotImplementedError("strategies must implement on_bar(ctx)")

    def on_fill(self, ctx, fill) -> None:
        """Called after each fill with a ``Fill`` record."""

    def on_end(self, ctx) -> None:
        """Called once after the last session is marked to market."""


def check_strategy_class(cls: type) -> None:
    """Check the declarations of a Strategy subclass. Raises StrategyError."""
    label = getattr(cls, "__name__", repr(cls))
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise StrategyError(f"{label} is not a Strategy subclass")
    if cls.api_version not in SUPPORTED_API_VERSIONS:
        raise StrategyError(
            f"{label}: api_version {cls.api_version!r} is not supported "
            f"(supported: {sorted(SUPPORTED_API_VERSIONS)})"
        )
    if not isinstance(cls.name, str) or not cls.name.strip():
        raise StrategyError(f"{label}: name must be a non-empty string")
    if not isinstance(cls.description, str) or not cls.description.strip():
        raise StrategyError(f"{label}: description must be a non-empty string")
    if not isinstance(cls.requires, (list, tuple)) or not all(isinstance(r, str) and r for r in cls.requires):
        raise StrategyError(f"{label}: requires must be a list of feed names")
    if not isinstance(cls.params, dict):
        raise StrategyError(f"{label}: params must be a dict of Param")
    for pname, p in cls.params.items():
        if not isinstance(p, Param):
            raise StrategyError(f"{label}: param {pname!r} must be a Param(...)")
    if not isinstance(cls.universe, (list, tuple, str)):
        raise StrategyError(f"{label}: universe must be a list of tickers or a list name")
    if cls.on_bar is Strategy.on_bar:
        raise StrategyError(f"{label}: on_bar(ctx) is not implemented")


def load_strategy_file(path: str | Path) -> type[Strategy]:
    """Import a strategy file and return its single Strategy subclass."""
    path = Path(path).resolve()
    if not path.is_file():
        raise StrategyError(f"strategy file not found: {path}")
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()[:16]
    module_name = f"_bt_strategy_{path.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise StrategyError(f"{path.name}: failed to import: {exc!r}") from exc
    classes = [
        obj for obj in vars(module).values()
        if inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy
        and obj.__module__ == module_name
    ]
    if len(classes) != 1:
        raise StrategyError(
            f"{path.name}: expected exactly one Strategy subclass, found {len(classes)}"
        )
    check_strategy_class(classes[0])
    return classes[0]

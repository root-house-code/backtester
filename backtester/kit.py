"""The strategy authoring kit.

``backtest kit`` writes:

    kit/STRATEGY_API.md       the full strategy contract, generated from the
                              engine's own docstrings and validation rules
    kit/my_strategy.py        a strategy template
    kit/test_my_strategy.py   fixture tests for the template

STRATEGY_API.md is self-contained: paste it into the chat that writes
strategies. It is generated, never edited by hand; a test fails if the
committed copy differs from what the code would generate, so it cannot drift
from the API.
"""

from __future__ import annotations

import inspect
import textwrap
from pathlib import Path

from . import __version__
from . import engine as engine_module
from .context import Context, Position
from .data.panel import Bar
from .feeds import Event, Feed
from .feeds.sources import FileFeed
from .orders import Fill
from .strategy import BUILTIN_FEEDS, SUPPORTED_API_VERSIONS, Param, Strategy
from .testing import FixtureResult, make_fixture, run_on_fixture
from .validation import (
    ALLOWED_IMPORTS,
    BANNED_CALLS,
    CLOCK_METHODS,
    FILE_METHODS,
    NUMPY_FILE_FUNCS,
    STAGES,
)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_STRATEGIES = ["buy_and_hold.py", "momentum_rotation.py", "test_momentum_rotation.py",
                        "event_hold.py", "test_event_hold.py"]

TEMPLATE_STRATEGY = '''\
from backtester import Param, Strategy


class MyStrategy(Strategy):
    api_version = 1
    name = "My strategy"
    description = "Buy a close above the prior N-session high; exit after a fixed number of sessions."
    requires = ["prices"]
    universe = ["SPY", "QQQ", "IWM"]
    params = {
        "lookback": Param(int, default=20, min=5, max=250, description="Sessions in the prior high"),
        "hold": Param(int, default=10, min=1, max=100, description="Sessions to hold each entry"),
        "size": Param(float, default=0.25, min=0.01, max=0.33, description="Fraction of equity per entry"),
    }

    def on_bar(self, ctx):
        # Decided before today's open; the last row is yesterday's close.
        closes = ctx.history("close", self.p.lookback + 1)
        if len(closes) < self.p.lookback + 1:
            return  # not enough history yet
        for ticker in ctx.universe:
            if ctx.position(ticker).qty > 0:
                continue  # already holding; the exit is scheduled
            prior_high = closes[ticker].iloc[:-1].max()
            if closes[ticker].iloc[-1] > prior_high:
                ctx.order(ticker, pct=self.p.size, at="open", exit_after=self.p.hold, tag="breakout")
'''

TEMPLATE_TEST = '''\
from my_strategy import MyStrategy

from backtester.testing import make_fixture, run_on_fixture


def test_buys_at_the_open_after_a_breakout():
    # 30 flat sessions, then session 30 closes above the prior 20-session high.
    fx = make_fixture(prices={"AAA": [100.0] * 30 + [105.0] * 6, "BBB": [50.0] * 36})
    result = run_on_fixture(MyStrategy, fx)
    assert result.orders["ticker"].tolist() == ["AAA"]
    assert result.orders["session"].tolist() == [31]  # decided before session 31's open
    assert result.orders["status"].tolist() == ["filled"]


def test_exits_after_the_hold_period():
    fx = make_fixture(prices={"AAA": [100.0] * 30 + [105.0] * 20})
    result = run_on_fixture(MyStrategy, fx, params={"hold": 5})
    trade = result.trades.iloc[0]
    assert trade["exit_reason"] == "exit_after"
    assert trade["sessions_held"] == 5
'''


# --- formatting helpers ------------------------------------------------------

def _ann(a) -> str:
    if isinstance(a, str):
        return a
    return getattr(a, "__name__", str(a))


def signature(fn, name: str | None = None) -> str:
    sig = inspect.signature(fn)
    parts = []
    for p in sig.parameters.values():
        if p.name == "self":
            continue
        prefix = {p.VAR_POSITIONAL: "*", p.VAR_KEYWORD: "**"}.get(p.kind, "")
        s = prefix + p.name
        if p.annotation is not p.empty:
            s += f": {_ann(p.annotation)}"
        if p.default is not p.empty:
            s += (" = " if p.annotation is not p.empty else "=") + repr(p.default)
        parts.append(s)
    ret = "" if sig.return_annotation is sig.empty else f" -> {_ann(sig.return_annotation)}"
    return f"{name or fn.__name__}({', '.join(parts)}){ret}"


def _doc(obj) -> str:
    return inspect.getdoc(obj) or ""


def _context_members():
    for name, member in Context.__dict__.items():
        if name.startswith("_"):
            continue
        if isinstance(member, property):
            yield f"ctx.{name}", _doc(member.fget), True
        elif isinstance(member, staticmethod):
            continue
        elif callable(member):
            yield f"ctx.{signature(member, name)}", _doc(member), False


def _fence(code: str, lang: str = "python") -> str:
    return f"```{lang}\n{code.rstrip()}\n```"


# --- the document ------------------------------------------------------------

def build_api_doc(root: Path = ROOT) -> str:
    """Generate STRATEGY_API.md from the engine's docstrings and rules."""
    v = sorted(SUPPORTED_API_VERSIONS)[-1]
    out: list[str] = []
    add = out.append

    add(f"# Backtester strategy API (api_version {v})\n")
    add(f"Generated by `backtest kit` from backtester {__version__}. Do not edit by hand; "
        "regenerate it instead.\n")
    add("This document is the complete contract for writing a strategy. A strategy is one Python "
        "file in the `strategies/` folder containing exactly one `Strategy` subclass, plus an "
        "optional `test_<file name>.py` with fixture tests. The app validates every file before "
        "it runs and reports each problem with its file, line, rule and a fix hint.\n")

    add("## Quick start\n")
    add("Copy this template, rename the class and file, and change the logic.\n")
    add(_fence(TEMPLATE_STRATEGY) + "\n")

    add("## How a run works\n")
    add(_fence(_doc(engine_module), "text") + "\n")
    add("- Decisions in `on_bar` are made before the session's open, using bars through the previous "
        "close. Today's bar is never visible.\n"
        "- `at=\"open\"` orders fill at today's open; `at=\"close\"` orders fill at today's close.\n"
        "- Prices are adjusted for splits and dividends.\n"
        "- `pct`, `value` and `target_weights` orders are converted to shares at the fill price.\n"
        "- Within a batch, sells fill before buys. A buy larger than available cash is resized to "
        "fit (or rejected, if the run is set that way).\n"
        "- Orders last one session. An order for a ticker with no bar that session is skipped and "
        "logged.\n"
        "- Long-only: sells are capped at the position. Whole shares unless the run enables "
        "fractional shares.\n")

    add("## The Strategy class\n")
    add(_fence(f"class {Strategy.__name__}", "python") + "\n")
    add(_doc(Strategy) + "\n")
    add(f"Supported `api_version` values: {', '.join(str(x) for x in sorted(SUPPORTED_API_VERSIONS))}. "
        f"Built-in names for `requires`: {', '.join(f'`{f}`' for f in sorted(BUILTIN_FEEDS))}, plus any "
        "feed added under `data/feeds/` or `feeds/` (see Feeds below).\n")

    add("## Param\n")
    add(_fence(signature(Param.__init__, "Param")) + "\n")
    add(_doc(Param) + "\n")

    add("## ctx: the strategy's view of the market and the account\n")
    add(_doc(Context) + "\n")
    for title, doc, is_prop in _context_members():
        add(f"### `{title}`" + (" (property)" if is_prop else "") + "\n")
        add(doc + "\n")
    add("`ctx.state` is a dict that persists across sessions. `ctx.rng` is a seeded "
        "`numpy.random.Generator`, the only allowed source of randomness.\n")

    add("## Feeds: events and reference data\n")
    add("Prices are built in. Anything else arrives as a **feed**, named in `requires`:\n\n"
        "- **Event feeds** hold records that appear at a moment (filings, earnings, your own signals). "
        "Read them with `ctx.events(name)`. Each record is delivered once, on the first session whose "
        "09:30 ET open comes after its `available_at`, so acting `at=\"open\"` trades that session's open. "
        "A record published after the open, or with a date but no time, trades the next session.\n"
        "- **Reference feeds** hold a per-ticker value that changes over time (sector, shares outstanding). "
        "Read the latest value known before today's open with `ctx.ref(ticker, field)`.\n"
        "- Built-in: `shares_outstanding` (Yahoo, as reported) and `market_cap` (last raw close x the latest "
        "shares outstanding).\n"
        "- `universe = \"from_feed:<name>\"` makes the universe every ticker the feed has mentioned in "
        "records visible so far. It grows during the run and never includes tickers mentioned later.\n"
        "- Tag each order with the event's id (`tag=e.id`) and pass the event to `ctx.reject(e, reason)` "
        "when skipping it. The signal audit then shows what happened to every event.\n"
        "- An event for a ticker with no price data is skipped and logged; the skip rate is a headline metric.\n")
    add(_fence('''\
class EventHold(Strategy):
    requires = ["prices", "my_events", "market_cap"]
    universe = "from_feed:my_events"
    params = {"hold_days": Param(int, default=30, min=1, max=250), "cap_max": Param(float, default=2e9, min=0)}

    def on_bar(self, ctx):
        for e in ctx.events("my_events"):
            if ctx.ref(e.ticker, "market_cap") > self.p.cap_max:
                ctx.reject(e, "above cap_max")
                continue
            ctx.order(e.ticker, pct=0.05, at="open", exit_after=self.p.hold_days, tag=e.id)''') + "\n")
    add("### `Event`\n")
    add(_doc(Event) + "\n")
    add("### Adding a feed from a file\n")
    add("Drop CSV or Parquet files into `data/feeds/<name>/` next to a `feed.yaml`. No code is needed. "
        "Every record needs a ticker and an `available_at` time: when the information became knowable, "
        "not when the underlying thing happened.\n")
    add(_fence(textwrap.dedent(_doc(FileFeed).split("::", 1)[1]).strip("\n"), "yaml") + "\n")
    add("### Adding a feed as a Python plug-in\n")
    add("For data that must be fetched or derived, put one `Feed` subclass in a `.py` file in `feeds/`. "
        "Plug-ins are not strategies: the strategy rules above do not apply to them.\n")
    add(_doc(Feed) + "\n")
    add(f"`{signature(Feed.fetch)}`: " + _doc(Feed.fetch) + "\n")
    add("Every feed is checked when it is loaded: required columns present, timestamps with a time zone, "
        "none in the future, duplicates dropped with a warning, and tickers without cached prices counted. "
        "Check feeds with `backtest feeds`.\n")

    add("## Records\n")
    for cls in (Position, Fill, Bar):
        add(f"### `{cls.__name__}`\n")
        add(_doc(cls) + "\n")

    add("## Errors\n")
    add("- `LookaheadError`: the strategy asked for data that was not yet knowable (today or later).\n"
        "- `OrderError`: an order's arguments are invalid (the message says which).\n"
        "- `StrategyError`: the file breaks the strategy contract.\n"
        "- `ConfigError`: a run setting or parameter value is invalid.\n")

    add("## Rules for strategy files\n")
    add(f"- Imports: only {', '.join(f'`{m}`' for m in ALLOWED_IMPORTS)}, and "
        "`from backtester import ...` (for example `from backtester import Param, Strategy`). "
        "`numpy.random` is not allowed.\n"
        f"- Banned built-ins: {', '.join(f'`{n}()`' for n in sorted(BANNED_CALLS))}.\n"
        "- No file access: no `pandas.read_*`, no "
        f"{', '.join(f'`numpy.{n}`' for n in sorted(NUMPY_FILE_FUNCS))}, and no "
        f"{', '.join(f'`.{n}()`' for n in sorted(FILE_METHODS))}.\n"
        f"- No wall clock: no {', '.join(f'`.{n}()`' for n in sorted(CLOCK_METHODS))}. Use `ctx.today`.\n"
        "- No randomness except `ctx.rng`.\n"
        "- No private attributes: nothing that starts with `_`, except your own `self._name` "
        "attributes and `super().__init__`. Dunder attributes (`__class__`, `__dict__`, ...) are "
        "not allowed.\n"
        "- Read the market and the account only through `ctx`. Keep state in `ctx.state` or on `self`.\n")

    add("## Validation\n")
    add("Every strategy file goes through these stages in order. Validation stops at the first "
        "stage that fails.\n")
    add("| Stage | Checks |\n| --- | --- |")
    add("\n".join(f"| {name} | {desc} |" for name, desc in STAGES) + "\n")
    add("A failing stage produces a report like this:\n")
    add(_fence("""\
Validation FAILED: my_strategy.py
Stage 1 of 5 (static) failed with 1 issue.

  static        failed
  declarations  not run
  tests         not run
  dry run       not run
  determinism   not run

[1] my_strategy.py:2  import-not-allowed
    Code:  import requests
    Error: module `requests` is not on the import allowlist
    Fix:   Remove it. Strategy files may import only numpy, pandas, ...""", "text") + "\n")
    add("Validate from the command line with `backtest validate strategies/my_strategy.py`, or on "
        "the Strategies page of the app.\n")

    add("## Fixture testing\n")
    add("Put tests in `test_<strategy file name>.py` next to the strategy. They run with pytest "
        "during validation. A fixture is a tiny synthetic market, so a test can check the logic "
        "exactly (\"a breakout on session 30 buys at session 31's open\").\n")
    add(f"### `{signature(make_fixture)}`\n")
    add(_doc(make_fixture) + "\n")
    add(f"### `{signature(run_on_fixture)}`\n")
    add(_doc(run_on_fixture) + "\n")
    add("### `FixtureResult`\n")
    add(_doc(FixtureResult) + "\n")
    add("Example tests for the template:\n")
    add(_fence(TEMPLATE_TEST) + "\n")

    add("## Reference strategies\n")
    for name in REFERENCE_STRATEGIES:
        path = root / "strategies" / name
        if path.is_file():
            add(f"### `strategies/{name}`\n")
            add(_fence(path.read_text(encoding="utf-8")) + "\n")

    return "\n".join(out).rstrip() + "\n"


def kit_files(root: Path = ROOT) -> dict[str, str]:
    """The kit's files, by file name."""
    return {
        "STRATEGY_API.md": build_api_doc(root),
        "my_strategy.py": TEMPLATE_STRATEGY,
        "test_my_strategy.py": TEMPLATE_TEST,
    }


def write_kit(out_dir: str | Path = ROOT / "kit", root: Path = ROOT) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in kit_files(root).items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written

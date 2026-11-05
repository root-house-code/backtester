"""Command-line runner.

    backtest run strategies/momentum_rotation.py --start 2015-01-01 --end 2024-12-31 \\
        [--tickers SPY,QQQ] [--capital 100000] [--param lookback=126] ...
    backtest validate strategies/my_strategy.py [--quick]
    backtest feeds [check|refresh] [names...]
    backtest kit [--out kit]
    backtest ui
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

from .errors import BacktesterError


def _parse_params(items: list[str]) -> dict:
    params = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--param expects name=value, got {item!r}")
        k, v = item.split("=", 1)
        params[k.strip()] = v.strip()
    return params


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest", description="Deterministic daily-bar backtester")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run a strategy file and write a run folder")
    r.add_argument("strategy", type=Path, help="path to a strategy .py file")
    r.add_argument("--start", required=True, help="first session, YYYY-MM-DD")
    r.add_argument("--end", required=True, help="last session, YYYY-MM-DD")
    r.add_argument("--tickers", help="comma-separated universe (overrides the strategy's)")
    r.add_argument("--capital", type=float, dest="initial_capital", help="initial capital (USD)")
    r.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="strategy parameter override (repeatable)")
    r.add_argument("--benchmark", help="benchmark ticker (default SPY)")
    r.add_argument("--commission-per-share", type=float)
    r.add_argument("--commission-min", type=float)
    r.add_argument("--slippage-bps", type=float)
    r.add_argument("--max-adv-pct", type=float, help="cap orders at this percent of 20-day avg dollar volume")
    r.add_argument("--cash-shortfall", choices=["resize", "reject"])
    r.add_argument("--fractional", action="store_true", dest="fractional_shares", default=None,
                   help="allow fractional shares")
    r.add_argument("--seed", type=int, help="seed for ctx.rng")
    r.add_argument("--in-sample-end", help="score in-sample (through this date) and out-of-sample separately")
    r.add_argument("--walk-forward", metavar="WINDOW,STEP", help="rolling walk-forward folds, e.g. 2y,6m")
    r.add_argument("--runs-dir", type=Path, default=Path("runs"))
    r.add_argument("--cache-dir", type=Path, default=Path("data") / "cache")
    r.add_argument("--no-refresh", action="store_true", help="use cached prices only")

    v = sub.add_parser("validate", help="validate strategy files and print a report for each")
    v.add_argument("strategies", type=Path, nargs="+", help="strategy .py files")
    v.add_argument("--cache-dir", type=Path, default=Path("data") / "cache",
                   help="price cache for the dry run (missing tickers get synthetic prices)")
    v.add_argument("--quick", action="store_true", help="static and declaration checks only")

    f = sub.add_parser("feeds", help="list feeds and check their data")
    f.add_argument("action", nargs="?", choices=["check", "refresh"], default="check",
                   help="check (default): validate every feed; refresh: fetch a plug-in feed again")
    f.add_argument("names", nargs="*", help="feed names (default: all)")
    f.add_argument("--feeds-dir", type=Path, default=Path("data") / "feeds")
    f.add_argument("--plugins-dir", type=Path, default=Path("feeds"))
    f.add_argument("--cache-dir", type=Path, default=Path("data") / "cache")

    k = sub.add_parser("kit", help="write the strategy authoring kit (STRATEGY_API.md and a template)")
    k.add_argument("--out", type=Path, default=Path("kit"), help="output folder (default: kit)")

    sub.add_parser("ui", help="open the Streamlit UI in a browser")
    return p


def cmd_validate(args) -> int:
    from .validation import validate

    failed = 0
    for k, path in enumerate(args.strategies):
        report = validate(path, cache_dir=args.cache_dir, quick=args.quick)
        if k:
            print()
        print(report.text(), end="")
        failed += report.failed_stage is not None
    return 1 if failed else 0


def cmd_feeds(args) -> int:
    from .feeds import FeedRegistry

    registry = FeedRegistry(args.feeds_dir, args.plugins_dir, args.cache_dir, refresh=args.action == "refresh")
    names = args.names or registry.names()
    failed = 0
    for k, name in enumerate(names):
        if k:
            print()
        feed = registry.get(name)
        print(f"{name}  [{feed.kind}, {registry.origin(name)}]  {feed.description}".rstrip())
        if args.action == "refresh":
            data = registry.load(name, check=False)
            validation = data.validation
        else:
            validation = registry.validate(name)
        print(validation.text(), end="")
        failed += not validation.ok
    return 1 if failed else 0


def cmd_kit(args) -> int:
    from .kit import write_kit

    for path in write_kit(args.out):
        print(f"wrote {path}")
    return 0


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.2f}%"


def _num(x, fmt="{:.2f}"):
    return "n/a" if x is None else fmt.format(x)


def summary(metrics: dict) -> str:
    r, k, t, d = metrics["returns"], metrics["risk"], metrics["trades"], metrics["data_quality"]
    p = metrics["period"]
    lines = [
        f"Period        {p['start']} -> {p['end']} ({p['sessions']} sessions)",
        f"Total return  {_pct(r['total_return'])}   (benchmark {_pct(r['benchmark_total_return'])})",
        f"CAGR          {_pct(r['cagr'])}   (benchmark {_pct(r['benchmark_cagr'])})",
        f"Volatility    {_pct(k['volatility'])}",
        f"Sharpe        {_num(k['sharpe'])}   Sortino {_num(k['sortino'])}   Calmar {_num(k['calmar'])}",
        f"Max drawdown  {_pct(k['max_drawdown'])}   ({k['max_drawdown_duration_sessions']} sessions)",
        f"Alpha / beta  {_pct(r['alpha'])} / {_num(r['beta'])}",
        f"Trades        {t['count']}   win rate {_pct(t['win_rate'])}   "
        f"profit factor {_num(t['profit_factor'])}   avg held {_num(t['avg_sessions_held'], '{:.1f}')} sessions",
        f"Exposure      {_pct(metrics['exposure']['avg_pct_invested'])} avg invested   "
        f"turnover {_num(metrics['exposure']['turnover'])}x/yr",
        f"Skipped       {d['signals_skipped_no_data']} of {d['signals_total']} signals for missing data "
        f"({_pct(d['skip_rate'])})",
    ]
    ci = metrics.get("confidence", {}).get("mean_trade_return", {})
    if ci.get("ci_low") is not None:
        lines.append(f"Mean trade    {_pct(ci['mean'])}   95% CI {_pct(ci['ci_low'])} to {_pct(ci['ci_high'])} "
                     f"({ci['n']} trades, bootstrap)")
    split = metrics.get("split")
    if split:
        lines.append("")
        for label, key in (("In-sample", "in_sample"), ("Out-of-sample", "out_of_sample")):
            m = split[key]
            lines.append(f"{label:<14}{m['period']['start']} -> {m['period']['end']}   "
                         f"CAGR {_pct(m['returns']['cagr'])}   Sharpe {_num(m['risk']['sharpe'])}   "
                         f"max DD {_pct(m['risk']['max_drawdown'])}   trades {m['trades']['count']}")
    wf = metrics.get("walk_forward")
    if wf and wf["folds"]:
        s = wf["summary"]
        lines.append("")
        lines.append(f"Walk-forward  {s['folds']} folds ({wf['window']} window, {wf['step']} step)   "
                     f"OOS total {_pct(s['oos_total_return'])}   OOS CAGR {_pct(s['oos_cagr'])}   "
                     f"positive {_pct(s['oos_positive_share'])} of folds")
        lines.append(f"              mean Sharpe in-sample {_num(s['mean_is_sharpe'])}, "
                     f"out-of-sample {_num(s['mean_oos_sharpe'])}")
    return "\n".join(lines)


def cmd_run(args) -> int:
    from .data.yfinance_provider import YFinanceProvider
    from .run import run_backtest, write_run_folder

    overrides = {
        "start": args.start,
        "end": args.end,
        "universe": [t for t in args.tickers.split(",") if t.strip()] if args.tickers else None,
        "params": _parse_params(args.param),
        "initial_capital": args.initial_capital,
        "benchmark": args.benchmark,
        "commission_per_share": args.commission_per_share,
        "commission_min": args.commission_min,
        "slippage_bps": args.slippage_bps,
        "max_adv_pct": args.max_adv_pct,
        "cash_shortfall": args.cash_shortfall,
        "fractional_shares": args.fractional_shares,
        "seed": args.seed,
        "in_sample_end": args.in_sample_end,
        "walk_forward": args.walk_forward,
    }
    provider = YFinanceProvider(cache_dir=args.cache_dir, refresh=not args.no_refresh)
    result = run_backtest(args.strategy, overrides, provider=provider)
    folder = write_run_folder(result, args.runs_dir)
    print(f"{result.config.strategy['name']}  ({args.strategy})")
    print(summary(result.metrics))
    print(f"\nRun folder: {folder}")
    return 0


def cmd_ui() -> int:
    # The UI lives outside the engine package; this only launches it.
    import subprocess

    app = Path(__file__).resolve().parents[1] / "app" / "main.py"
    if not app.is_file():
        print(f"error: UI not found at {app}", file=sys.stderr)
        return 2
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)], cwd=app.parents[1])


def main(argv: list[str] | None = None) -> int:
    # yfinance triggers pandas deprecation warnings internally; they are not actionable here.
    warnings.filterwarnings("ignore", module="yfinance")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "kit":
            return cmd_kit(args)
        if args.command == "feeds":
            return cmd_feeds(args)
        if args.command == "ui":
            return cmd_ui()
    except BacktesterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())

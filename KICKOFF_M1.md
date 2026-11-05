# Kickoff: Backtester M1 — Engine core on prices

You're building milestone M1 of a Python backtesting engine. The full design is in `SPEC.md` in this repo. Read it end to end before writing code. This prompt scopes what to build now; the spec is the source of truth for behavior.

## Goal

A pure-Python library plus a CLI that runs price-only strategies on daily bars and writes a complete run folder. No UI yet (that's M2). No LLM calls anywhere in the code.

## In scope

- **Package layout:** `backtester/` library, `strategies/` for strategy files, `runs/` for outputs, `data/cache/` for Parquet, `tests/`. Python 3.11+, `pyproject.toml`, pytest.
- **Price data:** a `PriceProvider` interface with a yfinance implementation. Cache adjusted and raw daily OHLCV as Parquet per ticker; fetch once, then refresh incrementally. Use `exchange_calendars` (NYSE) for sessions.
- **Security identity:** key everything internally on a `security_id` (equal to the ticker symbol in V1). Don't use raw ticker strings as keys deep in the engine.
- **Strategy API:** `Strategy` base class and `Param` exactly as in the spec's "Strategy API contract", minus feed-related calls. Implement: `ctx.today`, `ctx.universe`, `ctx.history`, `ctx.price`, `ctx.is_first_session_of`, `ctx.position(s)`, `ctx.cash`, `ctx.equity`, `ctx.order`, `ctx.target_weights`, `ctx.close`, `ctx.reject`, `ctx.log`, `ctx.state`, `ctx.rng`, plus `on_start` / `on_fill` / `on_end` hooks.
- **Point-in-time enforcement:** `ctx` must make lookahead impossible. On session t, the strategy can see bars only through the close of t−1. Any attempt to reach later data raises an error.
- **Daily cycle and fills:** exactly as in "Execution and accounting": decide before the open; fill `at="open"` orders at t's open; check stops/targets against t's high/low (gap-through fills at the open; both touched → assume stop); fill `at="close"` orders at t's close; mark to t's close.
- **Exits:** `exit_after` counts trading sessions. Stops and targets supported.
- **Costs:** commission and slippage settings exist in the config but default to 0. Optional liquidity cap, off by default. Whole shares; cash shortfalls resize (default) or reject, and are logged.
- **Ledger:** signed quantities (shorts come in V2), positions in adjusted-share terms, a full fill log, daily equity snapshots. A ticker that stops trading mid-hold closes at its last close and the trade is flagged.
- **Run config:** resolved with precedence CLI override → strategy `Param` default → engine default, recording the source of each value. Defaults per the spec's config table.
- **Analytics:** the metrics in "Run outputs and metrics", except in/out-of-sample split, walk-forward and bootstrap intervals (those are M5).
- **Run folder:** write `config.json`, `strategy.py` (+ hash), `equity.parquet`, `trades.parquet`, `signals.parquet`, `orders.parquet`, `metrics.json`, `data_manifest.json`, `log.txt` under `runs/<timestamp>-<strategy>/`.
- **CLI:** `backtest run strategies/<file>.py --start 2015-01-01 --end 2024-12-31 [--tickers SPY,QQQ] [--capital 100000] [--param lookback=126]`, printing a short metrics summary and the run folder path.
- **Reference strategies:** `strategies/buy_and_hold.py` and `strategies/momentum_rotation.py` (the momentum example from the spec).

## Out of scope for M1

Streamlit UI, the feed framework (`ctx.events`, `ctx.ref`, `Feed`, `from_feed` universes), shares outstanding / market cap, the strategy validation pipeline and authoring kit, short selling, walk-forward and in/out-of-sample splits.

## Build order: tests first

Write these tests before the engine code they cover, using small synthetic price fixtures (no network) wherever possible:

1. **Lookahead canary:** a strategy that tries to read bar t (or later) during session t raises an error.
2. **Fill timing:** an order placed in `on_bar` on session t with `at="open"` fills at t's open; `at="close"` fills at t's close.
3. **Stops/targets:** gap-through fills at the open; stop and target touched the same day resolves to the stop.
4. **`exit_after`:** a 3-session hold exits on the correct session, skipping weekends and holidays.
5. **Accounting invariants:** cash + position value = equity every session; total trade P&L = equity change − costs.
6. **Determinism:** the same config and data produce an identical metrics hash.
7. **Cost sanity:** a zero-cost run never underperforms the same run with costs.
8. **Known answer (real data, marked as a slower test):** buy-and-hold SPY with zero costs matches SPY's adjusted return over the period to within 0.01%.

Then build the engine until they all pass, and add golden-run snapshots for both reference strategies.

## Done when

- All tests above pass.
- `backtest run strategies/momentum_rotation.py --start 2015-01-01 --end 2024-12-31` produces a complete run folder and a sensible metrics summary.
- Running the same command twice produces byte-identical `metrics.json`.

## Working style

- Keep the engine free of UI code; the UI in M2 will only read run folders.
- Prefer small, typed modules (`data/`, `strategy.py`, `context.py`, `broker.py`, `ledger.py`, `analytics.py`, `run.py`, `cli.py`).
- Write docstrings on every public `Strategy` and `ctx` member. M3 generates `STRATEGY_API.md` from them, so they're part of the product.
- When the spec is ambiguous, pick the more conservative option, note it in `DECISIONS.md`, and keep going.

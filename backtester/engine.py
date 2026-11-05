"""The daily cycle.

For each session t:
  1. Advance the clock: bars through the close of t-1 become visible.
     Positions in securities that stopped trading are closed at their last close.
  2. Queue scheduled exits (``exit_after`` reached) as open orders.
  3. Call ``on_bar(ctx)`` (and ``on_start`` before the first one).
  4. Fill open orders at t's open; check stops/targets against t's high/low;
     fill close orders at t's close.
  5. Mark the portfolio to t's close and record an equity snapshot.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from . import orders as O
from .broker import Broker
from .config import ResolvedConfig
from .context import Context
from .data.panel import PointInTimeView, PricePanel
from .ids import SecurityMaster
from .ledger import Ledger, Lot

EXIT_REASON_BY_KIND = {O.ORDER: "sell", O.TARGET: "rebalance", O.CLOSE: "close"}


class Engine:
    def __init__(self, strategy_cls, cfg: ResolvedConfig, panel: PricePanel,
                 master: SecurityMaster, start_idx: int, end_idx: int,
                 universe: list[str] | None = None, feeds: dict | None = None,
                 universe_feed: str | None = None, ref_fields: dict | None = None):
        self.cfg = cfg
        self.panel = panel
        self.master = master
        self.start_idx = start_idx
        self.end_idx = end_idx
        self.static_universe = list(universe if universe is not None else cfg["universe"])
        self.feeds = dict(feeds or {})          # name -> FeedView
        self.universe_feed = universe_feed      # from_feed universes: the feed's name
        self.ref_fields = dict(ref_fields or {})  # field -> (feed name, column)
        self.current_events: dict[str, list] = {}
        self.view = PointInTimeView(panel)
        self.view.cursor = start_idx
        self.ledger = Ledger(cfg["initial_capital"])
        self.broker = Broker(cfg, panel, self.ledger, self)
        self.ctx = Context(self, cfg["seed"])
        self.strategy = strategy_cls(cfg.params)
        self.orders: list[O.Order] = []
        self.queue: list[O.Order] = []
        self.signals: list[dict] = []
        self.fills: list[dict] = []
        self.equity_rows: list[dict] = []
        self.log_lines: list[str] = []
        self.phase = "setup"
        self.i = start_idx
        self._next_order = 1

    @property
    def universe_tickers(self) -> list[str]:
        """Tickers in scope now: the static universe, or for a from_feed universe
        every ticker the feed has mentioned in records visible this session."""
        if self.universe_feed is None:
            return self.static_universe
        return self.feeds[self.universe_feed].tickers_visible(self.i)

    def _deliver_events(self, i: int) -> None:
        """Make this session's new event records available and log each one as a signal."""
        self.current_events = {}
        for name, view in self.feeds.items():
            if view.kind != "event":
                continue
            events = view.events_at(i)
            self.current_events[name] = events
            for e in events:
                sid = self.master.to_id(e.ticker)
                self.signals.append({
                    "date": self.view.today, "security_id": str(sid), "ticker": e.ticker,
                    "decision": "event", "reason": None, "tag": None, "order_id": None,
                    "feed": name, "event_id": e.id, "available_at": e.available_at,
                })

    # --- main loop -----------------------------------------------------
    def run(self) -> None:
        for i in range(self.start_idx, self.end_idx + 1):
            self.i = i
            self.view.cursor = i
            self.phase = "engine"
            self.broker.close_stopped_trading(i)
            self._queue_scheduled_exits(i)
            self._deliver_events(i)

            self.phase = "decide"
            if i == self.start_idx:
                self.strategy.on_start(self.ctx)
            self.strategy.on_bar(self.ctx)

            self.phase = "fill"
            self.broker.execute_batch(self._take(i, "open"), "open", i)
            self.broker.check_stops_targets(i)
            self.broker.execute_batch(self._take(i, "close"), "close", i)
            self._mark(i)

        self.phase = "end"
        self.strategy.on_end(self.ctx)
        for order in self.queue:
            order.status = O.CANCELLED
            order.message = "run ended before the order could fill"
        self.queue = []

    def _take(self, i: int, at: str) -> list[O.Order]:
        batch = [o for o in self.queue if o.fill_idx == i and o.at == at]
        self.queue = [o for o in self.queue if not (o.fill_idx == i and o.at == at)]
        return batch

    def _queue_scheduled_exits(self, i: int) -> None:
        for lot in sorted(self.ledger.lots(), key=lambda l: l.lot_id):
            due = lot.exit_due_idx
            if due is not None and due <= i:
                order = self.engine_order(lot, kind=O.EXIT_AFTER, at="open", i=i,
                                          exit_reason=O.EXIT_AFTER)
                self.queue.append(order)

    def _mark(self, i: int) -> None:
        arr = self.panel.arrays
        positions_value = 0.0
        for sid in self.ledger.held():
            positions_value += self.ledger.qty(sid) * self._close_or_last(sid, i)
        for lot in self.ledger.lots():
            c = self.panel.col(lot.security_id)
            o, h, l, cl = (arr[f][i, c] for f in ("open", "high", "low", "close"))
            if lot.entry_idx == i and lot.entry_at == "close":
                lot.touch(cl, cl)
            else:
                lot.touch(l, h)
        for lot in self.ledger.closed_lots:
            if lot.exit_idx == i:
                self._touch_exit_day(lot, i)
        equity = self.ledger.cash + positions_value
        self.equity_rows.append({
            "date": self.panel.sessions[i],
            "equity": equity,
            "cash": self.ledger.cash,
            "positions_value": positions_value,
            "exposure": positions_value / equity if equity else float("nan"),
        })

    def _touch_exit_day(self, lot: Lot, i: int) -> None:
        c = self.panel.col(lot.security_id)
        o, h, l = (self.panel.arrays[f][i, c] for f in ("open", "high", "low"))
        exit_px = lot.sold_value / lot.sold_qty if lot.sold_qty else float("nan")
        entered_today = lot.entry_idx == i
        if lot.exit_at == "open":
            lot.touch(exit_px, exit_px)
        elif lot.exit_at == "intraday":
            start = exit_px if math.isnan(o) else o
            lot.touch(min(start, exit_px), max(start, exit_px))
        elif lot.exit_at == "close":
            if entered_today and lot.entry_at == "close":
                lot.touch(exit_px, exit_px)
            else:
                lot.touch(l, h)

    def _close_or_last(self, sid, i) -> float:
        p = self.panel.value("close", i, sid)
        if np.isnan(p):
            p = self.panel.last_close_at_or_before(i, sid)
        return p

    # --- called by ctx and broker --------------------------------------
    def queue_order(self, ticker: str, *, kind: str, at: str, **fields) -> int:
        sid = self.master.to_id(ticker)
        ticker = self.master.to_ticker(sid)
        fill_idx = self.i if self.phase == "decide" else self.i + 1
        order = O.Order(
            order_id=self._next_order, created=self.view.today, fill_idx=fill_idx,
            security_id=sid, ticker=ticker, at=at, kind=kind, source="strategy",
            exit_reason=EXIT_REASON_BY_KIND.get(kind), **fields,
        )
        self._next_order += 1
        self.orders.append(order)
        self.signals.append({
            "date": self.view.today, "security_id": str(sid), "ticker": ticker,
            "decision": "order", "reason": kind, "tag": order.tag, "order_id": order.order_id,
        })
        if self.phase == "end":
            order.status = O.CANCELLED
            order.message = "placed in on_end; no sessions left to fill"
        else:
            self.queue.append(order)
            if fill_idx != self.i:
                self.log("info", f"order {order.order_id} placed outside on_bar; fills next session")
        return order.order_id

    def engine_order(self, lot: Lot, *, kind: str, at: str, i: int, exit_reason: str) -> O.Order:
        order = O.Order(
            order_id=self._next_order, created=self.panel.sessions[i], fill_idx=i,
            security_id=lot.security_id, ticker=lot.ticker, at=at, kind=kind,
            source="engine", tag=lot.tag, lot_id=lot.lot_id, exit_reason=exit_reason,
        )
        self._next_order += 1
        self.orders.append(order)
        return order

    def record_reject(self, ticker: str, reason: str, tag: str | None) -> None:
        sid = self.master.to_id(ticker)
        self.signals.append({
            "date": self.view.today, "security_id": str(sid), "ticker": self.master.to_ticker(sid),
            "decision": "reject", "reason": reason, "tag": tag, "order_id": None,
        })

    def record_fill(self, fill: O.Fill, order: O.Order) -> None:
        self.fills.append({
            "date": fill.date, "security_id": str(order.security_id), "ticker": fill.ticker,
            "qty": fill.qty, "price": fill.price, "commission": fill.commission,
            "order_id": fill.order_id, "at": fill.at, "reason": fill.reason, "tag": fill.tag,
        })
        prev = self.phase
        self.phase = "fill" if prev == "decide" else prev
        try:
            self.strategy.on_fill(self.ctx, fill)
        finally:
            self.phase = prev

    def log(self, level: str, msg: str, source: str = "engine", **fields) -> None:
        date = self.view.today.date().isoformat()
        extra = f" {json.dumps(fields, sort_keys=True, default=str)}" if fields else ""
        self.log_lines.append(f"{date} {level.upper():<7} {source}: {msg}{extra}")

    # --- outputs -------------------------------------------------------
    def trades_frame(self) -> pd.DataFrame:
        last = self.end_idx
        rows = []
        for lot in sorted(self.ledger.closed_lots + self.ledger.lots(), key=lambda l: l.lot_id):
            is_open = lot.is_open
            mark_value = lot.qty * self._close_or_last(lot.security_id, last) if is_open else 0.0
            exit_qty = lot.sold_qty + (lot.qty if is_open else 0.0)
            exit_value = lot.sold_value + mark_value
            pnl = exit_value - lot.bought_value
            exit_idx = last if is_open else lot.exit_idx
            rows.append({
                "trade_id": lot.lot_id,
                "security_id": str(lot.security_id),
                "ticker": lot.ticker,
                "entry_date": lot.entry_date,
                "entry_price": lot.avg_entry,
                "exit_date": pd.NaT if is_open else lot.exit_date,
                "exit_price": exit_value / exit_qty if exit_qty else float("nan"),
                "shares": lot.bought_qty,
                "open_shares": lot.qty,
                "pnl": pnl,
                "commission": lot.commission,
                "pnl_net": pnl - lot.commission,
                "return": pnl / lot.bought_value if lot.bought_value else float("nan"),
                "sessions_held": exit_idx - lot.entry_idx,
                "mae": lot.mae,
                "mfe": lot.mfe,
                "exit_reason": "open" if is_open else lot.exit_reason,
                "stop": lot.stop,
                "target": lot.target,
                "tag": lot.tag,
                "is_open": is_open,
                "flags": ",".join(lot.flags),
            })
        cols = ["trade_id", "security_id", "ticker", "entry_date", "entry_price", "exit_date",
                "exit_price", "shares", "open_shares", "pnl", "commission", "pnl_net", "return",
                "sessions_held", "mae", "mfe", "exit_reason", "stop", "target", "tag", "is_open", "flags"]
        df = pd.DataFrame(rows, columns=cols)
        df["entry_date"] = pd.to_datetime(df["entry_date"])
        df["exit_date"] = pd.to_datetime(df["exit_date"])
        df["is_open"] = df["is_open"].astype(bool)
        return df

    def orders_frame(self) -> pd.DataFrame:
        cols = list(O.Order(0, pd.Timestamp(0), 0, "", "", "", "", "").record())
        return pd.DataFrame([o.record() for o in self.orders], columns=cols)

    def fills_frame(self) -> pd.DataFrame:
        cols = ["date", "security_id", "ticker", "qty", "price", "commission", "order_id", "at", "reason", "tag"]
        return pd.DataFrame(self.fills, columns=cols)

    def signals_frame(self) -> pd.DataFrame:
        """Every candidate: feed events, orders and rejects.

        An event's outcome comes from what the strategy did with it, linked by
        the event id used as an order ``tag`` or in ``ctx.reject``: the order's
        status, "rejected" (with the reason), or "no action".
        """
        status = {o.order_id: (o.status, o.message) for o in self.orders}
        by_tag: dict[str, tuple] = {}
        for s in self.signals:
            tag = s.get("tag")
            if tag is None or tag in by_tag or s["decision"] == "event":
                continue
            if s["decision"] == "order":
                st, msg = status.get(s["order_id"], (None, None))
                by_tag[tag] = (st, msg, s["order_id"])
            elif s["decision"] == "reject":
                by_tag[tag] = ("rejected", s["reason"], None)
        rows = []
        for k, s in enumerate(self.signals, start=1):
            row = {"signal_id": k, "feed": None, "event_id": None, "available_at": None, **s}
            if s["decision"] == "event":
                st, msg, oid = by_tag.get(s["event_id"], ("no action", None, None))
                row.update(outcome=st, outcome_reason=msg or None, order_id=oid)
            else:
                st, msg = status.get(s["order_id"], (None, None))
                if s["decision"] == "reject":
                    st, msg = "rejected", s["reason"]
                row.update(outcome=st, outcome_reason=msg or None)
            rows.append(row)
        cols = ["signal_id", "date", "security_id", "ticker", "decision", "reason", "tag",
                "order_id", "outcome", "outcome_reason", "feed", "event_id", "available_at"]
        df = pd.DataFrame(rows, columns=cols)
        df["order_id"] = df["order_id"].astype("Int64")
        df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
        return df

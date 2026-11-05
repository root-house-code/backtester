"""Broker simulator: turns queued orders into fills at session prices.

Fill rules (see SPEC "Execution and accounting"):
- ``at="open"`` fills at the session open, ``at="close"`` at the close, both
  adjusted for slippage.
- Stops/targets are checked against the session's high and low after open
  fills. A gap through the level fills at the open. If both are touched the
  same day, the stop is assumed. Slippage applies to these fills too.
- Within a batch: explicit sells and engine exits first, then target-weight
  sells, then all buys, so sale proceeds fund purchases.
- ``pct``, ``value`` and ``target_weights`` orders are notional: the share
  count is computed at fill time from the fill price and the equity at that
  batch's prices. ``shares`` orders are fixed.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from . import orders as O
from .ledger import EPS, Ledger

if TYPE_CHECKING:
    from .config import ResolvedConfig
    from .data.panel import PricePanel

ADV_SESSIONS = 20
CASH_TOLERANCE = 1e-6


class CostModel:
    def __init__(self, cfg: ResolvedConfig):
        self.per_share = cfg["commission_per_share"]
        self.minimum = cfg["commission_min"]
        self.slippage = cfg["slippage_bps"] / 10_000.0
        self.fractional = cfg["fractional_shares"]

    def slip(self, price: float, side: int) -> float:
        """Fill price after slippage: worse for the trader on either side."""
        return price * (1 + self.slippage * side)

    def commission(self, qty: float) -> float:
        qty = abs(qty)
        if qty <= EPS or (self.per_share == 0 and self.minimum == 0):
            return 0.0
        return max(self.per_share * qty, self.minimum)

    def round_down(self, qty: float) -> float:
        """Round a share count toward zero (whole shares unless fractional)."""
        if self.fractional:
            return qty
        # Guard against float noise like 494.99999999 -> 494.
        return float(math.floor(qty + 1e-9))

    def max_affordable(self, cash: float, price: float) -> float:
        """Largest share count whose cost plus commission fits in cash."""
        if cash <= 0:
            return 0.0
        q = cash / (price + self.per_share)
        if self.minimum > 0:
            q = min(q, max(0.0, (cash - self.minimum) / price))
        return self.round_down(q)


class Broker:
    def __init__(self, cfg: ResolvedConfig, panel: PricePanel, ledger: Ledger, engine):
        self.cfg = cfg
        self.panel = panel
        self.ledger = ledger
        self.engine = engine
        self.costs = CostModel(cfg)
        self.max_adv_pct = cfg["max_adv_pct"]
        self.cash_shortfall = cfg["cash_shortfall"]

    # --- helpers -------------------------------------------------------
    def _mark_price(self, sid, field, i) -> float:
        p = self.panel.value(field, i, sid)
        if np.isnan(p):
            p = self.panel.last_close_at_or_before(i - 1, sid)
        return p

    def equity_at(self, field: str, i: int) -> float:
        value = self.ledger.cash
        for sid in self.ledger.held():
            value += self.ledger.qty(sid) * self._mark_price(sid, field, i)
        return value

    def _adv(self, sid, i) -> float | None:
        if i < ADV_SESSIONS or not self.panel.has(sid):
            return None
        window = self.panel.arrays["dollar_volume"][i - ADV_SESSIONS:i, self.panel.col(sid)]
        if np.isnan(window).any():
            return None
        return float(window.mean())

    def _finish(self, order, status, message=""):
        order.status = status
        if message:
            order.message = f"{order.message}; {message}" if order.message else message
        level = "info" if status in (O.FILLED, O.NO_CHANGE) else "warning"
        if status != O.FILLED and status != O.NO_CHANGE:
            self.engine.log(level, f"order {order.order_id} {order.ticker} {status}: {order.message}")

    # --- batches -------------------------------------------------------
    def execute_batch(self, batch: list[O.Order], at: str, i: int) -> None:
        if not batch:
            return
        equity = self.equity_at(at, i)
        sells, targets, buys = [], [], []
        for order in batch:
            price = self.panel.value(at, i, order.security_id)
            if np.isnan(price):
                self._finish(order, O.SKIPPED, "no price data")
                continue
            if order.kind == O.TARGET:
                targets.append((order, price))
                continue
            desired = self._desired_qty(order, price, equity)
            if desired is None:
                continue
            if desired < -EPS:
                sells.append((order, price, desired))
            elif desired > EPS:
                buys.append((order, price, desired))
            else:
                self._finish(order, O.NO_CHANGE, "zero shares")

        for order, price, desired in sells:
            self._sell(order, price, -desired, at, i)

        for order, price in targets:
            desired = self._target_delta(order, price, equity)
            if desired < -EPS:
                self._sell(order, price, -desired, at, i)
            elif desired > EPS:
                buys.append((order, price, desired))
            else:
                self._finish(order, O.NO_CHANGE, "already at target")

        for order, price, desired in sorted(buys, key=lambda b: b[0].order_id):
            self._buy(order, price, desired, at, i)

    def _desired_qty(self, order: O.Order, price: float, equity: float) -> float | None:
        if order.kind in (O.CLOSE, O.EXIT_AFTER):
            if order.lot_id is not None:
                lot = self.ledger.lot(order.lot_id)
                qty = lot.qty if lot else 0.0
            else:
                qty = self.ledger.qty(order.security_id)
            if qty <= EPS:
                self._finish(order, O.CANCELLED, "no position to close")
                return None
            return -qty
        if order.shares is not None:
            return order.shares
        notional = order.pct * equity if order.pct is not None else order.value
        side = 1 if notional > 0 else -1
        qty = notional / self.costs.slip(price, side)
        return math.copysign(self.costs.round_down(abs(qty)), qty)

    def _target_delta(self, order: O.Order, price: float, equity: float) -> float:
        current = self.ledger.qty(order.security_id)
        raw_target = order.weight * equity / price
        side = 1 if raw_target > current else -1
        target = self.costs.round_down(order.weight * equity / self.costs.slip(price, side))
        delta = target - current
        if side * delta < 0:  # slippage flipped the direction: nothing to do
            return 0.0
        return delta

    def _sell(self, order: O.Order, price: float, qty: float, at: str, i: int) -> None:
        sid = order.security_id
        if order.lot_id is not None:
            lot = self.ledger.lot(order.lot_id)
            held = lot.qty if lot else 0.0
        else:
            held = self.ledger.qty(sid)
        order.requested_qty = -qty
        if held <= EPS:
            self._finish(order, O.REJECTED, "no position to sell (long-only)")
            return
        status = O.FILLED
        if qty > held + EPS:
            qty, status = held, O.RESIZED
            order.message = "clipped to position size (long-only)"
        elif abs(qty - held) > EPS:
            qty = self.costs.round_down(qty)
        if qty <= EPS:
            self._finish(order, O.REJECTED, "rounds to zero shares")
            return
        self._fill_sell(order, self.costs.slip(price, -1), qty, at, i, status)

    def _fill_sell(self, order, fill_price, qty, at, i, status=O.FILLED) -> None:
        commission = self.costs.commission(qty)
        reason = order.exit_reason or "sell"
        self.ledger.sell(order.security_id, qty, fill_price, commission, idx=i,
                         date=self.panel.sessions[i], at=at, reason=reason, lot_id=order.lot_id)
        self._record(order, -qty, fill_price, commission, at, i, reason, status)

    def _buy(self, order: O.Order, price: float, qty: float, at: str, i: int) -> None:
        order.requested_qty = qty
        fill_price = self.costs.slip(price, 1)
        status = O.FILLED
        if self.max_adv_pct is not None:
            adv = self._adv(order.security_id, i)
            if adv is None:
                self._finish(order, O.REJECTED, f"no {ADV_SESSIONS}-session volume history for liquidity cap")
                return
            cap = self.costs.round_down(adv * self.max_adv_pct / 100.0 / fill_price)
            if qty > cap + EPS:
                qty, status = cap, O.RESIZED
                order.message = f"resized to {self.max_adv_pct:g}% of 20-day avg dollar volume"
        cost = qty * fill_price + self.costs.commission(qty)
        if cost > self.ledger.cash + CASH_TOLERANCE:
            if self.cash_shortfall == "reject":
                self._finish(order, O.REJECTED, "insufficient cash")
                return
            qty, status = min(qty, self.costs.max_affordable(self.ledger.cash, fill_price)), O.RESIZED
            order.message = (order.message + "; " if order.message else "") + "resized to available cash"
        if qty <= EPS:
            self._finish(order, O.REJECTED, "insufficient cash" if status == O.RESIZED else "rounds to zero shares")
            return
        commission = self.costs.commission(qty)
        self.ledger.buy(order.security_id, order.ticker, qty, fill_price, commission,
                        idx=i, date=self.panel.sessions[i], at=at, order=order)
        self._record(order, qty, fill_price, commission, at, i, "entry", status)

    def _record(self, order, qty, price, commission, at, i, reason, status) -> None:
        order.filled_qty = qty
        order.fill_price = price
        order.fill_date = self.panel.sessions[i]
        order.commission = commission
        self._finish(order, status)
        fill = O.Fill(
            date=self.panel.sessions[i], ticker=order.ticker, qty=qty, price=price,
            commission=commission, order_id=order.order_id, at=at, reason=reason, tag=order.tag,
        )
        self.engine.record_fill(fill, order)

    # --- stops, targets, stopped trading -------------------------------
    def check_stops_targets(self, i: int) -> None:
        for lot in sorted(self.ledger.lots(), key=lambda l: l.lot_id):
            if lot.stop is None and lot.target is None:
                continue
            if lot.entry_idx == i and lot.entry_at == "close":
                continue
            sid = lot.security_id
            o, h, l = (self.panel.value(f, i, sid) for f in ("open", "high", "low"))
            if np.isnan(o) or np.isnan(h) or np.isnan(l):
                continue
            stop, target = lot.stop, lot.target
            if stop is not None and o <= stop:
                price, kind = o, O.STOP
            elif target is not None and o >= target:
                price, kind = o, O.TARGET_HIT
            elif stop is not None and l <= stop:
                price, kind = stop, O.STOP
            elif target is not None and h >= target:
                price, kind = target, O.TARGET_HIT
            else:
                continue
            order = self.engine.engine_order(lot, kind="stop" if kind == O.STOP else "target_hit",
                                             at="intraday", i=i, exit_reason=kind)
            order.requested_qty = -lot.qty
            self._fill_sell(order, self.costs.slip(price, -1), lot.qty, "intraday", i)

    def close_stopped_trading(self, i: int) -> None:
        """Close positions in securities that have stopped trading, at their last close."""
        for sid in list(self.ledger.held()):
            col = self.panel.col(sid)
            last = int(self.panel.last_valid[col])
            if last >= i or last >= self.panel.last_data_idx:
                continue
            price = float(self.panel.arrays["close"][last, col])
            for lot in list(self.ledger.open_lots.get(sid, [])):
                lot.flags.append("stopped_trading")
                order = self.engine.engine_order(lot, kind=O.STOPPED_TRADING, at="last_close", i=i,
                                                 exit_reason=O.STOPPED_TRADING)
                order.requested_qty = -lot.qty
                order.message = f"no bars after {self.panel.sessions[last].date()}; closed at last close"
                self._fill_sell(order, price, lot.qty, "last_close", i)

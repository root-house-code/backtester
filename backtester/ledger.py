"""Portfolio ledger: cash, lots (trades), and the fill log.

Quantities are signed (positive = long) so short selling in V2 is an addition.
Positions are held in adjusted-share terms, so splits and dividends need no
special handling.

A *lot* is one trade record. Orders with exit rules (stop, target,
exit_after) always open their own lot. Plain buys add to the security's open
plain lot if there is one (scaling in). Sells without a lot target reduce lots
first-in, first-out. A lot's trade is complete when its quantity reaches zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .ids import SecurityId

EPS = 1e-9


@dataclass
class Lot:
    lot_id: int
    security_id: SecurityId
    ticker: str
    entry_idx: int
    entry_date: pd.Timestamp
    entry_at: str
    tag: str | None = None
    stop: float | None = None
    target: float | None = None
    exit_after: int | None = None
    qty: float = 0.0
    bought_qty: float = 0.0
    bought_value: float = 0.0
    sold_qty: float = 0.0
    sold_value: float = 0.0
    commission: float = 0.0
    exit_idx: int | None = None
    exit_date: pd.Timestamp | None = None
    exit_reason: str | None = None
    exit_at: str | None = None
    mae: float = 0.0
    mfe: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def plain(self) -> bool:
        return self.stop is None and self.target is None and self.exit_after is None

    @property
    def is_open(self) -> bool:
        return self.qty > EPS

    @property
    def avg_entry(self) -> float:
        return self.bought_value / self.bought_qty if self.bought_qty > EPS else float("nan")

    @property
    def exit_due_idx(self) -> int | None:
        return None if self.exit_after is None else self.entry_idx + self.exit_after

    def touch(self, low: float, high: float) -> None:
        """Update max adverse / favorable excursion with a price range."""
        avg = self.avg_entry
        if avg != avg or avg <= 0:
            return
        if low == low:
            self.mae = min(self.mae, low / avg - 1)
        if high == high:
            self.mfe = max(self.mfe, high / avg - 1)


class Ledger:
    def __init__(self, initial_cash: float):
        self.cash = float(initial_cash)
        self.open_lots: dict[SecurityId, list[Lot]] = {}
        self.closed_lots: list[Lot] = []
        self.fills: list[dict] = []
        self._next_lot = 1

    # --- queries -------------------------------------------------------
    def qty(self, sid: SecurityId) -> float:
        return sum(lot.qty for lot in self.open_lots.get(sid, ()))

    def held(self) -> list[SecurityId]:
        return [sid for sid, lots in self.open_lots.items() if lots]

    def lots(self) -> list[Lot]:
        return [lot for lots in self.open_lots.values() for lot in lots]

    def lot(self, lot_id: int) -> Lot | None:
        for lot in self.lots():
            if lot.lot_id == lot_id:
                return lot
        return None

    def avg_price(self, sid: SecurityId) -> float:
        lots = self.open_lots.get(sid, ())
        q = sum(l.qty for l in lots)
        if q <= EPS:
            return float("nan")
        return sum(l.qty * l.avg_entry for l in lots) / q

    # --- mutations -----------------------------------------------------
    def buy(self, sid, ticker, qty, price, commission, *, idx, date, at, order) -> Lot:
        assert qty > 0
        lot = None
        if not order.has_exit_rules:
            plain = [l for l in self.open_lots.get(sid, ()) if l.plain]
            lot = plain[-1] if plain else None
        if lot is None:
            lot = Lot(
                lot_id=self._next_lot, security_id=sid, ticker=ticker,
                entry_idx=idx, entry_date=date, entry_at=at, tag=order.tag,
                stop=order.stop, target=order.target, exit_after=order.exit_after,
            )
            self._next_lot += 1
            self.open_lots.setdefault(sid, []).append(lot)
        lot.qty += qty
        lot.bought_qty += qty
        lot.bought_value += qty * price
        lot.commission += commission
        self.cash -= qty * price + commission
        return lot

    def sell(self, sid, qty, price, commission, *, idx, date, at, reason, lot_id=None) -> list[tuple[Lot, float]]:
        """Reduce long lots by ``qty`` shares (FIFO, or one lot). Returns (lot, shares) pairs."""
        assert qty > 0
        lots = self.open_lots.get(sid, [])
        if lot_id is not None:
            lots = [l for l in lots if l.lot_id == lot_id]
        remaining, taken = qty, []
        for lot in list(lots):
            if remaining <= EPS:
                break
            q = min(lot.qty, remaining)
            taken.append((lot, q))
            remaining -= q
        sold = qty - remaining
        for lot, q in taken:
            share = commission * (q / sold) if sold > 0 else 0.0
            lot.qty -= q
            if lot.qty <= EPS:
                lot.qty = 0.0
            lot.sold_qty += q
            lot.sold_value += q * price
            lot.commission += share
            if not lot.is_open:
                lot.exit_idx, lot.exit_date, lot.exit_reason, lot.exit_at = idx, date, reason, at
                self.open_lots[sid].remove(lot)
                self.closed_lots.append(lot)
        if sid in self.open_lots and not self.open_lots[sid]:
            del self.open_lots[sid]
        self.cash += sold * price - commission
        return taken

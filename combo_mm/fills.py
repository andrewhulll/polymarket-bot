"""Position tracking derived from the Drop Copy fills ledger.

Fills themselves live in the store's ``fills`` table (written exactly-once
from Drop Copy records; see :mod:`combo_mm.store`). :class:`FillsLedger` is
a pure read model over it: net quantity and volume-weighted average price
per symbol, computed from the fill rows -- no separate position table, so a
duplicate fill can never double-count.

Side convention (our fills as the maker): ``BUY`` adds to the position,
``SELL`` subtracts.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from combo_mm.store import EventStore

__all__ = ["FillsLedger"]


class FillsLedger:
    """Read model: positions derived from the store's fills."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    def get_position(self, symbol: str) -> Optional[Dict[str, object]]:
        """Net qty (Decimal string) + VWAP for one symbol, or None if flat."""
        fills = self._store.get_fills_for_position(symbol)
        net = Decimal(0)
        cost = Decimal(0)  # signed notional at fill prices
        for f in fills:
            qty = Decimal(str(f["qty"] or 0))
            price = Decimal(str(f["price"] or 0))
            dq = qty if f["side"] == "BUY" else -qty
            prev_net = net
            net += dq
            if prev_net == 0:
                cost = net * price
            elif (prev_net > 0) == (dq > 0) and abs(net) > abs(prev_net):
                cost += dq * price  # adding: extend cost basis
            elif (prev_net > 0) != (net > 0) and net != 0:
                cost = net * price  # flipped through zero: rebase
            # reducing but not flipping: cost basis unchanged (realized later)
        if net == 0:
            return None
        return {
            "symbol": symbol,
            "net_qty": str(net),
            "avg_price": float(cost / net),
        }

    def all_positions(self) -> List[Dict[str, object]]:
        """Every symbol with a non-zero net position."""
        symbols = {f["symbol"] for f in self._store.get_fills_for_position()
                   if f.get("symbol")}
        out = []
        for symbol in sorted(symbols):
            pos = self.get_position(symbol)
            if pos is not None:
                out.append(pos)
        return out

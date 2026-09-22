"""Replay and reserve capital for the assumed paper-fill ledger.

The live dashboard treats every winning paper quote as a fill.  This module
keeps that counterfactual net notional behind a hard equity boundary and,
critically, latches the boundary once it is reached.  A later opposite-side
RFQ must not make quoting resume after the paper account has used all equity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3


EPSILON = 1e-9


@dataclass
class PaperCapitalState:
    equity: float
    net_notional: float = 0.0
    exhausted: bool = False
    admitted_ids: set[str] = field(default_factory=set)
    rejected_ids: set[str] = field(default_factory=set)
    allocations: dict[str, float] = field(default_factory=dict)
    response_prices: dict[str, float] = field(default_factory=dict)

    def available_allocation(self, proposed: float) -> float:
        """Return the signed notional that still fits, without mutating state."""
        if self.exhausted or abs(proposed) < EPSILON:
            return 0.0
        bounded = max(-self.equity, min(self.equity,
                                       self.net_notional + proposed))
        return bounded - self.net_notional

    def commit(self, rfq_id: str, allocated: float,
               response_price: float | None = None) -> None:
        self.admitted_ids.add(rfq_id)
        self.allocations[rfq_id] = allocated
        if response_price is not None:
            self.response_prices[rfq_id] = response_price
        self.net_notional += allocated
        if abs(self.net_notional) >= self.equity - EPSILON:
            self.net_notional = self.equity if self.net_notional >= 0 else -self.equity
            self.exhausted = True

    def release_lost_quote(self, rfq_id: str) -> None:
        """Release a quote later shown to have lost, unless the cap latched."""
        if self.exhausted:
            return
        allocated = self.allocations.pop(rfq_id, None)
        self.response_prices.pop(rfq_id, None)
        if allocated is not None:
            self.net_notional -= allocated


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {str(row[1]) for row in conn.execute(
        f"PRAGMA table_info({table})"
    )}


def replay_paper_capital(conn: sqlite3.Connection, equity: float) -> PaperCapitalState:
    """Rebuild the quote-time paper capital gate from durable quote history.

    Quotes which an observed accepted trade beat never become assumed fills.
    Fully settled quotes no longer contribute current net notional.  Once an
    unsettled assumed fill reaches the equity boundary, every later quote is
    classified as a capital rejection even if its direction would unwind the
    position; this mirrors the live fail-closed latch.
    """
    state = PaperCapitalState(max(0.0, float(equity)))
    required = ("priced_quotes", "quotes", "live_trades", "rfq_legs")
    if state.equity <= 0 or any(not _has_table(conn, name) for name in required):
        state.exhausted = state.equity <= 0
        return state

    # Older capture databases predate side-specific quantities.  Keep them
    # readable by falling back to the original size/size_unit columns.
    bid_qty = "p.bid_qty" if _has_column(conn, "priced_quotes", "bid_qty") else "NULL"
    ask_qty = "p.ask_qty" if _has_column(conn, "priced_quotes", "ask_qty") else "NULL"
    rows = conn.execute(f"""
        SELECT p.rfq_id, p.priced_at, p.response_action, p.response_price,
               {bid_qty} AS bid_qty, {ask_qty} AS ask_qty, p.size, p.size_unit,
               t.price AS market_price, t.executed_at,
               (SELECT COUNT(*) FROM rfq_legs l
                WHERE l.rfq_id=p.rfq_id) AS leg_count,
               (SELECT COUNT(*) FROM rfq_legs l
                WHERE l.rfq_id=p.rfq_id AND l.settlement_price IS NOT NULL)
                   AS settled_count
        FROM priced_quotes p
        LEFT JOIN live_trades t ON t.rfq_id=p.rfq_id
        WHERE p.trigger='auto' AND p.status='QUOTED'
          AND EXISTS (SELECT 1 FROM quotes q WHERE q.rfq_id=p.rfq_id
                      AND q.status='shadow')
        ORDER BY p.priced_at, p.rowid
    """).fetchall()

    for raw in rows:
        row = dict(raw) if isinstance(raw, sqlite3.Row) else {
            "rfq_id": raw[0], "priced_at": raw[1], "response_action": raw[2],
            "response_price": raw[3], "bid_qty": raw[4], "ask_qty": raw[5],
            "size": raw[6], "size_unit": raw[7], "market_price": raw[8],
            "executed_at": raw[9], "leg_count": raw[10], "settled_count": raw[11],
        }
        rfq_id = str(row["rfq_id"])
        if state.exhausted:
            state.rejected_ids.add(rfq_id)
            continue
        state.admitted_ids.add(rfq_id)

        # A trade which happened before our decision, or which beat our paper
        # price, means this admitted quote was not an assumed fill.
        if row["executed_at"] and row["priced_at"]:
            try:
                from datetime import datetime
                decided = datetime.fromisoformat(str(row["priced_at"]).replace("Z", "+00:00"))
                executed = datetime.fromisoformat(str(row["executed_at"]).replace("Z", "+00:00"))
                if decided > executed:
                    continue
            except (TypeError, ValueError):
                pass
        price = row["response_price"]
        action = row["response_action"]
        market = row["market_price"]
        if price is None or action not in ("BUY", "SELL"):
            continue
        price = float(price)
        if market is not None and ((action == "BUY" and price < float(market)) or
                                   (action == "SELL" and price > float(market))):
            continue
        if row["leg_count"] and row["leg_count"] == row["settled_count"]:
            continue

        raw_qty = row["bid_qty"] if action == "BUY" else row["ask_qty"]
        if raw_qty is None:
            qty = float(row["size"] or 0)
            if row["size_unit"] == "notional" and price > 0:
                qty /= price
        else:
            qty = float(raw_qty or 0)
        if qty <= 0:
            continue
        proposed = (1.0 if action == "BUY" else -1.0) * price * qty
        allocated = state.available_allocation(proposed)
        if abs(allocated) < EPSILON:
            state.admitted_ids.discard(rfq_id)
            state.rejected_ids.add(rfq_id)
            state.exhausted = True
            continue
        state.commit(rfq_id, allocated, price)
    return state

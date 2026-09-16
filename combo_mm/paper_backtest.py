"""Paper/shadow backtest computation (dashboard-only).

Replays the fixture/simulated dataset through the pipeline + V1 pricer +
shadow quotes with the virtual clock, drains the simulated Drop Copy feed
for fills, then computes backtest-style metrics:

- RFQs received / quoted / rejected / expired / executed
- quote rate, execution rate (executed / quoted)
- expected P&L: sum of expected edge at quote time (assumes the quoted size
  fills on one side, capturing half-spread vs fair)
- realized P&L: Drop Copy fills vs combo settlement values (from the durable
  leg-settlement reconciliation -- the stream never carries settlements)
- max downswing (max peak-to-trough of cumulative realized P&L),
  max upswing (max trough-to-peak)
- inventory/exposure over time: net position notional from the fills ledger
  over virtual time

No future information: the replay processes items in timestamp order, so the
pricer only ever sees book snapshots with timestamps <= the event time. The
replay loop asserts this ordering explicitly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from combo_mm.books import LegBookCache
from combo_mm.config import PipelineConfig
from combo_mm.dropcopy import SimulatedDropCopyTransport, drain_drop_copy
from combo_mm.fixtures import BASE_TS, SELF_USER_ID
from combo_mm.normalize import normalize
from combo_mm.pricing import QUOTED_OK
from combo_mm.reference import ReferenceCache
from combo_mm.shadow import ShadowQuoter
from combo_mm.store import EventStore
from combo_mm.stream import SimulatedTransport

log = logging.getLogger(__name__)

__all__ = ["BacktestResult", "run_backtest", "combo_settlement_value"]


def combo_settlement_value(legs: List[Dict[str, Any]]) -> Optional[float]:
    """Combo settlement from RAW leg settlements: product of q_i.

    ``settlement_price`` is the leg's own YES/LONG result in [0,1]; the
    YES/NO inversion happens here (and only here), never at ingest.
    Returns None when any leg lacks a settlement price (not settled yet).
    """
    value = 1.0
    for leg in legs:
        sp = leg.get("settlement_price")
        if sp is None:
            return None
        sp = float(sp)
        value *= sp if leg.get("side") == "YES" else (1.0 - sp)
    return value


@dataclass
class BacktestResult:
    rfqs_received: int = 0
    rfqs_quoted: int = 0
    rfqs_rejected: int = 0
    rfqs_expired: int = 0
    rfqs_executed: int = 0
    quote_rate: float = 0.0
    execution_rate: float = 0.0
    expected_pnl: float = 0.0
    realized_pnl: float = 0.0
    max_downswing: float = 0.0
    max_upswing: float = 0.0
    n_fills: int = 0
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    exposure_curve: List[Tuple[str, float]] = field(default_factory=list)
    per_rfq: List[Dict[str, Any]] = field(default_factory=list)


def run_backtest(session: List[Dict[str, Any]],
                 combos: List[Dict[str, Any]],
                 drop_copy_records: List[Dict[str, Any]],
                 config: PipelineConfig,
                 *,
                 self_user_id: str = SELF_USER_ID,
                 db_path: str = ":memory:") -> Tuple[BacktestResult, EventStore]:
    """Full-information replay (incl. stream-invisible events) + metrics.

    Returns ``(result, store)``; the store backs the dashboard's RFQ and
    pricing views so all three views read one consistent run.
    """
    config.validate()
    store = EventStore(db_path)
    transport = SimulatedTransport(session, self_user_id, combos)
    books = LegBookCache(staleness_ms=config.staleness_ms)
    reference = ReferenceCache(transport, ttl_s=config.reference_ttl_s)
    quoter = ShadowQuoter(store, books, reference, config)

    last_t = -1
    for item in sorted(session, key=lambda i: i.get("t", 0)):
        t = item.get("t", 0)
        # No future information: timestamps are non-decreasing, so every book
        # snapshot applied so far has ts <= the current event's ts.
        assert t >= last_t, f"session out of order at t={t}"
        last_t = t
        kind = item.get("kind")
        if kind == "disconnect":
            continue
        if kind == "book":
            books.update(
                symbol=item["symbol"], bid=item.get("bid"), ask=item.get("ask"),
                bid_size=item.get("bid_size"), ask_size=item.get("ask_size"),
                updated_at=item.get("ts"), seq=item.get("seq"),
            )
            store.ingest_book(
                item["symbol"], item.get("bid"), item.get("ask"),
                item.get("bid_size", 0.0), item.get("ask_size", 0.0),
                item.get("seq", 0), item.get("ts"))
            continue
        if kind != "event":
            continue
        now = BASE_TS + timedelta(milliseconds=t)
        event = normalize(item["raw"], now=now)
        if store.apply(event) and event.event_type in ("rfq_created", "rfq_updated"):
            quoter.maybe_quote(event)

    # Fills reconcile exclusively through Drop Copy.
    drain_drop_copy(SimulatedDropCopyTransport(drop_copy_records), store,
                    now=BASE_TS)

    result = _compute_metrics(store)
    return result, store


def _compute_metrics(store: EventStore) -> BacktestResult:
    res = BacktestResult()
    rfqs = store.list_rfqs()
    res.rfqs_received = len(rfqs)

    # Latest shadow decision per RFQ.
    latest: Dict[str, Any] = {}
    for d in store.get_shadow_decisions(limit=10000):
        latest.setdefault(d["rfq_id"], d)
    res.rfqs_quoted = sum(1 for d in latest.values()
                          if d["decision"] == QUOTED_OK)
    res.rfqs_rejected = sum(1 for d in latest.values()
                            if d["decision"] != QUOTED_OK)
    res.rfqs_expired = sum(1 for r in rfqs if r["status"] == "EXPIRED")
    res.rfqs_executed = sum(
        1 for r in rfqs
        if any(q["status"] == "EXECUTED"
               for q in store.get_quotes_for_rfq(r["rfq_id"])))
    res.quote_rate = res.rfqs_quoted / res.rfqs_received if res.rfqs_received else 0.0
    res.execution_rate = res.rfqs_executed / res.rfqs_quoted if res.rfqs_quoted else 0.0

    # Expected P&L: sum of expected edge at quote time. Expected edge per
    # unit is the half-spread in price units (spread_bps / 10000); the shadow
    # table stores spread_bps and the RFQ table stores the requested qty.
    rfq_by_id = {r["rfq_id"]: r for r in rfqs}
    expected = Decimal(0)
    for d in latest.values():
        if d["decision"] == QUOTED_OK:
            r = rfq_by_id.get(d["rfq_id"]) or {}
            qty = r.get("qty_decimal")
            if not qty:
                # Cash-sized RFQs have no qty_decimal: use the smaller live
                # quoted side size (conservative; "0" means side unquoted).
                sides = [Decimal(str(s)) for s in (d.get("buy_qty"), d.get("sell_qty"))
                         if s and Decimal(str(s)) > 0]
                qty = min(sides) if sides else 0
            expected += (Decimal(str(d["spread_bps"] or 0)) / Decimal(10000)
                         * Decimal(str(qty)))
    res.expected_pnl = float(expected)

    # Realized P&L: Drop Copy fills vs combo settlement values.
    settle: Dict[str, Optional[float]] = {}
    for r in rfqs:
        legs = (store.get_rfq(r["rfq_id"]) or {}).get("legs") or []
        settle[r["rfq_id"]] = combo_settlement_value(legs)

    fills = store.get_fills_for_position()
    res.n_fills = len(fills)
    cum = 0.0
    peak = trough = 0.0
    max_dd = max_up = 0.0
    equity: List[Tuple[str, float]] = []
    net: Dict[str, Decimal] = {}
    last_price: Dict[str, float] = {}
    exposure: List[Tuple[str, float]] = []
    for f in fills:
        s = settle.get(f["rfq_id"] or "")
        qty = Decimal(str(f["qty"] or 0))
        price = float(f["price"] or 0)
        if s is not None:
            pnl = ((s - price) * float(qty) if f["side"] == "BUY"
                   else (price - s) * float(qty))
            cum += pnl
            peak = max(peak, cum)
            trough = min(trough, cum)
            max_dd = max(max_dd, peak - cum)
            max_up = max(max_up, cum - trough)
        equity.append((f["executed_time"] or "", cum))
        dq = qty if f["side"] == "BUY" else -qty
        sym = f["symbol"] or ""
        net[sym] = net.get(sym, Decimal(0)) + dq
        last_price[sym] = price
        notional = sum(abs(float(net[k])) * last_price[k] for k in net)
        exposure.append((f["executed_time"] or "", notional))

    res.realized_pnl = cum
    res.max_downswing = max_dd
    res.max_upswing = max_up
    res.equity_curve = equity
    res.exposure_curve = exposure

    for r in rfqs:
        d = latest.get(r["rfq_id"])
        res.per_rfq.append(
            {
                "rfq_id": r["rfq_id"],
                "symbol": r["symbol"],
                "status": r["status"],
                "reason_code": d["decision"] if d else None,
                "fair": d["fair_price"] if d else None,
                "settlement": settle.get(r["rfq_id"]),
            }
        )
    return res

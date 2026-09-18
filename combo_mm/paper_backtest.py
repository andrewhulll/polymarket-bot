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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from combo_mm.backtest.metrics import compute as _shared_compute
from combo_mm.backtest.metrics import swings as _shared_swings
from combo_mm.backtest.metrics import (
    combo_settlement_value as _combo_settlement_value,
)
from combo_mm.books import LegBookCache
from combo_mm.config import PipelineConfig
from combo_mm.dropcopy import SimulatedDropCopyTransport, drain_drop_copy
from combo_mm.engine import ShadowQuotingEngine
from combo_mm.fixtures import BASE_TS, SELF_USER_ID
from combo_mm.normalize import normalize
from combo_mm.pricing import QUOTED_OK
from combo_mm.reference import ReferenceCache
from combo_mm.store import EventStore
from combo_mm.stream import SimulatedTransport

log = logging.getLogger(__name__)

__all__ = ["BacktestResult", "run_backtest", "compute_metrics", "combo_settlement_value"]


combo_settlement_value = _combo_settlement_value
"""Combo settlement from RAW leg settlements: product of q_i.

Single implementation lives in :mod:`combo_mm.backtest.metrics`; this alias
keeps the historical import path working.
"""


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
                 db_path: str = ":memory:",
                 base_ts: Optional[datetime] = None) -> Tuple[BacktestResult, EventStore]:
    """Full-information replay (incl. stream-invisible events) + metrics.

    Returns ``(result, store)``; the store backs the dashboard's RFQ and
    pricing views so all three views read one consistent run. ``base_ts`` is
    the virtual clock's origin for the session's ``t`` offsets (a generated
    NFL dataset carries its own; see :mod:`combo_mm.nfl.rfq_sim`).
    """
    config.validate()
    base_ts = base_ts or BASE_TS
    store = EventStore(db_path)
    transport = SimulatedTransport(session, self_user_id, combos)
    books = LegBookCache(staleness_ms=config.staleness_ms)
    reference = ReferenceCache(transport, ttl_s=config.reference_ttl_s)
    engine = ShadowQuotingEngine(store, books, reference, config,
                                 params_version=config.params_version)

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
        now = base_ts + timedelta(milliseconds=t)
        event = normalize(item["raw"], now=now)
        if store.apply(event) and event.event_type in ("rfq_created", "rfq_updated"):
            engine.maybe_quote(event)

    # Fills reconcile exclusively through Drop Copy.
    drain_drop_copy(SimulatedDropCopyTransport(drop_copy_records), store,
                    now=base_ts)

    result = compute_metrics(store)
    return result, store


def compute_metrics(store: EventStore) -> BacktestResult:
    """Aggregate replay stats.

    Thin wrapper over the shared :mod:`combo_mm.backtest.metrics`
    implementation (issue #5 item A): the legacy ``expected_pnl`` is the
    shared ``quoted_edge_notional`` (quoted half-spread over all quotes,
    relabeled), and swings run on the legacy fill-time equity curve so
    dashboard numbers are unchanged.
    """
    m = _shared_compute(store)
    s = _shared_swings(m.equity_curve)
    return BacktestResult(
        rfqs_received=m.rfqs_received,
        rfqs_quoted=m.rfqs_quoted,
        rfqs_rejected=m.rfqs_rejected,
        rfqs_expired=m.rfqs_expired,
        rfqs_executed=m.rfqs_executed,
        quote_rate=m.quote_rate,
        execution_rate=m.execution_rate,
        expected_pnl=m.quoted_edge_notional,
        realized_pnl=m.realized_pnl,
        max_downswing=s["max_downswing"],
        max_upswing=s["max_upswing"],
        n_fills=m.n_fills,
        equity_curve=m.equity_curve,
        exposure_curve=[(r["ts"], r["net_notional"]) for r in m.exposure_curve],
        per_rfq=[{
            "rfq_id": row["rfq_id"],
            "symbol": row["symbol"],
            "status": row["status"],
            "reason_code": row["decision"],
            "fair": row["fair"],
            "settlement": row["settlement"],
        } for row in m.per_rfq],
    )

"""Reconnect recovery: durable reads in the contract-mandated order.

Startup/reconnect procedure (per the Polymarket US gRPC docs):

  1. Open the stream (``StreamRFQEvents``) -- the subscription starts
     delivering NEW events from this point. There is no replay and no
     gap-free handoff between the durable reads below and the stream, so
     application is idempotent (event dedup + per-entity ``updatedTime``
     monotonicity); a late duplicate is harmless.
  2. ``GetRFQs(open)`` -- every actionable RFQ (empty is valid).
  3. ``GetQuotes(self)`` -- our own quotes (empty is valid).
  4. Only then apply subsequent stream events.

Missed closes are the critical case: an RFQ that closed while we were
disconnected looks locally OPEN. Two mechanisms catch it:

- ``GetRFQs(open)`` returns the exchange's actionable set; a locally
  non-terminal RFQ ABSENT from that snapshot must have gone terminal --
  :meth:`combo_mm.store.EventStore.mark_rfq_closed_by_absence` marks it
  CLOSED (legs untouched, terminal states never regress).
- A durable RFQ whose status is terminal always wins over a non-terminal
  local state.

Leg settlements are reconciled here too: the public stream emits no
settlement event, so ``GetRFQs`` is the only place they appear. RFQs we
have fills in get a targeted durable refresh so P&L can settle (the OPEN
read excludes terminal RFQs, which is exactly where settlements live).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from combo_mm.events import RFQ_TERMINAL_STATUSES

log = logging.getLogger(__name__)

__all__ = ["recovery_sync", "RecoveryReport", "OPEN_RFQ_READ_FILTER"]


#: The durable RFQ read must cover every non-terminal (actionable) RFQ.
OPEN_RFQ_READ_FILTER = "OPEN"


@dataclass
class RecoveryReport:
    """Outcome of one :func:`recovery_sync` pass.

    ``added`` / ``refreshed`` / ``ignored`` count RFQ + quote entities
    inserted, updated, or left alone by the durable reads;
    ``marked_closed`` counts locally-open RFQs inferred CLOSED by absence
    from ``GetRFQs(open)``.
    """

    added: int = 0
    marked_closed: int = 0
    refreshed: int = 0
    ignored: int = 0
    settled_rfqs: List[str] = field(default_factory=list)


def _rfq_to_wire(durable: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a durable RFQ dict to a wire-shaped payload for the store."""
    return {
        "id": durable.get("rfq_id") or durable.get("id"),
        "symbol": durable.get("symbol"),
        "rfqCreatorUserId": durable.get("rfqCreatorUserId"),
        "createdTime": durable.get("createdTime"),
        "updatedTime": durable.get("updatedTime"),
        "restRemainder": durable.get("restRemainder", False),
        "status": durable.get("status"),
        "qtyDecimal": durable.get("qtyDecimal"),
        "cashOrderQty": durable.get("cashOrderQty"),
        "comboLegs": durable.get("comboLegs") or [],
    }


def _quote_to_wire(durable: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a durable quote dict to a wire-shaped payload for the store."""
    return {
        "id": durable.get("quote_id") or durable.get("id"),
        "rfqId": durable.get("rfq_id") or durable.get("rfqId"),
        "creatorRfqUserId": durable.get("creatorRfqUserId"),
        "symbol": durable.get("symbol"),
        "status": durable.get("status"),
        "createdTime": durable.get("createdTime"),
        "updatedTime": durable.get("updatedTime"),
        "buyPrice": durable.get("buyPrice"),
        "sellPrice": durable.get("sellPrice"),
        "buyQtyDecimal": durable.get("buyQtyDecimal"),
        "sellQtyDecimal": durable.get("sellQtyDecimal"),
        "acceptedSide": durable.get("acceptedSide"),
        "confirmationDeadline": durable.get("confirmationDeadline"),
        "executionDeadline": durable.get("executionDeadline"),
        "orderId": durable.get("orderId"),
        "clientOrderId": durable.get("clientOrderId"),
    }


def recovery_sync(transport: Any, store: Any,
                  self_user_id: Optional[str] = None) -> RecoveryReport:
    """Run steps 2-4 of the startup/reconnect procedure.

    Steps are performed IN ORDER: RFQ read first, quote read second, then
    stream events resume (the consumer drains its dispatch queue before
    calling this, so no stream event interleaves mid-recovery).

    Returns a :class:`RecoveryReport`: counts of inserted/refreshed/ignored
    entities and the settled RFQ ids (for the backtest/P&L path).
    """
    self_user_id = self_user_id or transport.get_rfq_user_id()

    report = RecoveryReport()

    # Step 2: durable RFQ read (GetRFQs(open)). Empty is a valid snapshot.
    durable_rfqs: List[Dict[str, Any]] = transport.get_rfqs(
        status=OPEN_RFQ_READ_FILTER)
    log.info("recovery: GetRFQs(%s) returned %d RFQs",
             OPEN_RFQ_READ_FILTER, len(durable_rfqs))
    durable_ids = set()
    for durable in durable_rfqs:
        rid = durable.get("rfq_id") or durable.get("id")
        if rid:
            durable_ids.add(rid)
        try:
            outcome = store.apply_durable_rfq(_rfq_to_wire(durable))
        except ValueError:
            log.warning("recovery: skipping malformed durable RFQ %r", durable,
                        exc_info=True)
            continue
        if outcome == "inserted":
            report.added += 1
        elif outcome == "refreshed":
            report.refreshed += 1
        else:
            report.ignored += 1
        if any(leg.get("settlementPrice") is not None
               for leg in (durable.get("comboLegs") or [])):
            report.settled_rfqs.append(rid)

    # Absence inference: locally non-terminal RFQs missing from the durable
    # OPEN snapshot went terminal while we were away.
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for local in store.list_rfqs():
        if local["status"] in RFQ_TERMINAL_STATUSES:
            continue
        if local["rfq_id"] in durable_ids:
            continue
        if store.mark_rfq_closed_by_absence(local["rfq_id"], now_iso):
            report.marked_closed += 1
    if report.marked_closed:
        log.info("recovery: %d RFQs closed by absence from GetRFQs(open)",
                 report.marked_closed)

    # Settlement reconciliation: RFQs we have fills in need their latest
    # legs; the OPEN read excludes terminal RFQs, so refresh them directly.
    fill_rfq_ids = {f["rfq_id"] for f in store.get_fills_for_position()
                    if f.get("rfq_id")}
    if fill_rfq_ids:
        for durable in transport.get_rfqs(status=None):
            rid = durable.get("rfq_id") or durable.get("id")
            if rid not in fill_rfq_ids:
                continue
            try:
                store.apply_durable_rfq(_rfq_to_wire(durable))
            except ValueError:
                continue
            if (rid not in report.settled_rfqs and any(
                    leg.get("settlementPrice") is not None
                    for leg in (durable.get("comboLegs") or []))):
                report.settled_rfqs.append(rid)

    # Step 3: durable quote read (GetQuotes(self)). Empty is valid.
    durable_quotes: List[Dict[str, Any]] = transport.get_quotes(
        user_filter=self_user_id)
    log.info("recovery: GetQuotes(%s) returned %d quotes",
             self_user_id, len(durable_quotes))
    for durable in durable_quotes:
        try:
            outcome = store.apply_durable_quote(_quote_to_wire(durable))
        except ValueError:
            log.warning("recovery: skipping malformed durable quote %r", durable,
                        exc_info=True)
            continue
        if outcome == "inserted":
            report.added += 1
        elif outcome == "refreshed":
            report.refreshed += 1
        else:
            report.ignored += 1

    # Step 4 happens in the consumer: it resumes applying stream events only
    # after this function returns.
    return report

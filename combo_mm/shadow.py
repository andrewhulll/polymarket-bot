"""Shadow quoting: compute V1 draft quotes, store them, never submit.

For each eligible RFQ the :class:`ShadowQuoter` runs the V1 pricer and records
the outcome in the ``shadow_decisions`` table -- quotes AND declines, each
with its reason code and spread components for the dashboard.

Leg settlements are the RAW wire ``settlementPrice`` (YES/LONG result in
[0,1], never inverted at ingest); the YES/NO -> ``q_i`` mapping happens in
the pricer via ``resolved_price``.

Paper-mode invariant: this module has no transport reference and no code path
that can emit an outbound quote RPC. The intended ``CreateQuote`` call is
logged as NOT SENT. A test asserts ``SimulatedTransport.create_quote_calls``
stays empty after a full pipeline run.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from combo_mm.books import LegBookCache
from combo_mm.config import PipelineConfig
from combo_mm.events import NormalizedEvent, RFQ_TERMINAL_STATUSES
from combo_mm.pricing import LegMarkInput, QuoteDecision, price_combo
from combo_mm.reference import ReferenceCache
from combo_mm.store import EventStore

log = logging.getLogger(__name__)

__all__ = ["ShadowQuoter", "MODEL_VERSION"]

MODEL_VERSION = "v1"


def _iso_to_ms(value: Optional[str]) -> int:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return 0


class ShadowQuoter:
    """Paper-only quoter: prices RFQs and records decisions, submits nothing."""

    def __init__(self, store: EventStore, books: LegBookCache,
                 reference: ReferenceCache, config: PipelineConfig,
                 model_version: str = MODEL_VERSION) -> None:
        self._store = store
        self._books = books
        self._reference = reference
        self._config = config
        self._model_version = model_version

    def maybe_quote(self, event: NormalizedEvent) -> Optional[QuoteDecision]:
        """Price the RFQ from ``event`` and record the decision.

        Only ``rfq_created`` / ``rfq_updated`` trigger (re)quotes. Staleness
        is measured in *exchange time* (``event.event_at``), so replays and
        the demo stay deterministic regardless of wall-clock.
        """
        if event.event_type not in ("rfq_created", "rfq_updated"):
            return None
        rfq = self._store.get_rfq(event.rfq_id) if event.rfq_id else None
        if rfq is None:
            return None

        legs = list(rfq["legs"] or [])
        if not legs and rfq["symbol"]:
            meta = self._reference.get(rfq["symbol"])
            if meta is not None:
                legs = [
                    {"symbol": l["symbol"], "side": l.get("side", "YES"),
                     "settlement_price": None}
                    for l in meta.legs
                ]

        asof_ms = _iso_to_ms(event.event_at)
        snapshots = self._books.get([l["symbol"] for l in legs], now_ms=asof_ms)
        inputs: List[LegMarkInput] = []
        for l in legs:
            snap = snapshots[l["symbol"]]
            # Raw wire settlement -> pricer resolved_price. The pricer applies
            # the YES/NO inversion when computing q_i; nothing is inverted
            # here.
            inputs.append(LegMarkInput(
                symbol=l["symbol"],
                side=l.get("side") or "YES",
                bid=snap.bid,
                ask=snap.ask,
                bid_size=snap.bid_size,
                ask_size=snap.ask_size,
                stale=snap.stale or snap.missing,
                resolved_price=(float(l["settlement_price"])
                                if l.get("settlement_price") is not None else None),
            ))

        cfg = self._config
        qty = rfq["qty_decimal"]
        decision = price_combo(
            inputs,
            rfq_id=event.rfq_id or "",
            rfq_status=rfq["status"],
            qty_decimal=str(qty) if qty is not None else None,
            cash_order_qty=(str(rfq["cash_order_qty"])
                            if rfq["cash_order_qty"] is not None else None),
            model_version=self._model_version,
            decided_at=event.event_at,
            base_edge_bps=cfg.base_edge_bps,
            uncertainty_per_leg_bps=cfg.uncertainty_per_leg_bps,
            width_weight=cfg.width_weight,
            depth_slope_bps=cfg.depth_slope_bps,
            event_risk_bps=cfg.event_risk_bps,
            operational_buffer_bps=cfg.operational_buffer_bps,
            tick_size=cfg.tick_size,
            price_min=cfg.price_min,
            price_max=cfg.price_max,
            min_qty=cfg.min_qty,
        )
        self._store.record_shadow_decision(
            rfq_id=event.rfq_id or "",
            decision=(decision.reason_code),
            reason=f"model={self._model_version}",
            fair_price=decision.fair,
            buy_price=decision.buy_price,
            sell_price=decision.sell_price,
            spread_bps=decision.components.get("spread_bps_total"),
            expected_edge_bps=decision.expected_edge_bps,
            buy_qty=decision.buy_qty,
            sell_qty=decision.sell_qty,
            components_json=json.dumps(decision.components),
            ts=event.event_at,
        )
        # Paper mode: the outbound RPC is logged, never sent.
        log.info(
            "PAPER MODE: CreateQuote RPC for rfq=%s NOT SENT "
            "(fair=%.4f buy=%.3f sell=%.3f reason=%s)",
            event.rfq_id, decision.fair,
            decision.buy_price, decision.sell_price, decision.reason_code,
        )
        return decision

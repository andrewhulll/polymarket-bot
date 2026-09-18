"""Shadow quoting engine (issue #4): the always-on paper-trading loop.

For every eligible RFQ: eligibility filter -> pricer (#2 seam) -> risk
check (#3 seam) -> two-sided draft quote, stored in the ``quotes`` table
with ``status='shadow'``. Every outcome (quote, pricer decline, risk
decline, eligibility skip) is recorded in ``shadow_decisions`` with its
reason, so the dashboard can show RFQs seen vs quoted vs skipped.

SAFETY INVARIANT (hard requirements):
- This module has no call path that can emit an outbound quote RPC. It
  holds a ``ReferenceCache`` (read-only combo metadata over the transport),
  but nothing in the engine or its transitive imports references any
  quote-submission call; the retail adapter is read-only by construction.
- ``PAPER_MODE`` guard: the engine refuses to construct unless
  ``config.paper_mode`` is True (raises :class:`PaperModeError`), snapshots
  the flag, and re-checks it on every ``maybe_quote`` call.
- Every draft is reproducible: the full input snapshot (leg marks/prices,
  model version, params version, inventory state, spread knobs) is stored
  alongside the quote as canonical JSON.
- All timestamps come from exchange/virtual time (``event.event_at``);
  the engine never reads the wall clock.

The intended outbound call is logged as NOT SENT, never issued.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional

from combo_mm.books import LegBookCache
from combo_mm.config import PipelineConfig
from combo_mm.eligibility import check_eligibility
from combo_mm.events import NormalizedEvent
from combo_mm.pricer import Pricer, PricerResult, V1NaivePricer
from combo_mm.pricing import QUOTED_OK, LegMarkInput
from combo_mm.reference import ReferenceCache
from combo_mm.risk import (
    ConservativeRiskCheck,
    InventoryState,
    RiskCheck,
    RiskVerdict,
    RISK_KILL_SWITCH,
)
from combo_mm.inventory import InventoryProvider
from combo_mm.risk_policy import InventoryRiskCheck
from combo_mm.store import EventStore

log = logging.getLogger(__name__)

__all__ = [
    "PaperModeError",
    "DraftQuote",
    "ShadowQuotingEngine",
    "ENGINE_VERSION",
    "DECIDED_BY",
]

ENGINE_VERSION = "engine-v2"
DECIDED_BY = "shadow-engine"


class PaperModeError(Exception):
    """Raised when the engine is constructed with paper mode off."""


@dataclass(frozen=True)
class DraftQuote:
    """A stored-but-never-sent two-sided draft quote."""

    quote_id: str
    rfq_id: str
    status: str          # always 'shadow'
    origin: str          # always 'shadow'; the schema makes live unmistakable
    fair: float
    buy_price: float     # our offer (0.0 = side unavailable)
    sell_price: float    # our bid   (0.0 = side unavailable)
    buy_qty: str
    sell_qty: str
    expected_edge_bps: float
    model_version: str
    params_version: str
    input_snapshot_json: str   # canonical JSON: full reproducible inputs
    decided_at: str            # exchange time, never wall clock
    decided_by: str = DECIDED_BY


def _iso_to_ms(value: Optional[str]) -> int:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return 0


def _qty_float(value: Any) -> float:
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0


class ShadowQuotingEngine:
    """Paper-only quoting engine: prices RFQs, stores drafts, submits nothing.

    ``pricer`` / ``risk`` / ``inventory`` are injectable seams (defaults:
    :class:`V1NaivePricer`, :class:`ConservativeRiskCheck`, empty
    :class:`InventoryState`). ``params_version`` defaults to
    ``config.params_version`` (``"unversioned"`` unless configured).
    """

    def __init__(
        self,
        store: EventStore,
        books: LegBookCache,
        reference: ReferenceCache,
        config: PipelineConfig,
        *,
        pricer: Optional[Pricer] = None,
        risk: Optional[RiskCheck] = None,
        params_version: Optional[str] = None,
        inventory: Optional[InventoryState] = None,
        inventory_provider: Optional[Callable[..., InventoryState]] = None,
        game_resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        if config.paper_mode is not True:
            raise PaperModeError(
                "ShadowQuotingEngine refuses to run with paper_mode=False: "
                "no live-trading scaffolding exists in this repo."
            )
        self._store = store
        self._books = books
        self._reference = reference
        self._config = config
        # Snapshot the paper-mode flag: the live reference above is mutable,
        # so the guard below re-checks the snapshot on every call.
        self._paper_mode = config.paper_mode
        self._pricer = pricer if pricer is not None else V1NaivePricer()
        self._risk = (
            risk
            if risk is not None
            else (InventoryRiskCheck(config.risk) if config.risk.policy == "inventory"
                  else ConservativeRiskCheck(
                max_per_rfq_notional=config.max_per_rfq_notional,
                max_per_game_notional=config.max_per_game_notional,
                initial_capital=config.initial_capital,
            ))
        )
        self._params_version = (
            params_version if params_version is not None
            else config.params_version
        )
        self._inventory = inventory
        self._inventory_provider = (inventory_provider if inventory_provider is not None
                                    else InventoryProvider(store, capital=config.initial_capital,
                                                           game_resolver=game_resolver))
        self._game_resolver = game_resolver

    # -- main entry ------------------------------------------------------
    def maybe_quote(self, event: NormalizedEvent) -> Optional[DraftQuote]:
        """Run the paper-trading loop for one event.

        Returns the stored :class:`DraftQuote` on success, None when the
        RFQ is skipped or declined. Every path records exactly one
        ``shadow_decisions`` row (except internal event types, which are
        ignored silently). Exchange time only; fully deterministic.
        """
        if self._paper_mode is not True:
            raise PaperModeError(
                "ShadowQuotingEngine.paper_mode was disabled after "
                "construction; refusing to quote."
            )
        rfq = self._store.get_rfq(event.rfq_id) if event.rfq_id else None
        now_ms = _iso_to_ms(event.event_at)

        # (1) eligibility ------------------------------------------------
        elig = check_eligibility(
            event, rfq, now_ms=now_ms,
            stale_rfq_ms=self._config.stale_rfq_ms,
            reference=self._reference,
        )
        if not elig.eligible:
            if elig.skip_reason is None:
                return None  # internal event type: ignore, record nothing
            self._record_decision(
                event, rfq_id=event.rfq_id or "",
                decision=elig.skip_reason,
                reason=f"engine={ENGINE_VERSION}",
            )
            log.info("shadow skip: rfq=%s reason=%s",
                     event.rfq_id, elig.skip_reason)
            return None
        assert rfq is not None

        # (2) leg inputs (raw wire settlements -> pricer resolved_price) --
        inputs = self._leg_inputs(rfq, now_ms)

        # (3) price ------------------------------------------------------
        result = self._pricer.price(
            inputs,
            rfq_id=event.rfq_id or "",
            rfq_status=rfq["status"],
            qty_decimal=(str(rfq["qty_decimal"])
                         if rfq["qty_decimal"] is not None else None),
            cash_order_qty=(str(rfq["cash_order_qty"])
                            if rfq["cash_order_qty"] is not None else None),
            params_version=self._params_version,
            decided_at=event.event_at,
            config=self._config,
        )
        if not result.quotable:
            self._record_decision(
                event, rfq_id=result.rfq_id,
                decision=result.unquotable_reason or "UNKNOWN_DECLINE",
                reason=f"model={result.model_version}",
                fair_price=result.fair_value,
                components=result.extra.get("components") or {},
            )
            log.info("shadow decline: rfq=%s reason=%s",
                     result.rfq_id, result.unquotable_reason)
            return None

        # (4) risk -------------------------------------------------------
        notional = self._notional(result)
        inventory = (self._inventory if self._inventory is not None else
                     self._inventory_provider(event.event_at))
        game_key, game_source = self._game_key(rfq, result)
        result.extra.setdefault("symbol", rfq.get("symbol") or game_key)
        result.extra.setdefault("markets", tuple(str(leg["symbol"])
                                                  for leg in rfq.get("legs", [])))
        nfl_legs = [str(leg.get("symbol", "")) for leg in rfq.get("legs", [])
                    if str(leg.get("symbol", "")).startswith("NFL-")]
        if nfl_legs:
            result.extra.setdefault("teams", tuple(nfl_legs[0].split("-")[3:5]))
        verdict = (RiskVerdict(False, "0", "0", RISK_KILL_SWITCH,
                               {"game_key": game_key}, action="reject")
                   if inventory.kill_switch or self._config.risk.risk_halt
                   else self._risk.check(result, notional, inventory, game_key))
        verdict.detail.setdefault("game_key_source", game_source)
        self._store.record_risk_event(ts=event.event_at, rfq_id=result.rfq_id,
                                      quote_id="", game_id=game_key, verdict=verdict)
        if not verdict.ok:
            self._record_decision(
                event, rfq_id=result.rfq_id, decision=verdict.reason,
                reason=f"model={result.model_version}",
                fair_price=result.fair_value,
                components=result.extra.get("components") or {},
            )
            log.info("shadow risk reject: rfq=%s reason=%s",
                     result.rfq_id, verdict.reason)
            return None

        # (5) draft quote ------------------------------------------------
        draft = self._build_draft(event, rfq, result, verdict, inputs,
                                  notional, inventory)

        # (6) persist ----------------------------------------------------
        self._store.record_shadow_draft(
            quote_id=draft.quote_id,
            rfq_id=draft.rfq_id,
            symbol=rfq.get("symbol"),
            fair=draft.fair,
            buy_price=draft.buy_price,
            sell_price=draft.sell_price,
            buy_qty=draft.buy_qty,
            sell_qty=draft.sell_qty,
            expected_edge_bps=draft.expected_edge_bps,
            model_version=draft.model_version,
            params_version=draft.params_version,
            input_snapshot_json=draft.input_snapshot_json,
            decided_by=draft.decided_by,
            decided_at=draft.decided_at,
        )
        if hasattr(self._inventory_provider, "record") and self._inventory is None:
            self._inventory_provider.record(event.event_at, f"draft:{draft.quote_id}")
        extra = result.extra
        self._record_decision(
            event, rfq_id=draft.rfq_id, decision=QUOTED_OK,
            reason=f"model={result.model_version}",
            fair_price=draft.fair,
            buy_price=draft.buy_price,
            sell_price=draft.sell_price,
            spread_bps=extra.get("spread_bps_total"),
            expected_edge_bps=draft.expected_edge_bps,
            buy_qty=draft.buy_qty,
            sell_qty=draft.sell_qty,
            components=extra.get("components") or {},
        )

        # (7) paper mode: the outbound RPC is logged, never sent. --------
        log.info(
            "PAPER MODE: outbound quote RPC for rfq=%s NOT SENT "
            "(quote_id=%s fair=%.4f buy=%.3f sell=%.3f reason=%s)",
            draft.rfq_id, draft.quote_id, draft.fair,
            draft.buy_price, draft.sell_price, QUOTED_OK,
        )
        return draft

    # -- helpers ----------------------------------------------------------
    def _game_key(self, rfq: Dict[str, Any], result: PricerResult) -> tuple[str, str]:
        explicit = result.extra.get("game_id")
        if explicit:
            return str(explicit), "pricer"
        per_game = result.extra.get("per_game")
        if isinstance(per_game, dict) and len(per_game) == 1:
            return str(next(iter(per_game))), "pricer"
        if self._game_resolver:
            games = {self._game_resolver(str(leg["symbol"]))
                     for leg in rfq.get("legs", [])}
            games.discard(None)
            if len(games) == 1:
                return str(next(iter(games))), "registry"
        heads = {"-".join(str(leg["symbol"]).split("-")[:5])
                 for leg in rfq.get("legs", [])
                 if str(leg.get("symbol", "")).startswith("NFL-")}
        if len(heads) == 1:
            return next(iter(heads)), "leg_symbols"
        return str(rfq.get("symbol") or ""), "combo_symbol_fallback"

    def _leg_inputs(self, rfq: Dict[str, Any],
                    now_ms: int) -> List[LegMarkInput]:
        """Build pricer inputs from the RFQ's legs and book snapshots."""
        legs = list(rfq["legs"] or [])
        if not legs and rfq["symbol"]:
            meta = self._reference.get(rfq["symbol"])
            if meta is not None:
                legs = [
                    {"symbol": l["symbol"], "side": l.get("side", "YES"),
                     "settlement_price": None}
                    for l in meta.legs
                ]
        snapshots = self._books.get([l["symbol"] for l in legs],
                                    now_ms=now_ms)
        inputs: List[LegMarkInput] = []
        for l in legs:
            snap = snapshots[l["symbol"]]
            # Raw wire settlement -> pricer resolved_price. The pricer
            # applies the YES/NO inversion when computing q_i; nothing is
            # inverted here.
            inputs.append(LegMarkInput(
                symbol=l["symbol"],
                side=l.get("side") or "YES",
                bid=snap.bid,
                ask=snap.ask,
                bid_size=snap.bid_size,
                ask_size=snap.ask_size,
                stale=snap.stale or snap.missing,
                resolved_price=(float(l["settlement_price"])
                                if l.get("settlement_price") is not None
                                else None),
            ))
        return inputs

    def _notional(self, result: PricerResult) -> float:
        """Worst-case fill notional of the draft: fair x largest side qty."""
        extra = result.extra
        qty = max(_qty_float(extra.get("buy_qty")),
                  _qty_float(extra.get("sell_qty")))
        return result.fair_value * qty

    def _build_draft(self, event: NormalizedEvent, rfq: Dict[str, Any],
                     result: PricerResult, verdict: Any,
                     inputs: List[LegMarkInput],
                     notional: float, inventory: InventoryState) -> DraftQuote:
        extra = result.extra
        # Deterministic quote id: per-RFQ draft sequence. Identical across
        # replays because the count only depends on this run's own drafts.
        n = self._store.count_shadow_quotes(event.rfq_id or "") + 1
        quote_id = f"shdw-{event.rfq_id}-{n}"
        cfg = self._config
        snapshot = {
            "quote_id": quote_id,
            "rfq_id": event.rfq_id,
            "combo_symbol": rfq.get("symbol"),
            "leg_inputs": [asdict(i) for i in inputs],
            "leg_marks": (extra.get("components") or {}).get("leg_marks", []),
            "fair_value": result.fair_value,
            "marginals": result.marginals,
            "naive_product": result.naive_product,
            "corr_adjustment_bps": result.corr_adjustment_bps,
            "confidence": result.confidence,
            "buy_price": (verdict.adjusted_buy_price if verdict.adjusted_buy_price is not None
                          else extra.get("buy_price")),
            "sell_price": (verdict.adjusted_sell_price if verdict.adjusted_sell_price is not None
                           else extra.get("sell_price")),
            "requested_buy_qty": extra.get("buy_qty"),
            "requested_sell_qty": extra.get("sell_qty"),
            "buy_qty": verdict.adjusted_buy_qty,
            "sell_qty": verdict.adjusted_sell_qty,
            "risk_reason": verdict.reason,
            "requested_notional": notional,
            "risk_verdict_detail": verdict.detail,
            "risk_action": verdict.action,
            "risk_flags": verdict.flags,
            "exposure_before": verdict.exposure_before,
            "exposure_after": verdict.exposure_after,
            "model_version": result.model_version,
            "params_version": result.params_version,
            "inventory": inventory.to_snapshot(),
            "spread_knobs": {
                "base_edge_bps": cfg.base_edge_bps,
                "uncertainty_per_leg_bps": cfg.uncertainty_per_leg_bps,
                "width_weight": cfg.width_weight,
                "depth_slope_bps": cfg.depth_slope_bps,
                "event_risk_bps": cfg.event_risk_bps,
                "operational_buffer_bps": cfg.operational_buffer_bps,
                "tick_size": cfg.tick_size,
                "price_min": cfg.price_min,
                "price_max": cfg.price_max,
                "min_qty": cfg.min_qty,
            },
            "risk_knobs": {
                "max_per_rfq_notional": cfg.max_per_rfq_notional,
                "max_per_game_notional": cfg.max_per_game_notional,
                "initial_capital": cfg.initial_capital,
            },
            "decided_at": event.event_at,
            "decided_by": DECIDED_BY,
            "engine_version": ENGINE_VERSION,
        }
        input_snapshot_json = json.dumps(snapshot, sort_keys=True,
                                         default=str)
        return DraftQuote(
            quote_id=quote_id,
            rfq_id=event.rfq_id or "",
            status="shadow",
            origin="shadow",
            fair=result.fair_value,
            buy_price=float(verdict.adjusted_buy_price if verdict.adjusted_buy_price is not None
                            else extra.get("buy_price") or 0.0),
            sell_price=float(verdict.adjusted_sell_price if verdict.adjusted_sell_price is not None
                             else extra.get("sell_price") or 0.0),
            buy_qty=verdict.adjusted_buy_qty,
            sell_qty=verdict.adjusted_sell_qty,
            expected_edge_bps=float(extra.get("expected_edge_bps") or 0.0),
            model_version=result.model_version,
            params_version=result.params_version,
            input_snapshot_json=input_snapshot_json,
            decided_at=event.event_at,
        )

    def _record_decision(self, event: NormalizedEvent, *, rfq_id: str,
                         decision: str, reason: str = "",
                         fair_price: Optional[float] = None,
                         buy_price: Optional[float] = None,
                         sell_price: Optional[float] = None,
                         spread_bps: Optional[float] = None,
                         expected_edge_bps: Optional[float] = None,
                         buy_qty: Optional[str] = None,
                         sell_qty: Optional[str] = None,
                         components: Optional[Dict[str, Any]] = None) -> None:
        # Exchange time only: ts is the event's exchange timestamp.
        self._store.record_shadow_decision(
            rfq_id=rfq_id,
            decision=decision,
            reason=reason,
            fair_price=fair_price,
            buy_price=buy_price,
            sell_price=sell_price,
            spread_bps=spread_bps,
            expected_edge_bps=expected_edge_bps,
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            components_json=(json.dumps(components, sort_keys=True,
                                        default=str)
                             if components is not None else None),
            ts=event.event_at,
        )

"""Pricer seam for the paper shadow engine.

The headless NFL capture uses :class:`combo_mm.nfl.live_pricer.NflLivePricer`
because its position IDs require catalog and live book lookups. This V1
adapter remains useful for offline replay and non-NFL RFQs.

:class:`V1NaivePricer` is the current implementation: it adapts the
existing independent-leg :func:`combo_mm.pricing.price_combo` (fair =
product of marginal leg probabilities, ``corr_adjustment_bps = 0.0``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from combo_mm.config import PipelineConfig
from combo_mm.pricing import QUOTED_OK, LegMarkInput, QuoteDecision, price_combo

__all__ = ["PricerResult", "Pricer", "V1NaivePricer", "MODEL_VERSION",
           "SAME_GAME_NESTED", "SAME_GAME_TOO_LARGE", "SAME_GAME_UNMODELED",
           "UNRESOLVED_LEG"]

MODEL_VERSION = "v1"
SAME_GAME_NESTED = "SAME_GAME_NESTED"
SAME_GAME_TOO_LARGE = "SAME_GAME_TOO_LARGE"
SAME_GAME_UNMODELED = "SAME_GAME_UNMODELED"
UNRESOLVED_LEG = "UNRESOLVED_LEG"


@dataclass(frozen=True)
class PricerResult:
    """Structured pricing outcome produced by any :class:`Pricer`.

    ``unquotable_reason`` is None when the RFQ is quotable and carries the
    decline reason code otherwise. ``extra`` carries pricer-specific quote
    terms the engine needs to build the two-sided draft (buy/sell prices,
    qtys, expected edge, spread components) without re-running the pricer;
    V1 fills it from the adapted :class:`QuoteDecision`, the MVN pricer
    will fill it from its own computation.
    """

    rfq_id: str
    model_version: str
    params_version: str
    fair_value: float
    marginals: Dict[str, float]          # symbol -> q_i (marginal win prob)
    naive_product: float                 # product of marginals (V1 fair)
    corr_adjustment_bps: float            # 0.0 for V1; joint-model delta for MVN
    confidence: float                    # 0..1, pricer-defined heuristic
    unquotable_reason: Optional[str]     # None when quotable
    legs_snapshot_hash: str
    decided_at: str                      # exchange time, never wall clock
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def quotable(self) -> bool:
        return self.unquotable_reason is None


class Pricer(Protocol):
    """Every combo pricer implements this interface."""

    model_version: str

    def price(
        self,
        legs: List[LegMarkInput],
        *,
        rfq_id: str,
        rfq_status: str = "OPEN",
        qty_decimal: Optional[str] = None,
        cash_order_qty: Optional[str] = None,
        params_version: str = "unversioned",
        decided_at: str = "",
        config: Optional[PipelineConfig] = None,
    ) -> PricerResult:
        """Price one combo RFQ. Pure function -- no I/O."""
        ...


def _v1_confidence(decision: QuoteDecision) -> float:
    """Simple documented confidence heuristic for the V1 pricer.

    Starts at 1.0 and decays with the summed leg touch spread (in bps)
    and with leg count: a tight two-leg book scores near 1, a wide or
    many-legged book scores lower. Declined RFQs score 0.0. This is a
    placeholder until the MVN pricer (#2) supplies a model-based
    confidence.
    """
    if decision.reason_code != QUOTED_OK:
        return 0.0
    leg_marks = decision.components.get("leg_marks", [])
    spread_bps = sum(float(m.get("spread_bps", 0.0)) for m in leg_marks)
    n_legs = max(len(leg_marks), 1)
    confidence = 1.0 - spread_bps / 5000.0 - 0.05 * (n_legs - 1)
    return max(0.0, min(1.0, confidence))


class V1NaivePricer:
    """Independent-leg pricer with a catalog-backed same-game guardrail."""

    model_version: str = MODEL_VERSION

    def __init__(self, resolver: Optional[Callable[[str], Any]] = None,
                 same_game_haircut_bps: float = 150.0,
                 same_game_max_qty: Optional[float] = None) -> None:
        if same_game_haircut_bps < 0 or (same_game_max_qty is not None and same_game_max_qty <= 0):
            raise ValueError("invalid same-game guardrail")
        self.resolver = resolver
        self.same_game_haircut_bps = same_game_haircut_bps
        self.same_game_max_qty = same_game_max_qty

    def price(
        self,
        legs: List[LegMarkInput],
        *,
        rfq_id: str,
        rfq_status: str = "OPEN",
        qty_decimal: Optional[str] = None,
        cash_order_qty: Optional[str] = None,
        params_version: str = "unversioned",
        decided_at: str = "",
        config: Optional[PipelineConfig] = None,
    ) -> PricerResult:
        cfg = config or PipelineConfig()
        guard_reason = None
        same_games: Dict[str, List[Any]] = {}
        if self.resolver is not None:
            for leg in legs:
                market = self.resolver(leg.symbol)
                if market is None:
                    guard_reason = UNRESOLVED_LEG
                    break
                if market.game:
                    same_games.setdefault(market.game, []).append(market)
            if guard_reason is None:
                for members in same_games.values():
                    if len(members) < 2:
                        continue
                    if any(not m.is_nfl for m in members):
                        guard_reason = SAME_GAME_UNMODELED
                        break
                    # ML and spread are nested or nearly contradictory on the
                    # same score margin. A fixed haircut cannot bound that error.
                    from combo_mm.nfl.catalog_markets import parse_catalog_leg
                    parsed_markets = [parse_catalog_leg(m.slug, m.outcome_index) for m in members]
                    if any(parsed is None for parsed in parsed_markets):
                        guard_reason = SAME_GAME_UNMODELED
                        break
                    kinds = {parsed[0].kind for parsed in parsed_markets}
                    if "ML" in kinds and "SPR" in kinds:
                        guard_reason = SAME_GAME_NESTED
                        break
                if guard_reason is None and any(len(m) >= 2 for m in same_games.values()):
                    if self.same_game_max_qty is not None and qty_decimal is not None:
                        if float(qty_decimal) > self.same_game_max_qty:
                            guard_reason = SAME_GAME_TOO_LARGE
        guarded = any(len(m) >= 2 for m in same_games.values())
        decision = price_combo(
            legs,
            rfq_id=rfq_id,
            rfq_status=rfq_status,
            qty_decimal=qty_decimal,
            cash_order_qty=cash_order_qty,
            model_version=self.model_version,
            decided_at=decided_at,
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
            extra_spread_bps={"same_game_haircut_bps": self.same_game_haircut_bps}
            if guarded and guard_reason is None else None,
        )
        quoted = decision.reason_code == QUOTED_OK and guard_reason is None
        marginals = {
            m["symbol"]: float(m["q"])
            for m in decision.components.get("leg_marks", [])
            if "symbol" in m and "q" in m
        }
        return PricerResult(
            rfq_id=rfq_id,
            model_version=self.model_version,
            params_version=params_version,
            fair_value=decision.fair,
            marginals=marginals,
            naive_product=decision.fair,   # V1: fair IS the naive product
            corr_adjustment_bps=0.0,       # V1 assumes independent legs
            confidence=_v1_confidence(decision),
            unquotable_reason=None if quoted else (guard_reason or decision.reason_code),
            legs_snapshot_hash=decision.legs_snapshot_hash,
            decided_at=decided_at,
            extra={
                "buy_price": decision.buy_price,
                "sell_price": decision.sell_price,
                "buy_qty": decision.buy_qty,
                "sell_qty": decision.sell_qty,
                "half_spread": decision.half_spread,
                "expected_edge_bps": decision.expected_edge_bps,
                "spread_bps_total": decision.components.get("spread_bps_total"),
                "components": decision.components,
            },
        )

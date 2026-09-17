"""NFL same-game joint pricer behind the :class:`combo_mm.pricer.Pricer` seam.

Fair value is the bivariate-normal score model's joint probability of the
legs (:mod:`combo_mm.nfl.joint`) instead of the product of marginals. Spread,
tick rounding, sizing and every decline check are reused from
:func:`combo_mm.pricing.price_combo` via ``fair_override``, so the quote terms
are directly comparable with the V1 naive pricer on the same books.

Leg symbols are mapped to a game model and a canonical score leg with
:meth:`NflJointPricer.register_game` (the weekly backtest does this from the
closing lines; live Polymarket symbol mapping is not wired yet). An RFQ is
declined when a leg is unmapped (``UNMODELED_LEG``), a leg is a NO side
(``UNMODELED_LEG``), or its legs span more than one game (``CROSS_GAME``).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from combo_mm.config import PipelineConfig
from combo_mm.nfl import MODEL_VERSION as NFL_MODEL_VERSION
from combo_mm.nfl.joint import GameModel, Leg
from combo_mm.pricer import PricerResult, _v1_confidence
from combo_mm.pricing import QUOTED_OK, LegMarkInput, price_combo

__all__ = ["NflJointPricer", "UNMODELED_LEG", "CROSS_GAME", "MODEL_VERSION"]

MODEL_VERSION = f"{NFL_MODEL_VERSION}-joint"
UNMODELED_LEG = "UNMODELED_LEG"
CROSS_GAME = "CROSS_GAME"


class NflJointPricer:
    """Correlation-aware same-game pricer. Pure: no I/O after registration."""

    model_version: str = MODEL_VERSION

    def __init__(self) -> None:
        self._models: Dict[str, GameModel] = {}
        self._legs: Dict[str, Tuple[str, Leg]] = {}   # symbol -> (game_id, leg)

    def register_game(self, game_id: str, model: GameModel, legs: Dict[str, Leg]) -> None:
        """Map leg symbols (YES side) to ``model``'s canonical score legs."""
        self._models[game_id] = model
        for symbol, leg in legs.items():
            self._legs[symbol] = (game_id, leg)

    def joint(self, symbols: List[str]) -> Tuple[Optional[float], Optional[str]]:
        """Model joint probability of YES on every symbol, or (None, decline code)."""
        mapped = [self._legs.get(s) for s in symbols]
        if not mapped or any(m is None for m in mapped):
            return None, UNMODELED_LEG
        games = {m[0] for m in mapped}  # type: ignore[index]
        if len(games) != 1:
            return None, CROSS_GAME
        model = self._models[games.pop()]
        return model.joint([m[1] for m in mapped]), None  # type: ignore[index]

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
        if any(leg.side != "YES" for leg in legs):
            fair_override, reason = None, UNMODELED_LEG
        else:
            fair_override, reason = self.joint([leg.symbol for leg in legs])
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
            fair_override=fair_override,
        )
        components = dict(decision.components)
        naive = float(components.get("naive_fair", decision.fair))
        if reason is None and decision.reason_code == QUOTED_OK:
            components["correlation_adjustment_bps"] = (decision.fair - naive) * 10000.0
            unquotable = None
        else:
            unquotable = reason or decision.reason_code
            components.setdefault("decline_detail", unquotable)
        quoted = unquotable is None
        marginals = {
            m["symbol"]: float(m["q"])
            for m in components.get("leg_marks", [])
            if "symbol" in m and "q" in m
        }
        return PricerResult(
            rfq_id=rfq_id,
            model_version=self.model_version,
            params_version=params_version,
            fair_value=decision.fair if quoted else 0.0,
            marginals=marginals,
            naive_product=naive,
            corr_adjustment_bps=(decision.fair - naive) * 10000.0 if quoted else 0.0,
            confidence=_v1_confidence(decision) if quoted else 0.0,
            unquotable_reason=unquotable,
            legs_snapshot_hash=decision.legs_snapshot_hash,
            decided_at=decided_at,
            extra={
                "buy_price": decision.buy_price if quoted else 0.0,
                "sell_price": decision.sell_price if quoted else 0.0,
                "buy_qty": decision.buy_qty if quoted else "0",
                "sell_qty": decision.sell_qty if quoted else "0",
                "half_spread": decision.half_spread,
                "expected_edge_bps": decision.expected_edge_bps if quoted else 0.0,
                "spread_bps_total": components.get("spread_bps_total"),
                "components": components,
            },
        )

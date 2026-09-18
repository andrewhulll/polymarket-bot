"""NFL-model adapter for the shadow engine's ``Pricer`` seam (issue #2 follow-up).

:class:`NflPricerAdapter` implements the :class:`combo_mm.pricer.Pricer`
protocol and wraps :class:`combo_mm.nfl.live_pricer.NflLivePricer`, so the
:class:`~combo_mm.engine.ShadowQuotingEngine` prices live RFQs with the NFL
correlation model instead of the naive independent-leg product.

Translation notes:

- The engine's ``legs`` are gateway position ids (``LegMarkInput.symbol``),
  exactly what the live pricer's catalog resolves, so the adapter rebuilds a
  :class:`LiveRfq` straight from them. The combo side comes from the first
  leg's side (gateway legs inherit the combo side); direction is fixed to
  ``"BUY"`` because the shadow engine quotes both sides and never uses the
  requester's direction.
- The adapter's book source is the engine's already-populated in-memory
  :class:`~combo_mm.books.LegBookCache` -- no network reads. The pricer asks
  for books by ``LegRef(market_id, outcome_index)`` while the cache is keyed
  by position id, so the adapter rebuilds the ref->position index from the
  catalog before each ``price()`` call. Kickoff metadata is not in the
  cache, so ``kickoff()`` returns ``None`` (no ``GAME_STARTED`` declines on
  this path; the engine's own staleness eligibility still applies).
- ``decided_at`` (exchange time, never wall clock) is the pricer's ``now``,
  keeping replay deterministic.
- Fallback: when the model cannot run but a naive quote is still meaningful
  (missing/stale params, unresolvable legs, missing calibration, internal
  error, or a cross-game RFQ the model has nothing to add to), the adapter
  delegates to :class:`V1NaivePricer` and tags the result
  ``extra["pricer_fallback"] = True`` so model coverage stays measurable.
  Structural declines (game started, contradictory legs, model/market
  disagreement, low confidence, unmodeled same-game correlation, latency or
  deadline overruns) are passed through as declines.
- Latency: catalog + params are pre-warmed at construction and the pricer's
  per-game calibration cache amortizes repeat games. A quote whose measured
  ``latency_ms`` exceeds the budget (default: the pipeline's
  ``quote_latency_budget_ms``) is recorded as a ``QUOTE_LATENCY_EXCEEDED``
  decline rather than emitted as a stale draft.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from combo_mm.books import LegBookCache
from combo_mm.config import PipelineConfig
from combo_mm.leg_books import LegBook, LegRef
from combo_mm.nfl.live_pricer import (
    CALIBRATION_FAILED,
    CONTRADICTORY_LEGS,
    GAME_STARTED,
    LOW_CONFIDENCE,
    MISSING_CALIBRATION_MARKET,
    MODEL_MARKET_DISAGREE,
    MODEL_VERSION as NFL_MODEL_VERSION,
    NO_NFL_SAME_GAME,
    NO_QUOTABLE_SIDE,
    OTHER_SAME_GAME,
    PARAMS_STALE,
    PARAMS_UNAVAILABLE,
    PRICER_ERROR,
    QUOTE_DEADLINE_EXCEEDED,
    QUOTE_LATENCY_EXCEEDED,
    UNRESOLVED_LEG,
    UNSUPPORTED_LEG,
    LiveQuote,
    LiveRfq,
    NflLivePricer,
    NflLivePricerConfig,
)
from combo_mm.nfl.params_provider import ParamsProvider
from combo_mm.pricer import PricerResult, V1NaivePricer
from combo_mm.pricing import LegMarkInput

log = logging.getLogger(__name__)

__all__ = ["NflPricerAdapter", "build_nfl_adapter", "FALLBACK_CODES"]

#: Model decline codes that degrade to the naive pricer instead of declining.
#: The model is unavailable or has nothing to add, but a naive quote is still
#: meaningful. Everything else is a structural reason not to quote.
FALLBACK_CODES = frozenset({
    PARAMS_UNAVAILABLE,
    PARAMS_STALE,
    UNRESOLVED_LEG,
    MISSING_CALIBRATION_MARKET,
    CALIBRATION_FAILED,
    PRICER_ERROR,
    NO_NFL_SAME_GAME,  # cross-game combos: independence is exactly right
})

#: Structural declines are passed through, never replaced by a naive quote.
DECLINE_CODES = frozenset({
    GAME_STARTED,
    CONTRADICTORY_LEGS,
    MODEL_MARKET_DISAGREE,
    LOW_CONFIDENCE,
    NO_QUOTABLE_SIDE,
    QUOTE_DEADLINE_EXCEEDED,
    QUOTE_LATENCY_EXCEEDED,
    OTHER_SAME_GAME,   # non-NFL same-game legs: unmodeled correlation
    UNSUPPORTED_LEG,   # e.g. 1H lines / player props on a modeled game
})

_EPS = 1e-9


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _snapshot_hash(legs: List[LegMarkInput]) -> str:
    leg_inputs = [
        {
            "symbol": leg.symbol,
            "side": leg.side,
            "bid": leg.bid,
            "ask": leg.ask,
            "bid_size": leg.bid_size,
            "ask_size": leg.ask_size,
            "stale": leg.stale,
            "resolved_price": leg.resolved_price,
        }
        for leg in legs
    ]
    return hashlib.sha256(
        json.dumps(leg_inputs, sort_keys=True, default=str).encode()
    ).hexdigest()


def _combo_side(legs: List[LegMarkInput]) -> str:
    """Gateway legs inherit the combo side; the first leg's side is the combo's."""
    side = str((legs[0].side if legs else None) or "YES").upper()
    return side if side in ("YES", "NO") else "YES"


class _CacheBookSource:
    """A :class:`LegBookSource` over the engine's in-memory :class:`LegBookCache`.

    The pricer looks books up by ``LegRef(market_id, outcome_index)``; the
    cache is keyed by position id (symbol). The adapter refreshes the
    ref->position index before each ``price()`` call via :meth:`set_index`.
    Synchronous, pure in-memory, never raises -- unknown refs come back
    ``None`` and the pricer declines on the missing book.
    """

    def __init__(self, cache: LegBookCache) -> None:
        self._cache = cache
        self._index: Dict[LegRef, str] = {}
        self._lock = threading.Lock()

    def set_index(self, index: Dict[LegRef, str]) -> None:
        with self._lock:
            self._index = dict(index)

    def books(self, legs: Sequence[LegRef]) -> Dict[LegRef, Optional[LegBook]]:
        with self._lock:
            symbols = [self._index.get(ref) for ref in legs]
        wanted = sorted({s for s in symbols if s is not None})
        snaps = self._cache.get(wanted) if wanted else {}
        out: Dict[LegRef, Optional[LegBook]] = {}
        for ref, symbol in zip(legs, symbols):
            snap = snaps.get(symbol) if symbol is not None else None
            if snap is None or snap.missing:
                out[ref] = None
                continue
            out[ref] = LegBook(
                bid=snap.bid, ask=snap.ask,
                bid_size=snap.bid_size, ask_size=snap.ask_size,
                ts_ms=snap.updated_ms, source="cache",
            )
        return out

    def kickoff(self, market_id: str) -> Optional[str]:
        # The in-memory cache carries no kickoff metadata.
        return None


class NflPricerAdapter:
    """A :class:`Pricer` that prices with the NFL correlation model.

    Wraps :class:`NflLivePricer`; see the module docstring for the translation,
    fallback, and latency rules.
    """

    model_version: str = NFL_MODEL_VERSION

    def __init__(
        self,
        catalog: Any,
        cache: LegBookCache,
        params: ParamsProvider,
        *,
        config: Optional[PipelineConfig] = None,
        model_config: Optional[NflLivePricerConfig] = None,
        latency_budget_ms: Optional[float] = None,
        fallback_resolver: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.catalog = catalog
        self.params = params
        self.config = config or PipelineConfig(paper_mode=True)
        if latency_budget_ms is None:
            latency_budget_ms = float(self.config.quote_latency_budget_ms)
        if latency_budget_ms <= 0:
            raise ValueError("latency_budget_ms must be positive")
        self.latency_budget_ms = latency_budget_ms
        self._book_source = _CacheBookSource(cache)
        self._nfl = NflLivePricer(
            catalog, self._book_source, params,
            config=self.config, model_config=model_config,
        )
        self._fallback = V1NaivePricer(resolver=fallback_resolver)
        # Pre-warm: the lazy numpy/scipy import and the params read cost
        # seconds -- paying them here keeps them off any RFQ's clock.
        # A failed warmup never raises; price() degrades per-RFQ instead.
        try:
            self._nfl.warmup()
        except Exception as exc:  # noqa: BLE001 -- warmup must not break construction
            log.warning("NFL pricer warmup failed (%s); per-RFQ fallback applies",
                        type(exc).__name__)

    # -- Pricer protocol ------------------------------------------------------
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
        """Price one combo RFQ with the NFL model (pure: no I/O)."""
        now = _parse_iso(decided_at)
        position_ids = [leg.symbol for leg in legs]
        side = _combo_side(legs)
        self._refresh_index(position_ids)
        live_rfq = LiveRfq(
            rfq_id=rfq_id,
            leg_position_ids=tuple(position_ids),
            side=side,
            direction="BUY",  # the shadow engine quotes both sides; unused downstream
            qty_decimal=qty_decimal,
            cash_order_qty=cash_order_qty,
            received_at=decided_at or None,
        )
        quote = self._nfl.price(live_rfq, now=now)
        if quote.quoted and quote.latency_ms > self.latency_budget_ms:
            return self._decline(
                legs, rfq_id, QUOTE_LATENCY_EXCEEDED,
                f"model price took {quote.latency_ms:.0f}ms "
                f"(budget {self.latency_budget_ms:.0f}ms)",
                params_version, decided_at, quote=quote,
            )
        if quote.quoted:
            return self._to_result(quote, legs, side, params_version, decided_at)
        if quote.reason_code in FALLBACK_CODES:
            naive = self._fallback.price(
                legs, rfq_id=rfq_id, rfq_status=rfq_status,
                qty_decimal=qty_decimal, cash_order_qty=cash_order_qty,
                params_version=params_version, decided_at=decided_at,
                config=config,
            )
            return replace(naive, extra={
                **naive.extra,
                "pricer_fallback": True,
                "pricer_fallback_reason": quote.reason_code,
                "pricer_fallback_detail": quote.reason_detail,
            })
        return self._decline(
            legs, rfq_id, quote.reason_code, quote.reason_detail,
            params_version, decided_at, quote=quote,
        )

    # -- internals ------------------------------------------------------------
    def _refresh_index(self, position_ids: List[str]) -> None:
        """Rebuild the LegRef -> position-id index for this RFQ's games."""
        index: Dict[LegRef, str] = {}
        markets = self.catalog.resolve(position_ids)
        games = set()
        for pid, market in zip(position_ids, markets):
            if market is None:
                continue
            index[LegRef(market.market_id, market.outcome_index)] = pid
            if market.game:
                games.add(market.game)
        # Calibration markets (spread/total refs) come from the same games.
        for game in games:
            for market in self.catalog.markets_for_game(game):
                index.setdefault(
                    LegRef(market.market_id, market.outcome_index),
                    market.position_id,
                )
        self._book_source.set_index(index)

    def _to_result(self, quote: LiveQuote, legs: List[LegMarkInput], side: str,
                   params_version: str, decided_at: str) -> PricerResult:
        fair = quote.fair if quote.fair is not None else 0.0
        marginals: Dict[str, float] = {}
        leg_marks: List[Dict[str, Any]] = []
        for entry in quote.legs:
            q = entry.get("q_market")
            if q is None:
                continue
            q = float(q)
            if side == "NO":
                q = 1.0 - q
            pid = str(entry.get("position_id"))
            marginals[pid] = q
            leg_marks.append({
                "symbol": pid, "q": q,
                "bid": entry.get("bid"), "ask": entry.get("ask"),
                "book_source": entry.get("book_source"),
                "modeled": entry.get("modeled"),
            })
        half_spread = quote.components.get("half_spread")
        expected_edge_bps = (
            (float(half_spread) / max(fair, _EPS)) * 10000.0
            if half_spread else 0.0
        )
        return PricerResult(
            rfq_id=quote.rfq_id,
            model_version=quote.model_version,
            params_version=quote.params_version or params_version,
            fair_value=fair,
            marginals=marginals,
            naive_product=quote.naive if quote.naive is not None else 0.0,
            # YES-basis, matching LiveQuote and the pricing-tab display.
            corr_adjustment_bps=(quote.corr_adjustment_bps
                                 if quote.corr_adjustment_bps is not None else 0.0),
            confidence=quote.confidence,
            unquotable_reason=None,
            legs_snapshot_hash=_snapshot_hash(legs),
            decided_at=decided_at,
            extra={
                # Our offer / our bid on the requested side.
                "buy_price": quote.ask,
                "sell_price": quote.bid,
                "buy_qty": quote.ask_qty,
                "sell_qty": quote.bid_qty,
                "half_spread": half_spread,
                "expected_edge_bps": expected_edge_bps,
                "spread_bps_total": quote.spread_bps_total,
                "components": quote.components,
                "nfl_games": quote.games,
                "nfl_explanations": quote.explanations,
                "leg_marks": leg_marks,
                "nfl_latency_ms": quote.latency_ms,
            },
        )

    def _decline(self, legs: List[LegMarkInput], rfq_id: str, reason: str,
                 detail: str, params_version: str, decided_at: str, *,
                 quote: Optional[LiveQuote] = None) -> PricerResult:
        components: Dict[str, Any] = {}
        model_version = self.model_version
        if quote is not None:
            components = dict(quote.components)
            # The decline came from the inner pricer: attribute it there.
            if quote.model_version:
                model_version = quote.model_version
        return PricerResult(
            rfq_id=rfq_id,
            model_version=model_version,
            params_version=(quote.params_version if quote is not None
                            and quote.params_version else params_version),
            fair_value=(quote.fair if quote is not None and quote.fair is not None
                        else 0.0),
            marginals={},
            naive_product=(quote.naive if quote is not None and quote.naive is not None
                           else 0.0),
            corr_adjustment_bps=0.0,
            confidence=0.0,
            unquotable_reason=reason,
            legs_snapshot_hash=_snapshot_hash(legs),
            decided_at=decided_at,
            extra={
                "components": components,
                "decline_detail": detail,
                "nfl_explanations": quote.explanations if quote is not None else [],
                "nfl_latency_ms": quote.latency_ms if quote is not None else 0.0,
            },
        )


def build_nfl_adapter(
    catalog: Any,
    cache: LegBookCache,
    params_dir: Optional[Path | str] = None,
    *,
    config: Optional[PipelineConfig] = None,
    model_config: Optional[NflLivePricerConfig] = None,
    latency_budget_ms: Optional[float] = None,
    fallback_resolver: Optional[Callable[[str], Any]] = None,
) -> NflPricerAdapter:
    """Build the shadow-engine adapter over a shared book cache.

    ``params_dir`` defaults to the repo's ``params/`` (weekly files). A
    missing or unreadable params dir never raises: the adapter degrades
    per-RFQ to the naive fallback.
    """
    if params_dir is None:
        params_dir = Path(__file__).resolve().parents[2] / "params"
    return NflPricerAdapter(
        catalog, cache, ParamsProvider(params_dir),
        config=config, model_config=model_config,
        latency_budget_ms=latency_budget_ms,
        fallback_resolver=fallback_resolver,
    )

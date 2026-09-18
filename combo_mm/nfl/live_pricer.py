"""Live NFL same-game combo pricer: an RFQ in, the bid and ask we would show out (issue #2).

Pipeline for one live RFQ (gateway legs are combo-catalog position ids)::

    legs --catalog--> markets --catalog_markets/#15 registry--> score legs, by game
         --live books (CLOB / Gamma)--> market marginals q_i, per leg
    game's main spread + total books --calibrate_means--> (mu_home, mu_away)
    weekly params file --matchup_covariance--> Sigma          => GameModel
    GameModel --> model joint P_m(all), model marginals P_m(leg_i)
    fair = prod(q_i) * P_m(all) / prod P_m(leg_i)   ("market_lift", default)
         clamped to the Frechet bounds, times any independent legs
    spread = V1 stack (pricing.price_combo) + model-risk add-ons --> bid / ask

**Why market_lift.** It keeps each leg's own market price exactly and borrows
only the *dependence* from the model. The model's moneyline marginal misses
the market by ~2 points on average (docs/correlation-model.md §4.4 #2), so
pricing ML legs off the model's own marginal ("model_joint", also computed
and logged) would import that error into every ML combo.

**Scope.** At least one NFL game must contribute two or more legs (the
screen in :mod:`combo_mm.rfq_screen`). Every leg in such a block must be a
full-game ML / spread / total / team total; any other leg on that game (a
first-half spread, a player prop) is correlated with the block in a way we
do not model, so the RFQ is declined. Legs from other games are priced as
independent and multiplied in.

**Sides.** Gateway legs are the outcomes that must all hit for the combo's
YES; the RFQ's ``side`` picks YES or NO of that combo, so a NO RFQ is priced
at ``1 - fair_yes``. ``bid`` is the price we would buy the requested side
at, ``ask`` the price we would sell it at: a requester who wants to BUY
trades against our ask, one who wants to SELL against our bid.

Paper only: this module computes prices; nothing here sends a quote.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from combo_mm.combo_markets import LegMarket
from combo_mm.config import PipelineConfig
from combo_mm.leg_books import LegBook, LegRef
from combo_mm.nfl import MODEL_VERSION as NFL_MODEL_VERSION
from combo_mm.nfl.catalog_markets import describe_leg, parse_catalog_leg, unsupported_reason
from combo_mm.nfl.markets import ML, SPR, TOT, TT, NflLegMarket, to_joint_leg
from combo_mm.nfl.params_provider import ParamsHandle, ParamsProvider
from combo_mm.pricing import QUOTED_OK, LegMarkInput, _leg_mark, price_combo

__all__ = [
    "MODEL_VERSION",
    "LiveRfq",
    "LiveQuote",
    "NflLivePricerConfig",
    "NflLivePricer",
    # decline codes
    "UNRESOLVED_LEG",
    "NO_NFL_SAME_GAME",
    "OTHER_SAME_GAME",
    "UNSUPPORTED_LEG",
    "GAME_STARTED",
    "PARAMS_UNAVAILABLE",
    "PARAMS_STALE",
    "MISSING_CALIBRATION_MARKET",
    "CALIBRATION_FAILED",
    "CONTRADICTORY_LEGS",
    "MODEL_MARKET_DISAGREE",
    "LOW_CONFIDENCE",
    "NO_QUOTABLE_SIDE",
    "PRICER_ERROR",
    "QUOTE_DEADLINE_EXCEEDED",
    "QUOTE_LATENCY_EXCEEDED",
]

MODEL_VERSION = f"{NFL_MODEL_VERSION}-live"

UNRESOLVED_LEG = "UNRESOLVED_LEG"
NO_NFL_SAME_GAME = "NO_NFL_SAME_GAME"
OTHER_SAME_GAME = "OTHER_SAME_GAME"
UNSUPPORTED_LEG = "UNSUPPORTED_LEG"
GAME_STARTED = "GAME_STARTED"
PARAMS_UNAVAILABLE = "PARAMS_UNAVAILABLE"
PARAMS_STALE = "PARAMS_STALE"
MISSING_CALIBRATION_MARKET = "MISSING_CALIBRATION_MARKET"
CALIBRATION_FAILED = "CALIBRATION_FAILED"
CONTRADICTORY_LEGS = "CONTRADICTORY_LEGS"
MODEL_MARKET_DISAGREE = "MODEL_MARKET_DISAGREE"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
NO_QUOTABLE_SIDE = "NO_QUOTABLE_SIDE"
PRICER_ERROR = "PRICER_ERROR"
QUOTE_DEADLINE_EXCEEDED = "QUOTE_DEADLINE_EXCEEDED"
QUOTE_LATENCY_EXCEEDED = "QUOTE_LATENCY_EXCEEDED"

METHODS = ("market_lift", "model_joint")


class LegCatalog(Protocol):
    def resolve(self, position_ids: Sequence[str]) -> List[Optional[LegMarket]]: ...
    def markets_for_game(self, game: str) -> List[LegMarket]: ...


class LegBookSource(Protocol):
    def books(self, legs: Sequence[LegRef]) -> Dict[LegRef, Optional[LegBook]]: ...
    def kickoff(self, market_id: str) -> Optional[str]: ...


@dataclass(frozen=True)
class NflLivePricerConfig:
    """Model knobs on top of :class:`PipelineConfig`'s V1 spread stack (bps unless noted)."""

    method: str = "market_lift"
    corr_scale: float = 1.0
    max_book_age_s: float = 30.0          # leg book older than this -> stale leg
    max_params_age_days: float = 9.0      # older weekly params -> PARAMS_STALE
    max_marginal_gap: float = 0.04        # |P_model(leg) - q_market| above this -> decline
    gap_weight: float = 0.5               # marginal_gap_bps = weight * gap * 1e4
    corr_model_risk_kappa: float = 0.10   # corr_model_risk_bps = kappa * |fair - naive| * 1e4
    key_number_bps: float = 40.0
    big_favorite_bps: float = 30.0
    tail_bps_per_leg: float = 25.0        # per same-game leg beyond two
    params_age_bps_per_day: float = 5.0   # per day beyond 7
    min_confidence: float = 0.4
    calibration_candidates: int = 2       # main spread / total markets tried per game

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        for name in ("corr_scale", "max_book_age_s", "max_params_age_days", "max_marginal_gap",
                     "gap_weight", "corr_model_risk_kappa", "key_number_bps", "big_favorite_bps",
                     "tail_bps_per_leg", "params_age_bps_per_day", "min_confidence"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a non-negative number")
        if self.calibration_candidates < 1:
            raise ValueError("calibration_candidates must be >= 1")


@dataclass(frozen=True)
class LiveRfq:
    """The parts of a gateway RFQ the pricer needs."""

    rfq_id: str
    leg_position_ids: Tuple[str, ...]
    side: str = "YES"                     # combo side requested: YES | NO
    direction: str = "BUY"                # requester's direction: BUY | SELL
    qty_decimal: Optional[str] = None     # shares
    cash_order_qty: Optional[str] = None  # notional
    submission_deadline_ms: Optional[int] = None
    received_at: Optional[str] = None

    @property
    def size(self) -> Optional[str]:
        return self.qty_decimal if self.qty_decimal is not None else self.cash_order_qty

    @property
    def size_unit(self) -> str:
        return "shares" if self.qty_decimal is not None else "notional"


@dataclass
class LiveQuote:
    """One pricing outcome for a live RFQ: the bid/ask we would show, or why not."""

    rfq_id: str
    priced_at: str
    status: str                            # QUOTED | DECLINED
    reason_code: str                       # QUOTED_OK or a decline code
    reason_detail: str = ""
    side: str = "YES"
    direction: str = "BUY"
    size: Optional[str] = None
    size_unit: str = "shares"
    fair: Optional[float] = None           # requested side
    naive: Optional[float] = None          # independent-leg product, requested side
    fair_yes: Optional[float] = None
    naive_yes: Optional[float] = None
    model_joint_yes: Optional[float] = None
    corr_adjustment_bps: Optional[float] = None  # (fair_yes - naive_yes) * 1e4
    bid: Optional[float] = None            # we buy the requested side at
    ask: Optional[float] = None            # we sell the requested side at
    bid_qty: Optional[str] = None
    ask_qty: Optional[str] = None
    response_action: Optional[str] = None  # our side of the trade: SELL (vs a BUY RFQ) | BUY
    response_price: Optional[float] = None
    confidence: float = 0.0
    spread_bps_total: Optional[float] = None
    legs_label: str = ""
    model_version: str = MODEL_VERSION
    params_version: Optional[str] = None
    method: str = "market_lift"
    latency_ms: float = 0.0
    after_deadline: bool = False
    legs: List[Dict[str, Any]] = field(default_factory=list)
    games: List[Dict[str, Any]] = field(default_factory=list)
    components: Dict[str, Any] = field(default_factory=dict)
    explanations: List[str] = field(default_factory=list)

    @property
    def quoted(self) -> bool:
        return self.status == "QUOTED"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _near_key_number(line: float) -> bool:
    return any(abs(abs(line) - k) <= 0.5 for k in (3.0, 7.0))


class _Decline(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass
class _Leg:
    position_id: str
    market: LegMarket
    ref: LegRef
    label: str
    nfl: Optional[NflLegMarket] = None
    nfl_side: str = "YES"
    book: Optional[LegBook] = None
    q: Optional[float] = None
    p_model: Optional[float] = None

    @property
    def joint_leg(self):
        return to_joint_leg(self.nfl, self.nfl_side)


class NflLivePricer:
    """Prices live NFL same-game combo RFQs. Thread-safe; no outbound quotes."""

    CALIBRATION_CACHE = 256

    def __init__(self, catalog: LegCatalog, books: LegBookSource, params: ParamsProvider, *,
                 config: Optional[PipelineConfig] = None,
                 model_config: Optional[NflLivePricerConfig] = None) -> None:
        self.catalog = catalog
        self.book_source = books
        self.params = params
        self.config = config or PipelineConfig(paper_mode=True)
        self.model_config = model_config or NflLivePricerConfig()
        self._calibrations: "OrderedDict[tuple, Any]" = OrderedDict()
        # The quoter's worker and an on-demand price() can land here together.
        self._cache_lock = threading.Lock()

    # -- entry ----------------------------------------------------------------
    def warmup(self) -> None:
        """Pay the lazy numpy/scipy import and the params read before the first RFQ.

        Cold, those cost seconds -- more than an RFQ's quote window. The
        quoter calls this when it starts so live pricing is warm.
        """
        from combo_mm.nfl.joint import GameModel  # noqa: F401
        from combo_mm.nfl.params_io import matchup_covariance  # noqa: F401
        self.params.current()

    def price(self, rfq: LiveRfq, now: Optional[datetime] = None) -> LiveQuote:
        now = now or datetime.now(timezone.utc)
        started = time.perf_counter()
        quote = LiveQuote(rfq_id=rfq.rfq_id, priced_at=_iso(now), status="DECLINED",
                          reason_code=PRICER_ERROR, side=rfq.side, direction=rfq.direction,
                          size=rfq.size, size_unit=rfq.size_unit, method=self.model_config.method)
        if rfq.submission_deadline_ms is not None:
            quote.after_deadline = now.timestamp() * 1000 > rfq.submission_deadline_ms
        try:
            if quote.after_deadline:
                raise _Decline(QUOTE_DEADLINE_EXCEEDED, "RFQ deadline passed before pricing started")
            self._price(rfq, now, quote)
        except _Decline as d:
            quote.status, quote.reason_code, quote.reason_detail = "DECLINED", d.code, d.detail
            quote.bid = quote.ask = quote.response_price = None
        except Exception as exc:  # never let one RFQ kill the worker; the code says what happened
            quote.status, quote.reason_code = "DECLINED", PRICER_ERROR
            quote.reason_detail = f"{type(exc).__name__}: {exc}"[:300]
        quote.latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        if quote.quoted:
            if (rfq.submission_deadline_ms is not None
                    and now.timestamp() * 1000 + quote.latency_ms > rfq.submission_deadline_ms):
                quote.status, quote.reason_code = "DECLINED", QUOTE_DEADLINE_EXCEEDED
                quote.reason_detail = "pricing finished after the RFQ deadline"
                quote.after_deadline = True
            elif quote.latency_ms > self.config.quote_latency_budget_ms:
                quote.status, quote.reason_code = "DECLINED", QUOTE_LATENCY_EXCEEDED
                quote.reason_detail = (f"pricing took {quote.latency_ms:.2f} ms; "
                                       f"budget {self.config.quote_latency_budget_ms} ms")
            if not quote.quoted:
                quote.bid = quote.ask = quote.response_price = None
        return quote

    # -- pipeline -------------------------------------------------------------
    def _price(self, rfq: LiveRfq, now: datetime, quote: LiveQuote) -> None:
        mc = self.model_config
        if rfq.side not in ("YES", "NO") or rfq.direction not in ("BUY", "SELL"):
            raise _Decline(PRICER_ERROR, f"bad side/direction {rfq.side}/{rfq.direction}")
        markets = self.catalog.resolve(list(rfq.leg_position_ids))
        legs: List[_Leg] = []
        for pid, m in zip(rfq.leg_position_ids, markets):
            if m is None:
                raise _Decline(UNRESOLVED_LEG, f"position {pid[:16]}... not in the combo catalog yet")
            legs.append(_Leg(position_id=pid, market=m, ref=LegRef(m.market_id, m.outcome_index),
                             label=f"{m.title} [{m.outcome}]"))
        quote.legs_label = " | ".join(leg.label for leg in legs)

        # (1) params -------------------------------------------------------------
        handle = self.params.current()
        if handle is None:
            raise _Decline(PARAMS_UNAVAILABLE, f"no params file in {self.params.params_dir}")
        age_days = handle.age_days(now)
        if age_days > mc.max_params_age_days:
            raise _Decline(PARAMS_STALE, f"{handle.version} is {age_days:.1f} days old")
        quote.params_version = handle.version

        # (2) scope: same-game blocks ----------------------------------------
        per_game: Dict[str, List[_Leg]] = {}
        for leg in legs:
            if leg.market.game:
                per_game.setdefault(leg.market.game, []).append(leg)
        blocks: Dict[str, List[_Leg]] = {}
        for game, members in per_game.items():
            if len(members) < 2:
                continue
            if not all(leg.market.is_nfl for leg in members):
                raise _Decline(OTHER_SAME_GAME, f"{game}: same-game legs outside the NFL model")
            for leg in members:
                parsed = self._parse(leg.market.slug, leg.market.outcome_index, handle)
                if parsed is None:
                    raise _Decline(UNSUPPORTED_LEG, f"{leg.label}: "
                                   f"{unsupported_reason(leg.market.slug) or 'unparsed NFL market'}")
                leg.nfl, leg.nfl_side = parsed
            blocks[game] = members
        if not blocks:
            raise _Decline(NO_NFL_SAME_GAME, "no NFL game contributes two or more legs")
        in_block = {id(leg) for members in blocks.values() for leg in members}
        independent = [leg for leg in legs if id(leg) not in in_block]

        # (3) kickoff + books -------------------------------------------------
        for game, members in blocks.items():
            kickoff = _parse_iso(self.book_source.kickoff(members[0].market.market_id))
            if kickoff is not None and now >= kickoff:
                raise _Decline(GAME_STARTED, f"{game} kicked off {_iso(kickoff)}")
        calib_refs = {game: self._calibration_markets(game) for game in blocks}
        refs = [leg.ref for leg in legs]
        for spreads, totals in calib_refs.values():
            refs += [LegRef(m.market_id, 0) for m in spreads + totals]
        books = self.book_source.books(list(dict.fromkeys(refs)))
        now_ms = int(now.timestamp() * 1000)
        mark_inputs: List[LegMarkInput] = []
        for leg in legs:
            leg.book = books.get(leg.ref)
            mark_input = self._mark_input(leg.position_id, leg.book, now_ms)
            mark_inputs.append(mark_input)
            mark, reason = _leg_mark(mark_input)
            if reason is not None:
                raise _Decline(reason, f"{leg.label}: {self._book_note(leg.book, now_ms)}")
            leg.q = mark

        # (4) per-game joint model ------------------------------------------------
        fair_yes, model_joint_yes = 1.0, 1.0
        addons: Dict[str, float] = {}
        explanations: List[str] = []
        confidence_hits: List[Tuple[str, float]] = []
        max_gap = 0.0
        for game, members in blocks.items():
            info = self._price_block(game, members, calib_refs[game], books, handle, now_ms)
            fair_yes *= info["fair"]
            model_joint_yes *= info["model_joint"]
            max_gap = max(max_gap, info["max_gap"])
            quote.games.append(info["report"])
            for k, v in info["addons"].items():
                addons[k] = addons.get(k, 0.0) + v
            explanations += info["explanations"]
            confidence_hits += info["confidence_hits"]
        for leg in independent:
            fair_yes *= leg.q  # type: ignore[operator]
            model_joint_yes *= leg.q  # type: ignore[operator]
        naive_yes = math.prod(leg.q for leg in legs)  # type: ignore[misc]
        if independent:
            explanations.append(f"{len(independent)} leg(s) from other games priced as independent: "
                                + " x ".join(f"{leg.q:.3f}" for leg in independent))

        corr_bps = (fair_yes - naive_yes) * 10000.0
        addons["corr_model_risk_bps"] = mc.corr_model_risk_kappa * abs(corr_bps)
        addons["marginal_gap_bps"] = mc.gap_weight * max_gap * 10000.0
        if age_days > 7.0:
            addons["params_age_bps"] = mc.params_age_bps_per_day * (age_days - 7.0)
            confidence_hits.append(("params age", 0.02 * (age_days - 7.0)))
        addons = {k: round(v, 4) for k, v in addons.items() if v > 0}
        explanations.insert(0, f"Correlation: fair YES {fair_yes:.4f} vs naive {naive_yes:.4f} "
                               f"({corr_bps:+,.0f} bps); model-risk haircut "
                               f"{addons.get('corr_model_risk_bps', 0.0):,.0f} bps")

        quote.fair_yes, quote.naive_yes = fair_yes, naive_yes
        quote.model_joint_yes, quote.corr_adjustment_bps = model_joint_yes, corr_bps
        quote.legs = [{
            "label": leg.label, "position_id": leg.position_id, "slug": leg.market.slug,
            "game": leg.market.game, "modeled": leg.nfl is not None,
            "canonical": leg.joint_leg.name if leg.nfl else None,
            "bid": leg.book.bid if leg.book else None, "ask": leg.book.ask if leg.book else None,
            "book_source": leg.book.source if leg.book else None,
            "q_market": leg.q, "p_model": leg.p_model,
        } for leg in legs]

        # (5) quote terms: V1 spread stack + model add-ons ---------------------
        fair_side = fair_yes if rfq.side == "YES" else 1.0 - fair_yes
        quote.naive = round(naive_yes if rfq.side == "YES" else 1.0 - naive_yes, 6)
        cfg = self.config
        decision = price_combo(
            mark_inputs, rfq_id=rfq.rfq_id, qty_decimal=rfq.qty_decimal,
            cash_order_qty=rfq.cash_order_qty, model_version=MODEL_VERSION,
            decided_at=quote.priced_at, base_edge_bps=cfg.base_edge_bps,
            uncertainty_per_leg_bps=cfg.uncertainty_per_leg_bps, width_weight=cfg.width_weight,
            depth_slope_bps=cfg.depth_slope_bps, event_risk_bps=cfg.event_risk_bps,
            operational_buffer_bps=cfg.operational_buffer_bps, tick_size=cfg.tick_size,
            price_min=cfg.price_min, price_max=cfg.price_max, min_qty=cfg.min_qty,
            fair_override=fair_side, extra_spread_bps=addons)
        components = dict(decision.components)
        components.pop("naive_fair", None)  # YES-leg product; logged as naive_yes instead
        quote.components = components
        quote.fair = round(fair_side, 6)
        leg_spread_bps = sum(float(m.get("spread_bps", 0.0)) for m in components.get("leg_marks", []))
        confidence_hits += [("gap", 2.0 * max_gap), ("legs", 0.05 * max(0, len(legs) - 2)),
                            ("leg width", leg_spread_bps / 5000.0)]
        quote.confidence = round(max(0.0, min(1.0, 1.0 - sum(v for _, v in confidence_hits))), 4)
        components["confidence_hits"] = {k: round(v, 4) for k, v in confidence_hits if v}
        quote.explanations = explanations
        if decision.reason_code != QUOTED_OK:
            raise _Decline(decision.reason_code, "no quotable side after spread / sizing")
        quote.spread_bps_total = components.get("spread_bps_total")
        quote.bid = round(decision.sell_price, 6) or None   # price_combo: sell_price = our bid
        quote.ask = round(decision.buy_price, 6) or None    # buy_price = our offer
        quote.bid_qty, quote.ask_qty = decision.sell_qty, decision.buy_qty
        if quote.confidence < mc.min_confidence:
            raise _Decline(LOW_CONFIDENCE, f"confidence {quote.confidence:.2f} < {mc.min_confidence}")
        if rfq.direction == "BUY":
            quote.response_action, quote.response_price = "SELL", quote.ask
        else:
            quote.response_action, quote.response_price = "BUY", quote.bid
        if quote.response_price is None:
            # One side can fall outside the instrument's price limits (e.g. a
            # fair so close to 1 that our offer would exceed price_max).
            raise _Decline(NO_QUOTABLE_SIDE,
                           f"no {quote.response_action} side for a {rfq.direction} RFQ")
        quote.status, quote.reason_code = "QUOTED", QUOTED_OK

    # -- helpers ----------------------------------------------------------------
    def _mark_input(self, symbol: str, book: Optional[LegBook], now_ms: int) -> LegMarkInput:
        if book is None:
            return LegMarkInput(symbol=symbol, side="YES")
        return LegMarkInput(symbol=symbol, side="YES", bid=book.bid, ask=book.ask,
                            bid_size=book.bid_size, ask_size=book.ask_size,
                            stale=book.closed or book.age_s(now_ms) > self.model_config.max_book_age_s)

    def _book_note(self, book: Optional[LegBook], now_ms: int) -> str:
        if book is None:
            return "no book on CLOB or Gamma"
        return (f"{book.source} book bid={book.bid} ask={book.ask} age={book.age_s(now_ms):.0f}s"
                + (" closed" if book.closed else ""))

    def _parse(self, slug: str, outcome_index: int,
               handle: ParamsHandle) -> Optional[Tuple[NflLegMarket, str]]:
        """Catalog slug -> registry market, with the slate's own game_id when known."""
        parsed = parse_catalog_leg(slug, outcome_index)
        if parsed is None:
            return None
        market, side = parsed
        row = handle.game(market.home, market.away)
        if row is not None:                      # name the nflverse game the params file knows
            market = replace(market, game_id=str(row["game_id"]), season=handle.season,
                             week=handle.week)
        return market, side

    def _calibration_markets(self, game: str) -> Tuple[List[LegMarket], List[LegMarket]]:
        """Full-game spread and total markets (outcome 0) nearest 50/50 by catalog price."""
        spreads, totals = [], []
        for m in self.catalog.markets_for_game(game):
            if m.outcome_index != 0:
                continue
            parsed = parse_catalog_leg(m.slug, 0)
            if parsed is None or parsed[0].kind not in (SPR, TOT):
                continue
            (spreads if parsed[0].kind == SPR else totals).append(m)
        n = self.model_config.calibration_candidates
        by_even = lambda m: abs((m.price if m.price is not None else 0.0) - 0.5)  # noqa: E731
        return sorted(spreads, key=by_even)[:n], sorted(totals, key=by_even)[:n]

    def _main_mark(self, candidates: List[LegMarket], books: Dict[LegRef, Optional[LegBook]],
                   now_ms: int, handle: ParamsHandle
                   ) -> Optional[Tuple[NflLegMarket, str, float, str]]:
        """The candidate nearest 50/50 with a usable book: (market, side, mark, slug)."""
        best = None
        for m in candidates:
            book = books.get(LegRef(m.market_id, 0))
            mark, reason = _leg_mark(self._mark_input(m.position_id, book, now_ms))
            if reason is not None or mark is None:
                continue
            parsed = self._parse(m.slug, 0, handle)
            if parsed is None:
                continue
            if best is None or abs(mark - 0.5) < abs(best[2] - 0.5):
                best = (parsed[0], parsed[1], mark, m.slug)
        return best

    def _price_block(self, game: str, members: List[_Leg], calib: Tuple[List[LegMarket], List[LegMarket]],
                     books: Dict[LegRef, Optional[LegBook]], handle: ParamsHandle,
                     now_ms: int) -> Dict[str, Any]:
        from combo_mm.nfl.joint import GameModel, calibrate_means  # numpy/scipy, imported lazily
        from combo_mm.nfl.params_io import matchup_covariance

        mc = self.model_config
        spread = self._main_mark(calib[0], books, now_ms, handle)
        total = self._main_mark(calib[1], books, now_ms, handle)
        if spread is None or total is None:
            raise _Decline(MISSING_CALIBRATION_MARKET,
                           f"{game}: no priced full-game {'spread' if spread is None else 'total'}")
        s_mkt, s_side, s_mark, s_slug = spread
        t_mkt, t_side, t_mark, t_slug = total
        # A spread market's line is in its subject's terms; the joint model's
        # is the home expected margin, and outcome 0 pays on the subject.
        spread_line = -s_mkt.line if s_mkt.subject_is_home else s_mkt.line
        p_home_cover = s_mark if s_mkt.subject_is_home else 1.0 - s_mark
        home, away = s_mkt.home, s_mkt.away

        key = (game, handle.version, mc.corr_scale, spread_line, round(p_home_cover, 4),
               t_mkt.line, round(t_mark, 4))  # noqa: E501
        with self._cache_lock:
            cached = self._calibrations.get(key)
            if cached is not None:
                self._calibrations.move_to_end(key)
        if cached is None:
            cal = calibrate_means(
                spread_line, p_home_cover, t_mkt.line, t_mark,  # type: ignore[arg-type]
                lambda mh, ma: matchup_covariance(handle.params, home, away, mh, ma, mc.corr_scale))
            if not (3.0 <= cal.mu_home <= 50.0 and 3.0 <= cal.mu_away <= 50.0):
                raise _Decline(CALIBRATION_FAILED,
                               f"{game}: implied means {cal.mu_home:.1f}/{cal.mu_away:.1f} out of range")
            cached = (cal, GameModel((cal.mu_home, cal.mu_away), cal.cov))
            with self._cache_lock:
                self._calibrations[key] = cached
                while len(self._calibrations) > self.CALIBRATION_CACHE:
                    self._calibrations.popitem(last=False)
        cal, model = cached

        # Duplicate outcomes collapse to one leg; distinct legs keep their market price.
        unique: "OrderedDict[str, _Leg]" = OrderedDict()
        for leg in members:
            unique.setdefault(leg.position_id, leg)
        joint_legs = [leg.joint_leg for leg in unique.values()]
        p_all = model.joint(joint_legs)
        gaps = []
        for leg, jl in zip(unique.values(), joint_legs):
            leg.p_model = model.leg(jl)
            gaps.append(abs(leg.p_model - leg.q))  # type: ignore[operator]
        for leg in members:  # duplicates share the first copy's model marginal
            leg.p_model = unique[leg.position_id].p_model
        max_gap = max(gaps)
        if p_all <= 1e-6:
            raise _Decline(CONTRADICTORY_LEGS, f"{game}: legs cannot all win together "
                           f"(model joint {p_all:.2e})")
        if max_gap > mc.max_marginal_gap:
            worst = max(zip(gaps, unique.values()), key=lambda g: g[0])[1]
            raise _Decline(MODEL_MARKET_DISAGREE,
                           f"{worst.label}: model {worst.p_model:.3f} vs market {worst.q:.3f}")

        qs = [leg.q for leg in unique.values()]
        naive = math.prod(qs)  # type: ignore[arg-type]
        lift = p_all / max(math.prod(leg.p_model for leg in unique.values()), 1e-12)  # type: ignore[misc]
        raw = naive * lift if mc.method == "market_lift" else p_all
        lo = max(0.0, sum(qs) - (len(qs) - 1))  # type: ignore[arg-type]
        hi = min(qs)  # type: ignore[type-var]
        fair = min(max(raw, lo), hi)

        kinds = {leg.nfl.kind for leg in unique.values()}  # type: ignore[union-attr]
        addons: Dict[str, float] = {}
        explanations = [
            f"{away} @ {home}: calibrated to {describe_leg(s_mkt, s_side)} ({s_mark:.3f}) and "
            f"{describe_leg(t_mkt, t_side)} ({t_mark:.3f}) -> mean score {home} {cal.mu_home:.1f}, "
            f"{away} {cal.mu_away:.1f}; sd {cal.cov.sigma_home:.2f}/{cal.cov.sigma_away:.2f}, "
            f"rho {cal.cov.rho:+.3f}",
            f"{away} @ {home}: model joint {p_all:.4f}, lift over independence {lift:.3f}, "
            f"naive {naive:.4f} -> fair {fair:.4f}" + (" (Frechet-clamped)" if fair != raw else ""),
        ]
        confidence_hits: List[Tuple[str, float]] = []
        if not cal.converged:
            confidence_hits.append(("calibration not converged", 0.15))
        spread_lines = [abs(leg.nfl.line) for leg in unique.values()   # type: ignore[union-attr]
                        if leg.nfl.kind == SPR]
        if ML in kinds and SPR in kinds and any(_near_key_number(x) for x in spread_lines):  # type: ignore[arg-type]
            addons["key_number_bps"] = mc.key_number_bps
            explanations.append("Key number: ML x spread near 3/7, where the normal margin misprices")
        if abs(spread_line) >= 10.0 and kinds & {ML, SPR} and kinds & {TOT, TT}:  # type: ignore[arg-type]
            addons["big_favorite_bps"] = mc.big_favorite_bps
            confidence_hits.append(("big favourite", 0.1))
            explanations.append("Big favourite: margin/total dependence for 10+ pt favourites is unmodeled")
        if len(unique) > 2:
            addons["tail_bps"] = mc.tail_bps_per_leg * (len(unique) - 2)
            explanations.append(f"Tails: {len(unique)} same-game legs, Gaussian tails are thin")

        report = {
            "game": game, "home": home, "away": away,
            "calibration_spread": s_slug, "p_home_cover": p_home_cover, "spread_line": spread_line,
            "calibration_total": t_slug, "p_over": t_mark, "total_line": t_mkt.line,
            "mu_home": cal.mu_home, "mu_away": cal.mu_away, "converged": cal.converged,
            "iterations": cal.iterations, **cal.cov.to_dict(), "corr_scale": mc.corr_scale,
            "model_joint": p_all, "naive": naive, "lift": lift, "fair": fair,
            "frechet_clamped": fair != raw, "max_marginal_gap": max_gap,
            "params_game_row": handle.game(home, away) is not None,
        }
        return {"fair": fair, "model_joint": p_all, "max_gap": max_gap, "addons": addons,
                "explanations": explanations, "confidence_hits": confidence_hits, "report": report}

"""Deterministic simulated NFL RFQ sessions built from historical games (#15 Part B).

There is no Polymarket NFL RFQ history to replay (see #11), and the only
scripted replay dataset is political markets, so Steps 2, 3 and 5 have no NFL
flow to run against. This module manufactures that flow from nflverse games
in **the exact wire format the pipeline already replays**
(:mod:`combo_mm.fixtures` item schema), so ``SimulatedTransport``,
``normalize``, ``EventStore.apply``, ``ShadowQuotingEngine`` and
``paper_backtest.run_backtest`` consume it unchanged.

Four principles decide everything here:

1. **Exogenous events only.** A session carries what the market would have
   shown us -- leg books, RFQ requests, the close of each request window,
   settlements -- and nothing that depends on *our* quote. There are no
   acceptances, confirmations or executions: those are counterfactual, and
   #5's fill model produces them. Pre-baking them would make the backtest
   meaningless.
2. **Leakage-safe by construction.** Every book is a function of the line
   path up to its own timestamp; final scores appear only in settlement
   events stamped after the game ends. Data only a fill model may see
   (requester type, the closing-line fair, a competitor quote) goes to a
   separate sidecar file that no pricing module may read.
3. **Same wire format.** Items are ``{"t": ms, "kind": "event"|"book"|
   "disconnect", ...}`` on a dataset-relative clock, with absolute
   ``exchange_ts`` on every event.
4. **Deterministic.** One seed gives byte-identical files. Randomness comes
   from per-(game, purpose) substreams seeded by SHA-256, so adding a feature
   cannot shift unrelated draws, and every normal draw is Box-Muller over
   ``random.Random`` (whose Mersenne Twister stream is stable across CPython
   versions) rather than a library sampler that may be re-tuned.

Modelling assumptions (documented in ``docs/rfq-simulation.md``, all knobs on
:class:`SimConfig`, all recorded in the manifest):

- **Lines.** nflverse has no opening lines, so the margin and total paths are
  Brownian motions run *backwards* from the closing line: the path ends at
  the true close and is noisier the earlier it is read.
- **Prices.** A leg's probability at time ``t`` comes from the score model at
  the path's implied means, offset by the constant correction that pins the
  main spread and total to their de-vigged closing prices. Moneylines blend
  linearly into the de-vigged closing moneyline, which the spread/total
  calibration does not pin.
- **Flow.** Arrivals are a non-homogeneous Poisson process rising into
  kickoff; combo shapes, sides and sizes follow configurable retail-tilted
  weights.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import random
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from combo_mm.nfl import MODEL_VERSION
from combo_mm.nfl import joint
from combo_mm.nfl.estimate import implied_means
from combo_mm.nfl.ingest import Game, devig_pair
from combo_mm.nfl.markets import (
    ML,
    PUSH_VOID,
    SPR,
    TIE_VOID,
    TOT,
    LegRegistry,
    NflLegMarket,
    settlement_price,
    to_joint_leg,
)
from combo_mm.nfl.params_io import matchup_covariance

__all__ = [
    "GENERATOR_VERSION",
    "FAMILIES",
    "SimConfig",
    "SimSession",
    "WalkForwardParams",
    "build_session",
    "write_dataset",
    "load_session",
    "load_manifest",
    "session_datetime",
]

GENERATOR_VERSION = "nfl-rfq-sim-1"

# Combo families: which markets a request touches. Weights are configurable;
# "contradictory" exists so the decline path (fair = 0) is exercised.
FAMILIES = ("ml_spread", "spread_total", "ml_total", "ml_spread_total",
            "alt_line", "contradictory")

_SETTLE_DELAY = timedelta(hours=3, minutes=30)
_OT_EXTRA = timedelta(minutes=15)


@dataclass(frozen=True)
class SimConfig:
    """Every knob of the simulation. Recorded verbatim in the manifest."""

    seed: int = 7
    rfqs_per_game: float = 40.0

    # Market listing (passed through to combo_mm.nfl.markets.game_markets).
    alt_lines: bool = True
    force_half_point_lines: bool = True
    push_rule: str = PUSH_VOID
    tie_rule: str = TIE_VOID

    # Line paths.
    open_offset_days: float = 6.0
    sigma_spread_week: float = 1.0      # points of margin-line noise per week out
    sigma_total_week: float = 1.25
    path_grid_hours: float = 1.0
    ml_blend: bool = True

    # Books.
    microstructure_sd: float = 0.004
    half_spread_main: float = 0.01
    half_spread_alt: float = 0.02
    tick_size: float = 0.001
    mid_min: float = 0.01               # clamp on the simulated mid, not a quote limit
    mid_max: float = 0.99
    book_size_median: float = 500.0
    book_size_sigma: float = 0.5
    book_lag_ms: int = 1500             # books precede their RFQ by U(0, this)

    # Flow.
    tau_hours: float = 18.0
    p_fav: float = 0.62
    p_over: float = 0.58
    family_weights: Tuple[Tuple[str, float], ...] = (
        ("ml_spread", 0.25), ("spread_total", 0.25), ("ml_total", 0.15),
        ("ml_spread_total", 0.20), ("alt_line", 0.10), ("contradictory", 0.05),
    )
    qty_share: float = 0.8              # rest are cash-sized RFQs
    qty_median: float = 50.0
    qty_sigma: float = 1.1
    qty_min: float = 1.0
    qty_max: float = 5000.0
    cash_median: float = 25.0
    cash_sigma: float = 1.1
    n_requesters: int = 500
    sharp_share: float = 0.05           # sidecar only, never in the session
    submission_deadline_s: float = 3.0

    # Robustness noise (re-delivery and re-ordering only: never new state).
    stale_share: float = 0.02
    stale_book_age_ms: int = 5000       # > PipelineConfig.staleness_ms
    duplicate_share: float = 0.01
    out_of_order_share: float = 0.005
    out_of_order_window_ms: int = 200
    disconnects_per_week: float = 1.0

    # Competitor model for the sidecar (a naive independent-leg maker).
    competitor_half_spread: float = 0.015

    def __post_init__(self) -> None:
        if self.rfqs_per_game <= 0 or self.open_offset_days <= 0 or self.tau_hours <= 0:
            raise ValueError("rfqs_per_game, open_offset_days and tau_hours must be > 0")
        if not 0.0 < self.qty_share <= 1.0:
            raise ValueError("qty_share must be in (0, 1]")
        if self.n_requesters < 1:
            raise ValueError("n_requesters must be >= 1")
        names = [name for name, _ in self.family_weights]
        if sorted(names) != sorted(FAMILIES):
            raise ValueError(f"family_weights must cover exactly {FAMILIES}")
        if any(w < 0 for _, w in self.family_weights):
            raise ValueError("family weights must be >= 0")
        if sum(w for _, w in self.family_weights) <= 0:
            raise ValueError("family weights must not all be zero")

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["family_weights"] = {name: w for name, w in self.family_weights}
        return out


# ---------------------------------------------------------------------------
# Deterministic randomness
# ---------------------------------------------------------------------------

class _Rng:
    """Version-stable draws over ``random.Random``.

    ``random.gauss``/``normalvariate`` are implementation details that have
    been re-tuned between CPython releases, so normals are Box-Muller over
    ``random()`` -- which is documented Mersenne Twister and reproducible.
    """

    def __init__(self, seed: int) -> None:
        self._r = random.Random(seed)

    def uniform(self, lo: float = 0.0, hi: float = 1.0) -> float:
        return lo + (hi - lo) * self._r.random()

    def normal(self, mu: float = 0.0, sd: float = 1.0) -> float:
        u1 = max(self._r.random(), 1e-300)
        u2 = self._r.random()
        return mu + sd * math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def lognormal(self, median: float, sigma: float) -> float:
        return median * math.exp(sigma * self.normal())

    def poisson(self, lam: float) -> int:
        """Knuth's product method (``lam`` here is tens, so no underflow)."""
        limit, product, n = math.exp(-lam), 1.0, 0
        while True:
            product *= self._r.random()
            if product <= limit:
                return n
            n += 1

    def chance(self, p: float) -> bool:
        return self._r.random() < p

    def pick(self, items: Sequence[Any]) -> Any:
        return items[min(int(self._r.random() * len(items)), len(items) - 1)]

    def weighted(self, pairs: Sequence[Tuple[Any, float]]) -> Any:
        total = sum(w for _, w in pairs)
        target = self._r.random() * total
        upto = 0.0
        for value, weight in pairs:
            upto += weight
            if target < upto:
                return value
        return pairs[-1][0]


def _substream(seed: int, *parts: Any) -> _Rng:
    """Independent stream for one (dataset seed, purpose, key) triple."""
    material = "|".join([str(seed)] + [str(p) for p in parts]).encode()
    return _Rng(int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))


# ---------------------------------------------------------------------------
# Line paths
# ---------------------------------------------------------------------------

class _LinePath:
    """Margin and total lines over the pre-game window, pinned at the close.

    A Brownian motion is walked **backwards** from kickoff on a fixed grid, so
    the path ends at the true closing line and is noisier the further out it
    is read. Reading the path never touches the final score, which is what
    makes the books leakage-safe.
    """

    def __init__(self, spread_line: float, total_line: float, open_at: datetime,
                 kickoff: datetime, rng: _Rng, cfg: SimConfig) -> None:
        self.open_at = open_at
        self.kickoff = kickoff
        span_h = max((kickoff - open_at).total_seconds() / 3600.0, cfg.path_grid_hours)
        self._n = max(int(math.ceil(span_h / cfg.path_grid_hours)), 1)
        self._step_h = span_h / self._n
        dt_weeks = (self._step_h / 24.0) / 7.0
        step_sd = math.sqrt(dt_weeks)
        spread_nodes = [spread_line]
        total_nodes = [total_line]
        for _ in range(self._n):
            spread_nodes.append(spread_nodes[-1] + cfg.sigma_spread_week * rng.normal(0.0, step_sd))
            total_nodes.append(total_nodes[-1] + cfg.sigma_total_week * rng.normal(0.0, step_sd))
        # Node 0 is kickoff; node n is the open. Store open -> kickoff order.
        self._spread = list(reversed(spread_nodes))
        self._total = list(reversed(total_nodes))

    def _interpolate(self, nodes: List[float], at: datetime) -> float:
        hours = (at - self.open_at).total_seconds() / 3600.0
        position = min(max(hours / self._step_h, 0.0), float(self._n))
        low = min(int(position), self._n - 1)
        frac = position - low
        return nodes[low] + frac * (nodes[low + 1] - nodes[low])

    def at(self, when: datetime) -> Tuple[float, float]:
        """``(margin_line, total_line)`` as the market would have shown them."""
        return self._interpolate(self._spread, when), self._interpolate(self._total, when)

    def progress(self, when: datetime) -> float:
        """0 at the open, 1 at kickoff."""
        span = (self.kickoff - self.open_at).total_seconds()
        return min(max((when - self.open_at).total_seconds() / span, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Per-game view of the listed markets
# ---------------------------------------------------------------------------

@dataclass
class _GameView:
    game: Game
    markets: List[NflLegMarket]
    kickoff: datetime
    open_at: datetime
    path: _LinePath
    params: Dict[str, Any]
    mu_offset: Tuple[float, float]       # calibration correction pinning the close
    p_home_ml_close: Optional[float]
    fav_ml: NflLegMarket
    dog_ml: NflLegMarket
    fav_spr: NflLegMarket
    dog_spr: NflLegMarket
    total: NflLegMarket
    alt_markets: List[NflLegMarket] = field(default_factory=list)

    @property
    def main_symbols(self) -> List[str]:
        return [self.fav_ml.symbol, self.dog_ml.symbol, self.fav_spr.symbol,
                self.dog_spr.symbol, self.total.symbol]

    def means(self, when: datetime) -> Tuple[float, float]:
        margin, total = self.path.at(when)
        mu_home, mu_away = implied_means(margin, total)
        return mu_home + self.mu_offset[0], mu_away + self.mu_offset[1]

    def covariance(self, when: datetime):
        mu_home, mu_away = self.means(when)
        return matchup_covariance(self.params, self.game.home, self.game.away,
                                  mu_home, mu_away)

    def leg_probability(self, market: NflLegMarket, side: str, when: datetime,
                        cfg: SimConfig) -> float:
        """P(side of ``market`` hits) as the market would price it at ``when``."""
        mu = self.means(when)
        cov = self.covariance(when)
        p = joint.leg_probability(to_joint_leg(market, side), mu, cov)
        if market.kind == ML and cfg.ml_blend and self.p_home_ml_close is not None:
            market_p = (self.p_home_ml_close if market.subject_is_home
                        else 1.0 - self.p_home_ml_close)
            if side == "NO":
                market_p = 1.0 - market_p
            weight = self.path.progress(when)
            p = (1.0 - weight) * p + weight * market_p
        return min(max(p, 1e-4), 1 - 1e-4)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_view(game: Game, registry: LegRegistry, params: Dict[str, Any],
                cfg: SimConfig) -> _GameView:
    markets = registry.markets_for_game(game.game_id)
    if not markets:
        raise ValueError(f"{game.game_id}: no markets registered")
    kickoff = _parse_iso(markets[0].kickoff_utc)
    open_at = kickoff - timedelta(days=cfg.open_offset_days)
    path = _LinePath(game.spread_line, game.total_line, open_at, kickoff,
                     _substream(cfg.seed, "path", game.game_id), cfg)

    # Pin the close: shift the path's implied means so the main spread and
    # total price at their de-vigged closing prices. Without this the dataset
    # would close at 0.50/0.50 regardless of what the market actually showed.
    p_home_cover = devig_pair(game.home_spread_odds, game.away_spread_odds)
    p_over = devig_pair(game.over_odds, game.under_odds)
    mu_offset = (0.0, 0.0)
    if p_home_cover is not None and p_over is not None:
        from combo_mm.nfl.joint import calibrate_means  # scipy, offline only

        def cov_fn(mu_home: float, mu_away: float):
            return matchup_covariance(params, game.home, game.away, mu_home, mu_away)

        calibration = calibrate_means(game.spread_line, p_home_cover,
                                      game.total_line, p_over, cov_fn)
        closing = implied_means(game.spread_line, game.total_line)
        mu_offset = (calibration.mu_home - closing[0], calibration.mu_away - closing[1])

    by_kind: Dict[str, List[NflLegMarket]] = {}
    for market in markets:
        by_kind.setdefault(market.kind, []).append(market)
    mains = [m for m in by_kind.get(SPR, []) if m.is_main_line]
    fav_spr = next(m for m in mains if m.line < 0)
    dog_spr = next(m for m in mains if m.line > 0)
    fav_ml = next(m for m in by_kind[ML] if m.subject == fav_spr.subject)
    dog_ml = next(m for m in by_kind[ML] if m.subject == dog_spr.subject)
    total = next(m for m in by_kind[TOT] if m.is_main_line)
    alts = [m for m in markets if not m.is_main_line]
    return _GameView(
        game=game, markets=markets, kickoff=kickoff, open_at=open_at, path=path,
        params=params, mu_offset=mu_offset,
        p_home_ml_close=devig_pair(game.home_moneyline, game.away_moneyline),
        fav_ml=fav_ml, dog_ml=dog_ml, fav_spr=fav_spr, dog_spr=dog_spr,
        total=total, alt_markets=alts,
    )


# ---------------------------------------------------------------------------
# Combo shapes
# ---------------------------------------------------------------------------

Leg = Tuple[str, str]   # (symbol, side)


def _ml_leg(view: _GameView, rng: _Rng, cfg: SimConfig) -> Leg:
    market = view.fav_ml if rng.chance(cfg.p_fav) else view.dog_ml
    return (market.symbol, "YES")


def _spread_leg(view: _GameView, rng: _Rng, cfg: SimConfig) -> Leg:
    market = view.fav_spr if rng.chance(cfg.p_fav) else view.dog_spr
    return (market.symbol, "YES")


def _total_leg(view: _GameView, rng: _Rng, cfg: SimConfig) -> Leg:
    return (view.total.symbol, "YES" if rng.chance(cfg.p_over) else "NO")


def _alt_leg(view: _GameView, rng: _Rng, cfg: SimConfig) -> Leg:
    market = rng.pick(view.alt_markets or [view.total])
    side = "YES" if market.kind == SPR or rng.chance(cfg.p_over) else "NO"
    return (market.symbol, side)


def _pick_legs(family: str, view: _GameView, rng: _Rng, cfg: SimConfig) -> List[Leg]:
    """Legs for one RFQ of ``family``, always within a single game."""
    if family == "contradictory":
        # The underdog wins outright AND the favourite covers: fair = 0, so the
        # pricer must decline rather than quote a price near the leg product.
        return [(view.dog_ml.symbol, "YES"), (view.fav_spr.symbol, "YES")]
    if family == "ml_spread":
        ml = _ml_leg(view, rng, cfg)
        spread = _spread_leg(view, rng, cfg)
        if ml[0] == view.dog_ml.symbol and spread[0] == view.fav_spr.symbol:
            spread = (view.dog_spr.symbol, "YES")   # leave impossible to its own family
        return [ml, spread]
    if family == "spread_total":
        return [_spread_leg(view, rng, cfg), _total_leg(view, rng, cfg)]
    if family == "ml_total":
        return [_ml_leg(view, rng, cfg), _total_leg(view, rng, cfg)]
    if family == "ml_spread_total":
        legs = _pick_legs("ml_spread", view, rng, cfg)
        return legs + [_total_leg(view, rng, cfg)]
    if family == "alt_line":
        first = _alt_leg(view, rng, cfg)
        second = _alt_leg(view, rng, cfg)
        if second[0] == first[0]:
            second = _total_leg(view, rng, cfg)
        return [first, second]
    raise ValueError(f"unknown family {family!r}")


def _combo_symbol(view: _GameView, legs: Sequence[Leg]) -> str:
    """Deterministic combo symbol: the game prefix plus each leg's market part."""
    prefix = view.fav_ml.symbol.rsplit("-ML-", 1)[0]
    parts = []
    for symbol, side in legs:
        tail = symbol[len(prefix) + 1:]
        parts.append(tail if side == "YES" else f"{tail}~NO")
    return f"{prefix}:" + "+".join(parts)


# ---------------------------------------------------------------------------
# Session assembly
# ---------------------------------------------------------------------------

@dataclass
class SimSession:
    """A generated dataset, in memory."""

    base_ts: datetime
    items: List[Dict[str, Any]]
    combos: List[Dict[str, Any]]
    registry: LegRegistry
    sidecar: List[Dict[str, Any]]
    counts: Dict[str, Any]


def _round_tick(value: float, tick: float, up: bool) -> float:
    scaled = value / tick
    stepped = math.ceil(scaled - 1e-9) if up else math.floor(scaled + 1e-9)
    return round(stepped * tick, 10)


def _book_item(symbol: str, probability: float, when: datetime, rng: _Rng,
               cfg: SimConfig, main: bool) -> Tuple[Dict[str, Any], float]:
    """``(book item, mid)``. The mid is what a maker reading this book sees."""
    mid = min(max(probability + rng.normal(0.0, cfg.microstructure_sd),
                  cfg.mid_min), cfg.mid_max)
    half = cfg.half_spread_main if main else cfg.half_spread_alt
    bid = max(_round_tick(mid - half, cfg.tick_size, up=False), cfg.tick_size)
    ask = min(_round_tick(mid + half, cfg.tick_size, up=True), 1.0 - cfg.tick_size)
    item = {
        "kind": "book", "symbol": symbol, "bid": bid, "ask": ask,
        "bid_size": round(rng.lognormal(cfg.book_size_median, cfg.book_size_sigma), 2),
        "ask_size": round(rng.lognormal(cfg.book_size_median, cfg.book_size_sigma), 2),
        "ts": _iso(when), "_at": when,
    }
    return item, (bid + ask) / 2.0


def _arrival_times(view: _GameView, rng: _Rng, cfg: SimConfig) -> List[datetime]:
    """Non-homogeneous Poisson arrivals, intensity rising into kickoff.

    Conditional on the count, arrival times are i.i.d. from the normalised
    intensity ``exp(-(kickoff - t) / tau)``, so they are drawn by inverting
    that CDF -- no thinning loop, and the same count always consumes the same
    number of draws.
    """
    window_h = (view.kickoff - view.open_at).total_seconds() / 3600.0
    scale = 1.0 - math.exp(-window_h / cfg.tau_hours)
    n = rng.poisson(cfg.rfqs_per_game)
    times = []
    for _ in range(n):
        back_h = -cfg.tau_hours * math.log(max(1.0 - rng.uniform() * scale, 1e-12))
        times.append(view.kickoff - timedelta(hours=min(back_h, window_h)))
    return sorted(times)


def _game_items(view: _GameView, cfg: SimConfig) -> Tuple[
        List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(items, combos, sidecar_rows)`` for one game. Items carry ``_at``."""
    game = view.game
    flow = _substream(cfg.seed, "flow", game.game_id)
    books_rng = _substream(cfg.seed, "books", game.game_id)
    by_symbol = {m.symbol: m for m in view.markets}
    items: List[Dict[str, Any]] = []
    combos: List[Dict[str, Any]] = []
    sidecar: List[Dict[str, Any]] = []
    rfq_legs: List[Tuple[str, List[Leg]]] = []

    close_model = None
    for index, at in enumerate(_arrival_times(view, flow, cfg)):
        rfq_id = f"{game.game_id}-R{index:04d}"
        family = flow.weighted(cfg.family_weights)
        legs = _pick_legs(family, view, flow, cfg)
        leg_rows = [{"symbol": symbol, "side": side} for symbol, side in legs]
        combo_symbol = _combo_symbol(view, legs)
        combos.append({
            "symbol": combo_symbol, "tick_size": cfg.tick_size,
            "price_min": 0.001, "price_max": 0.999, "min_qty": cfg.qty_min,
            "legs": leg_rows,
        })

        # Books: every RFQ leg plus the game's main markets, so #2 always has
        # the sibling markets its calibration needs.
        stale_leg = legs[0][0] if flow.chance(cfg.stale_share) else None
        wanted = [symbol for symbol, _ in legs]
        wanted += [s for s in view.main_symbols if s not in wanted]
        mids: Dict[str, float] = {}
        for symbol in wanted:
            lag_ms = (cfg.stale_book_age_ms if symbol == stale_leg
                      else flow.uniform(0.0, cfg.book_lag_ms))
            book_at = at - timedelta(milliseconds=lag_ms)
            market = by_symbol[symbol]
            probability = view.leg_probability(market, "YES", book_at, cfg)
            book, mids[symbol] = _book_item(symbol, probability, book_at, books_rng,
                                            cfg, market.is_main_line)
            items.append(book)

        if flow.chance(cfg.qty_share):
            qty = int(min(max(round(flow.lognormal(cfg.qty_median, cfg.qty_sigma)),
                              cfg.qty_min), cfg.qty_max))
            size: Dict[str, Any] = {"qtyDecimal": str(qty)}
        else:
            cash = round(flow.lognormal(cfg.cash_median, cfg.cash_sigma), 2)
            size = {"cashOrderQty": f"{cash:.2f}"}
        requester = f"taker-{int(flow.uniform(0, cfg.n_requesters)):03d}"
        at_iso = _iso(at)
        items.append({
            "kind": "event", "stream": True, "_at": at,
            "raw": {
                "event_id": f"{rfq_id}:created", "event_type": "rfq_created",
                "rfq_id": rfq_id, "exchange_ts": at_iso, "symbol": combo_symbol,
                "payload": {
                    "id": rfq_id, "symbol": combo_symbol,
                    "rfqCreatorUserId": requester, "createdTime": at_iso,
                    "updatedTime": at_iso, "restRemainder": False,
                    "status": "OPEN", "comboLegs": leg_rows,
                    "submissionDeadline": _iso(
                        at + timedelta(seconds=cfg.submission_deadline_s)),
                    **size,
                },
            },
        })
        # The RFQ stops accepting quotes at its submission deadline. That is
        # exogenous -- CLOSED is the neutral terminal state and says nothing
        # about whether anyone accepted, which is #5's fill model to decide
        # (the pipeline lets a quote advance after its RFQ closed; see
        # fixtures.py RFQ-004). Without it the engine would re-price the RFQ
        # when its settlement arrives hours later.
        closed_at = at + timedelta(seconds=cfg.submission_deadline_s)
        items.append({
            "kind": "event", "stream": True, "_at": closed_at,
            "raw": {
                "event_id": f"{rfq_id}:closed", "event_type": "rfq_closed",
                "rfq_id": rfq_id, "exchange_ts": _iso(closed_at),
                "payload": {"id": rfq_id, "status": "CLOSED",
                            "updatedTime": _iso(closed_at)},
            },
        })
        rfq_legs.append((rfq_id, legs))

        # --- sidecar: BACKTEST-ONLY, never on the session ------------------
        if close_model is None:
            close_model = joint.GameModel(view.means(view.kickoff),
                                          view.covariance(view.kickoff))
        canonical = [to_joint_leg(by_symbol[s], side) for s, side in legs]
        # The competitor is an independent-leg maker reading the same books.
        naive = 1.0
        for symbol, side in legs:
            naive *= mids[symbol] if side == "YES" else 1.0 - mids[symbol]
        model_fair = close_model.joint(canonical)
        naive_close = 1.0
        for symbol, side in legs:
            naive_close *= view.leg_probability(by_symbol[symbol], side,
                                                view.kickoff, cfg)
        sidecar.append({
            "rfq_id": rfq_id, "game_id": game.game_id, "family": family,
            "requester_id": requester,
            "requester_type": "sharp" if flow.chance(cfg.sharp_share) else "retail",
            "closing_model_fair": round(model_fair, 6),
            "closing_naive_fair": round(naive_close, 6),
            "naive_fair_at_request": round(naive, 6),
            "competitor_bid": round(max(naive - cfg.competitor_half_spread, 0.0), 6),
            "competitor_offer": round(min(naive + cfg.competitor_half_spread, 1.0), 6),
        })

    # --- settlements: stream-invisible, after the game ---------------------
    if game.played:
        settled_at = view.kickoff + _SETTLE_DELAY + (_OT_EXTRA if game.overtime
                                                     else timedelta(0))
        for rfq_id, legs in rfq_legs:
            settled_rows = []
            for symbol, side in legs:
                price = settlement_price(by_symbol[symbol], game.home_score,
                                         game.away_score, push_rule=cfg.push_rule)
                row: Dict[str, Any] = {"symbol": symbol, "side": side}
                if price is not None:
                    row["settlementPrice"] = price
                settled_rows.append(row)
            items.append({
                "kind": "event", "stream": False, "_at": settled_at,
                "raw": {
                    "event_id": f"{rfq_id}:settled", "event_type": "rfq_updated",
                    "rfq_id": rfq_id, "exchange_ts": _iso(settled_at),
                    "payload": {"id": rfq_id, "updatedTime": _iso(settled_at),
                                "comboLegs": settled_rows},
                },
            })
    return items, combos, sidecar


def _event_leg_symbols(item: Dict[str, Any]) -> set:
    legs = item["raw"].get("payload", {}).get("comboLegs") or []
    return {leg.get("symbol") for leg in legs}


def _swappable(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    """Can these two adjacent items trade places without either seeing a change?

    An event reads only its own legs' books, so an event and a book for an
    unrelated symbol are independent. Two events are independent because each
    applies to its own RFQ. Two books are not swapped: for a shared symbol the
    later snapshot wins, so their order is information.
    """
    kinds = (first["kind"], second["kind"])
    if kinds == ("event", "event"):
        return True
    if kinds == ("event", "book"):
        return second["symbol"] not in _event_leg_symbols(first)
    if kinds == ("book", "event"):
        return first["symbol"] not in _event_leg_symbols(second)
    return False


def _apply_noise(items: List[Dict[str, Any]], cfg: SimConfig,
                 span_weeks: float) -> Dict[str, int]:
    """Add re-delivery and re-ordering noise, in place. Never new state.

    Duplicates repeat an event under its own ``event_id`` (the store dedups).
    Re-ordering swaps an event with an adjacent item it does not read -- a
    book for a symbol that is not one of its legs, or another event -- so
    nothing either item observes changes. Both therefore leave the replayed
    state digest identical to a clean run, which is exactly the Step 1
    guarantee being exercised; ``test_nfl_rfq_sim`` asserts that equality.
    """
    rng = _substream(cfg.seed, "noise")
    counts = {"duplicates": 0, "reordered": 0, "disconnects": 0}

    duplicates = []
    for item in items:
        if item["kind"] == "event" and item["raw"]["event_type"] == "rfq_created" \
                and rng.chance(cfg.duplicate_share):
            copy = {**item, "raw": item["raw"],
                    "_at": item["_at"] + timedelta(milliseconds=rng.uniform(10, 200))}
            duplicates.append(copy)
    items.extend(duplicates)
    counts["duplicates"] = len(duplicates)

    items.sort(key=lambda i: (i["_at"], i["kind"], i.get("symbol", "")))
    window = timedelta(milliseconds=cfg.out_of_order_window_ms)
    for index in range(len(items) - 1):
        first, second = items[index], items[index + 1]
        if second["_at"] - first["_at"] > window:
            continue
        if not _swappable(first, second) or not rng.chance(cfg.out_of_order_share):
            continue
        first["_at"], second["_at"] = second["_at"], first["_at"]
        items[index], items[index + 1] = second, first
        counts["reordered"] += 1

    n_disconnects = int(max(round(span_weeks * cfg.disconnects_per_week), 0))
    if n_disconnects and items:
        first_at, last_at = items[0]["_at"], items[-1]["_at"]
        span_s = (last_at - first_at).total_seconds()
        for index in range(n_disconnects):
            offset = span_s * (index + 0.5) / n_disconnects
            items.append({"kind": "disconnect",
                          "_at": first_at + timedelta(seconds=offset)})
        counts["disconnects"] = n_disconnects
    return counts


def build_session(games: Sequence[Game], config: Optional[SimConfig] = None,
                  *, params_provider: Callable[[int, int], Dict[str, Any]],
                  registry: Optional[LegRegistry] = None) -> SimSession:
    """Generate a session from ``games`` (played games with closing lines).

    ``params_provider(season, week)`` supplies the covariance params for a
    game's week -- walk-forward by construction, since it is the same weekly
    file the pricer reads live. Games without closing lines are skipped.
    """
    cfg = config or SimConfig()
    usable = sorted((g for g in games if g.has_lines and g.spread_line != 0),
                    key=lambda g: (g.season, g.week, g.game_id))
    if not usable:
        raise ValueError("no games with closing lines to simulate")
    registry = registry or LegRegistry.from_games(
        usable, alt_lines=cfg.alt_lines,
        force_half_point_lines=cfg.force_half_point_lines, tie_rule=cfg.tie_rule)

    items: List[Dict[str, Any]] = []
    combos: List[Dict[str, Any]] = []
    sidecar: List[Dict[str, Any]] = []
    families: Dict[str, int] = {}
    params_used: Dict[str, Dict[str, Any]] = {}
    n_pushed = n_settled = 0
    for game in usable:
        params = params_provider(game.season, game.week)
        params_used[f"{game.season}_w{game.week:02d}"] = {
            "season": params.get("season"), "week": params.get("week"),
            "as_of": params.get("as_of"), "model_version": params.get("model_version"),
        }
        view = _build_view(game, registry, params, cfg)
        game_items, game_combos, game_sidecar = _game_items(view, cfg)
        items += game_items
        combos += game_combos
        sidecar += game_sidecar
        for row in game_sidecar:
            families[row["family"]] = families.get(row["family"], 0) + 1
        for item in game_items:
            if item["kind"] == "event" and item["raw"]["event_type"] == "rfq_updated":
                n_settled += 1
                if any("settlementPrice" not in leg
                       for leg in item["raw"]["payload"]["comboLegs"]):
                    n_pushed += 1

    if not items:
        raise ValueError(
            f"{len(usable)} games produced no RFQs: rfqs_per_game="
            f"{cfg.rfqs_per_game} is too low for this seed")
    span_weeks = max(
        (max(i["_at"] for i in items) - min(i["_at"] for i in items)).days / 7.0, 1.0)
    noise = _apply_noise(items, cfg, span_weeks)

    base_ts = min(i["_at"] for i in items).replace(microsecond=0)
    items.sort(key=lambda i: (i["_at"], i["kind"], i.get("symbol", "")))
    seq_by_symbol: Dict[str, int] = {}
    ordered: List[Dict[str, Any]] = []
    for item in items:
        at = item.pop("_at")
        entry = {"t": int((at - base_ts).total_seconds() * 1000), **item}
        if entry["kind"] == "book":
            symbol = entry["symbol"]
            seq_by_symbol[symbol] = seq_by_symbol.get(symbol, 0) + 1
            entry["seq"] = seq_by_symbol[symbol]
        ordered.append(entry)

    # Deduplicate combo reference rows: many RFQs share a combo shape.
    unique_combos = {c["symbol"]: c for c in combos}
    counts = {
        "games": len(usable),
        "rfqs": len(sidecar),
        "books": sum(1 for i in ordered if i["kind"] == "book"),
        "settlements": n_settled,
        "pushed_or_void": n_pushed,
        "combos": len(unique_combos),
        "rfqs_by_family": dict(sorted(families.items())),
        "sharp_rfqs": sum(1 for r in sidecar if r["requester_type"] == "sharp"),
        "params_weeks": dict(sorted(params_used.items())),
        **noise,
    }
    return SimSession(
        base_ts=base_ts, items=ordered,
        combos=[unique_combos[s] for s in sorted(unique_combos)],
        registry=registry, sidecar=sidecar, counts=counts,
    )


class WalkForwardParams:
    """Weekly covariance params for the generator, estimated walk-forward.

    ``params_provider`` for :func:`build_session`. Each week's file is
    estimated from games **strictly before** that week with the frozen
    estimator, then cached under ``directory`` as
    ``nfl_<season>_w<ww>.json`` -- the same kind of weekly file the pricer
    reads live, so the dataset can never carry a parameter fitted on its own
    outcomes.
    """

    def __init__(self, games: Sequence[Game], directory: Path | str,
                 estimator: Optional[Any] = None,
                 data_vintage: Optional[Dict[str, Any]] = None) -> None:
        from combo_mm.nfl.estimate import EstimatorConfig, ResidualTable

        self._table = ResidualTable.build(games)
        self._directory = Path(directory)
        self._estimator = estimator or EstimatorConfig()
        self._vintage = dict(data_vintage or {})
        self._cache: Dict[Tuple[int, int], Dict[str, Any]] = {}

    def __call__(self, season: int, week: int) -> Dict[str, Any]:
        from combo_mm.nfl.estimate import estimate_params
        from combo_mm.nfl.params_io import load_params, params_filename, write_params

        key = (season, week)
        if key in self._cache:
            return self._cache[key]
        path = self._directory / params_filename(season, week)
        if not path.exists():
            write_params(estimate_params(self._table, season, week, self._estimator,
                                         data_vintage=self._vintage), path)
        # Always read back the written file rather than using the estimate in
        # memory: the writer rounds to 6 dp, so a freshly estimated run would
        # otherwise produce marginally different prices from one replaying the
        # cached file, and the dataset would not be byte-reproducible. The
        # rounded file is also exactly what the pricer reads live.
        params = load_params(path)
        self._cache[key] = params
        return params


def session_datetime(base_ts: datetime, t_ms: int) -> datetime:
    """Absolute time of a session item (the replay's virtual clock base)."""
    return base_ts + timedelta(milliseconds=t_ms)


# ---------------------------------------------------------------------------
# On-disk dataset
# ---------------------------------------------------------------------------

def _canonical_jsonl_gz(rows: Iterable[Dict[str, Any]], path: Path) -> None:
    """Write gzipped JSONL deterministically (no mtime, sorted keys)."""
    payload = "".join(json.dumps(row, sort_keys=True) + "\n"
                      for row in rows).encode("utf-8")
    with open(path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as fh:
            fh.write(payload)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def write_dataset(session: SimSession, out_dir: Path | str,
                  config: Optional[SimConfig] = None, *,
                  source: Optional[Dict[str, Any]] = None) -> Path:
    """Write the B6 layout and its manifest. Returns the manifest path."""
    cfg = config or SimConfig()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    session_path = out / "session.jsonl.gz"
    sidecar_path = out / "sidecar.jsonl.gz"
    combos_path = out / "combos.json"
    markets_path = out / "markets.json"

    _canonical_jsonl_gz(session.items, session_path)
    _canonical_jsonl_gz(session.sidecar, sidecar_path)
    combos_path.write_bytes(
        (json.dumps(session.combos, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    session.registry.dump(markets_path)

    manifest = {
        "generator_version": GENERATOR_VERSION,
        "model_version": MODEL_VERSION,
        "seed": cfg.seed,
        "config": cfg.to_dict(),
        "base_ts": _iso(session.base_ts),
        "counts": session.counts,
        "source": dict(source or {}),
        "git_commit": _git_commit(),
        "files": {name: _sha256(out / name) for name in sorted(
            ("session.jsonl.gz", "sidecar.jsonl.gz", "combos.json", "markets.json"))},
        "warning": ("Simulated RFQ flow, not recorded Polymarket data. "
                    "sidecar.jsonl.gz is backtest-only: no pricing, risk or "
                    "engine module may read it."),
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_bytes(
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return manifest_path


def load_manifest(dataset_dir: Path | str) -> Dict[str, Any]:
    return json.loads((Path(dataset_dir) / "manifest.json").read_text(encoding="utf-8"))


def load_session(dataset_dir: Path | str) -> Tuple[
        List[Dict[str, Any]], List[Dict[str, Any]], LegRegistry, datetime]:
    """``(items, combos, registry, base_ts)`` for a written dataset.

    The sidecar is deliberately **not** loaded here: only #5's fill model may
    read it, and it opens that file itself.
    """
    directory = Path(dataset_dir)
    manifest = load_manifest(directory)
    with gzip.open(directory / "session.jsonl.gz", "rt", encoding="utf-8") as fh:
        items = [json.loads(line) for line in fh if line.strip()]
    combos = json.loads((directory / "combos.json").read_text(encoding="utf-8"))
    registry = LegRegistry.load(directory / "markets.json")
    return items, combos, registry, _parse_iso(manifest["base_ts"])

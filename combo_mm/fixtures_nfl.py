"""Hand-written NFL fixture week: one deliberate case per RFQ (#15 Part B7).

The scripted session in :mod:`combo_mm.fixtures` is political markets, so it
cannot exercise anything NFL-specific. This module is its NFL counterpart: a
committed four-game Sunday slate with ~40 RFQs where **every request is there
for a reason**, so tests and the dashboard have NFL flow without a data pull
and without the randomness of :mod:`combo_mm.nfl.rfq_sim`. Stdlib only.

The four games are chosen so their settlements cover the awkward cases:

===================  ==============================  =========================
game                 line / result                   what it exercises
===================  ==============================  =========================
``BUF @ KC``         KC -3 (integer), 27-24          spread **pushes** (voids)
``NYJ @ NE``         NE -6.5, 20-20                  ML **tie** (voids)
``SF @ SEA``         SF -2.5, 31-28 OT               **overtime** settle delay
``DAL @ PHI``        PHI -1.5, 24-17                 main total book **missing**
===================  ==============================  =========================

and the request list covers: a nested combo (one leg implies the other), an
impossible combo (fair 0), a pushed leg, an unknown leg symbol, a cross-game
combo, a stale leg book, a cancel, an expiry, duplicate and out-of-order
deliveries, cash sizing, and a mid-slate disconnect.

Decline codes are deliberately *not* asserted here: the correlation-aware
pricer that distinguishes nested from impossible is #2's, and this fixture is
the flow it will be developed against.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from combo_mm.nfl.ingest import Game, kickoff_utc
from combo_mm.nfl.markets import (
    ML,
    SPR,
    TOT,
    TT,
    LegRegistry,
    NflLegMarket,
    game_markets,
    settlement_price,
)

__all__ = [
    "BASE_TS",
    "SELF_USER_ID",
    "SEASON",
    "WEEK",
    "GAMES",
    "UNKNOWN_SYMBOL",
    "build_registry",
    "build_session",
    "leg_mids",
]

BASE_TS = datetime(2025, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
SELF_USER_ID = "maker-001"
SEASON, WEEK = 2025, 1

# A leg the registry cannot resolve: an NFL-shaped request on a market we do
# not model. The pricer must decline rather than guess a price.
UNKNOWN_SYMBOL = "NFL-2025-W01-BUF-KC-PROP-KC-PASSYD-275.5"

_SETTLE_DELAY = timedelta(hours=3, minutes=30)
_OT_EXTRA = timedelta(minutes=15)
_STALE_BOOK_AGE_MS = 5000       # > PipelineConfig.staleness_ms (2000)
_TICK = 0.001


def _game(game_id: str, home: str, away: str, gametime: str, spread: float,
          total: float, home_score: int, away_score: int,
          overtime: bool = False) -> Game:
    """One slate game. ``spread`` is the home expected margin (nflverse)."""
    return Game(
        game_id=game_id, season=SEASON, week=WEEK, game_type="REG",
        gameday="2025-09-07", home=home, away=away,
        home_score=home_score, away_score=away_score, overtime=overtime,
        neutral=False, spread_line=spread, total_line=total, gametime=gametime,
        home_moneyline=-160.0, away_moneyline=140.0,
        home_spread_odds=-110.0, away_spread_odds=-110.0,
        over_odds=-110.0, under_odds=-110.0,
    )


GAMES: List[Game] = [
    # KC -3 and a 3-point win: the spread lands exactly on an integer line.
    _game("2025_01_BUF_KC", "KC", "BUF", "13:00", 3.0, 47.5, 27, 24),
    # A tie: both moneylines void under the default tie rule.
    _game("2025_01_NYJ_NE", "NE", "NYJ", "13:00", 6.5, 41.5, 20, 20),
    # Overtime: settlement arrives 15 minutes later than the rest.
    _game("2025_01_SF_SEA", "SEA", "SF", "16:25", -2.5, 44.5, 31, 28, overtime=True),
    # The main total's book is never published (calibration fallback).
    _game("2025_01_DAL_PHI", "PHI", "DAL", "20:20", 1.5, 45.5, 24, 17),
]

# Hand-picked mids, roughly consistent with each game's lines. Keyed by
# (game_id, leg key) where the keys are the ones _game_legs resolves.
_MIDS: Dict[Tuple[str, str], float] = {
    ("2025_01_BUF_KC", "fav_ml"): 0.62,
    ("2025_01_BUF_KC", "dog_ml"): 0.38,
    ("2025_01_BUF_KC", "fav_spr"): 0.50,
    ("2025_01_BUF_KC", "dog_spr"): 0.50,
    ("2025_01_BUF_KC", "total"): 0.51,
    ("2025_01_NYJ_NE", "fav_ml"): 0.74,
    ("2025_01_NYJ_NE", "dog_ml"): 0.26,
    ("2025_01_NYJ_NE", "fav_spr"): 0.52,
    ("2025_01_NYJ_NE", "dog_spr"): 0.48,
    ("2025_01_NYJ_NE", "total"): 0.47,
    ("2025_01_SF_SEA", "fav_ml"): 0.58,
    ("2025_01_SF_SEA", "dog_ml"): 0.42,
    ("2025_01_SF_SEA", "fav_spr"): 0.49,
    ("2025_01_SF_SEA", "dog_spr"): 0.51,
    ("2025_01_SF_SEA", "total"): 0.53,
    ("2025_01_SF_SEA", "fav_tt"): 0.50,
    ("2025_01_DAL_PHI", "fav_ml"): 0.55,
    ("2025_01_DAL_PHI", "dog_ml"): 0.45,
    ("2025_01_DAL_PHI", "fav_spr"): 0.50,
    ("2025_01_DAL_PHI", "dog_spr"): 0.50,
    ("2025_01_DAL_PHI", "total"): 0.49,
}

# (rfq_id suffix, game index, leg keys, minutes before kickoff, note, extras).
# One row per deliberate case; "extras" carries the case-specific switch.
_REQUESTS: List[Tuple[str, int, Tuple[str, ...], int, str, Dict[str, Any]]] = [
    # -- game 1: KC -3 (integer line), 27-24 -----------------------------
    ("N01", 0, ("fav_ml", "fav_spr"), 180, "nested: KC wins implies nothing, KC -3 implies KC wins", {}),
    ("N02", 0, ("dog_ml", "fav_spr"), 174, "impossible: BUF wins AND KC covers -3", {}),
    ("N03", 0, ("fav_spr", "total"), 168, "pushed leg: KC -3 lands exactly, combo voids", {}),
    ("N04", 0, ("fav_ml", "total"), 162, "duplicate delivery of the create", {"duplicate": True}),
    ("N05", 0, ("dog_ml", "dog_spr"), 156, "nested the other way: BUF wins implies BUF +3", {}),
    ("N06", 0, ("fav_ml", "fav_spr", "total"), 150, "three legs, cash sized", {"cash": "40.00"}),
    ("N07", 0, ("dog_spr", "total"), 144, "stale leg book", {"stale": "dog_spr"}),
    ("N08", 0, (UNKNOWN_SYMBOL, "fav_ml"), 138, "unknown leg symbol: never guess a price", {}),
    ("N09", 0, ("fav_ml",), 132, "single-leg request", {}),
    ("N10", 0, ("fav_spr", "total"), 126, "cancelled before the deadline", {"cancel": True}),

    # -- game 2: NE -6.5, 20-20 tie --------------------------------------
    ("N11", 1, ("fav_ml", "fav_spr"), 180, "tie voids the moneyline leg", {}),
    ("N12", 1, ("dog_ml", "total"), 174, "dog ML + under", {"sides": {"total": "NO"}}),
    ("N13", 1, ("fav_spr", "total"), 168, "favourite covers + over", {}),
    ("N14", 1, ("dog_spr", "total"), 162, "dog covers + under", {"sides": {"total": "NO"}}),
    ("N15", 1, ("fav_ml", "total"), 156, "expired without a trade", {"expire": True}),
    ("N16", 1, ("fav_ml", "dog_spr"), 150, "favourite wins but does not cover", {}),
    ("N17", 1, ("fav_ml", "fav_spr", "total"), 144, "three legs", {}),
    ("N18", 1, ("dog_ml", "dog_spr"), 138, "nested: NYJ wins implies NYJ +6.5", {}),
    ("N19", 1, ("total",), 132, "single total leg", {}),
    ("N20", 1, ("fav_spr",), 126, "single spread leg", {}),

    # -- game 3: SF -2.5, 31-28 in overtime ------------------------------
    ("N21", 2, ("fav_ml", "fav_spr"), 200, "nested", {}),
    ("N22", 2, ("dog_spr", "total"), 194, "SEA +2.5 + over (the overtime path)", {}),
    ("N23", 2, ("fav_ml", "total"), 188, "delivered late: exchange time decides, not delivery time",
     {"late_delivery_ms": 1200}),
    ("N24", 2, ("fav_tt", "total"), 182, "team total + game total", {}),
    ("N25", 2, ("dog_ml", "total"), 176, "SEA wins outright + under", {"sides": {"total": "NO"}}),
    ("N26", 2, ("fav_ml", "dog_spr", "total"), 170, "three legs", {}),
    ("N27", 2, ("fav_spr", "fav_tt"), 164, "spread + team total", {}),
    ("N28", 2, ("dog_ml", "dog_spr"), 158, "nested", {}),
    ("N29", 2, ("total",), 152, "out-of-order: the close arrives before the create",
     {"cash": "25.00", "close_first": True}),
    ("N30", 2, ("fav_ml", "fav_spr", "fav_tt"), 146, "three legs on the favourite", {}),

    # -- game 4: PHI -1.5, 24-17, main total book never published --------
    ("N31", 3, ("fav_ml", "fav_spr"), 240, "main total book missing: calibration fallback", {}),
    ("N32", 3, ("dog_ml", "dog_spr"), 234, "nested, total still missing", {}),
    ("N33", 3, ("fav_spr",), 228, "single spread leg", {}),
    ("N34", 3, ("fav_ml", "dog_spr"), 222, "favourite wins without covering", {}),
    ("N35", 3, ("fav_ml",), 216, "single moneyline leg", {}),
    ("N36", 3, ("dog_ml", "fav_spr"), 210, "impossible", {}),
    ("N37", 3, ("fav_spr", "dog_spr"), 204, "both sides of one spread: impossible", {}),
    ("N38", 3, ("fav_ml", "fav_spr"), 198, "duplicate delivery", {"duplicate": True}),
    ("N39", 3, ("fav_ml", "fav_spr"), 192, "cross-game: a KC leg on a PHI request",
     {"extra_legs": [("2025_01_BUF_KC", "fav_ml")]}),
    ("N40", 3, ("fav_ml", "fav_spr"), 186, "below the minimum size", {"qty": "0"}),
]


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _t(when: datetime) -> int:
    return int((when - BASE_TS).total_seconds() * 1000)


def build_registry() -> LegRegistry:
    """Leg markets for the slate.

    Game 1 keeps its integer spread (``force_half_point_lines=False``) so the
    push case exists at all; the rest snap to half points as a live dataset
    would.
    """
    registry = LegRegistry()
    for index, game in enumerate(GAMES):
        for market in game_markets(game, alt_lines=True,
                                   force_half_point_lines=(index != 0)):
            registry.add(market)
    return registry


def _game_legs(game: Game, registry: LegRegistry) -> Dict[str, NflLegMarket]:
    """Leg keys (``fav_ml``, ``dog_spr``, ``total``, ``fav_tt``) -> market."""
    markets = registry.markets_for_game(game.game_id)
    mains = [m for m in markets if m.is_main_line]
    fav_spr = next(m for m in mains if m.kind == SPR and m.line < 0)
    dog_spr = next(m for m in mains if m.kind == SPR and m.line > 0)
    fav_tt = next((m for m in markets
                   if m.kind == TT and m.subject == fav_spr.subject), None)
    legs = {
        "fav_ml": next(m for m in markets if m.kind == ML and m.subject == fav_spr.subject),
        "dog_ml": next(m for m in markets if m.kind == ML and m.subject == dog_spr.subject),
        "fav_spr": fav_spr,
        "dog_spr": dog_spr,
        "total": next(m for m in mains if m.kind == TOT),
    }
    if fav_tt is not None:
        legs["fav_tt"] = fav_tt
    return legs


def leg_mids() -> Dict[str, float]:
    """Published mid per leg symbol. The missing main total is absent."""
    registry = build_registry()
    out: Dict[str, float] = {}
    for game in GAMES:
        legs = _game_legs(game, registry)
        for key, market in legs.items():
            mid = _MIDS.get((game.game_id, key))
            if mid is None:
                continue
            if game.game_id == "2025_01_DAL_PHI" and key == "total":
                continue        # deliberately unpublished
            out[market.symbol] = mid
    return out


def _book(symbol: str, mid: float, when: datetime, main: bool,
          seq: int) -> Dict[str, Any]:
    half = 0.01 if main else 0.02
    bid = round(max(mid - half, _TICK), 3)
    ask = round(min(mid + half, 1.0 - _TICK), 3)
    return {
        "t": _t(when), "kind": "book", "symbol": symbol, "bid": bid, "ask": ask,
        "bid_size": 2500.0, "ask_size": 2500.0, "seq": seq, "ts": _iso(when),
    }


def _event(event_id: str, event_type: str, rfq_id: str, when: datetime,
           payload: Dict[str, Any], symbol: Optional[str] = None,
           stream: bool = True,
           delivered_at: Optional[datetime] = None) -> Dict[str, Any]:
    """``when`` is the exchange timestamp; ``delivered_at`` the stream slot."""
    raw: Dict[str, Any] = {
        "event_id": event_id, "event_type": event_type, "rfq_id": rfq_id,
        "exchange_ts": _iso(when), "payload": payload,
    }
    if symbol:
        raw["symbol"] = symbol
    return {"t": _t(delivered_at or when), "kind": "event", "stream": stream,
            "raw": raw}


def _combo_symbol(game: Game, legs: List[Dict[str, str]]) -> str:
    prefix = f"NFL-{SEASON}-W{WEEK:02d}-{game.away}-{game.home}"
    parts = []
    for leg in legs:
        symbol = leg["symbol"]
        tail = symbol[len(prefix) + 1:] if symbol.startswith(prefix + "-") else symbol
        parts.append(tail if leg["side"] == "YES" else f"{tail}~NO")
    return f"{prefix}:" + "+".join(parts)


def build_session() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return ``(session_items, combos)`` sorted by ``t``, fixtures.py schema."""
    registry = build_registry()
    mids = leg_mids()
    kickoffs = {game.game_id: datetime.fromisoformat(
        kickoff_utc(game)[0].replace("Z", "+00:00")) for game in GAMES}
    legs_by_game = {game.game_id: _game_legs(game, registry) for game in GAMES}

    items: List[Dict[str, Any]] = []
    combos: Dict[str, Dict[str, Any]] = {}
    settle_rows: Dict[str, List[Tuple[str, str, str]]] = {}
    seq = 0

    for suffix, game_index, keys, minutes, _note, extras in _REQUESTS:
        game = GAMES[game_index]
        kickoff = kickoffs[game.game_id]
        at = kickoff - timedelta(minutes=minutes)
        rfq_id = f"RFQ-{suffix}"
        sides = extras.get("sides", {})

        leg_rows: List[Dict[str, str]] = []
        book_symbols: List[Tuple[str, float, bool, bool]] = []
        for key in keys:
            if key not in legs_by_game[game.game_id]:
                # A raw symbol (the unknown-market case): requested, unpriceable.
                leg_rows.append({"symbol": key, "side": "YES"})
                continue
            market = legs_by_game[game.game_id][key]
            leg_rows.append({"symbol": market.symbol, "side": sides.get(key, "YES")})
            if market.symbol in mids:
                book_symbols.append((market.symbol, mids[market.symbol],
                                     market.is_main_line, key == extras.get("stale")))
        for other_game_id, other_key in extras.get("extra_legs", ()):
            market = legs_by_game[other_game_id][other_key]
            leg_rows.append({"symbol": market.symbol, "side": "YES"})
            if market.symbol in mids:
                book_symbols.append((market.symbol, mids[market.symbol],
                                     market.is_main_line, False))

        combo_symbol = _combo_symbol(game, leg_rows)
        combos.setdefault(combo_symbol, {
            "symbol": combo_symbol, "tick_size": _TICK, "price_min": 0.001,
            "price_max": 0.999, "min_qty": 1.0, "legs": leg_rows,
        })

        for symbol, mid, main, stale in book_symbols:
            seq += 1
            age_ms = _STALE_BOOK_AGE_MS if stale else 500
            items.append(_book(symbol, mid, at - timedelta(milliseconds=age_ms),
                               main, seq))

        size: Dict[str, Any] = ({"cashOrderQty": extras["cash"]} if "cash" in extras
                                else {"qtyDecimal": extras.get("qty", "100")})
        # Delivery slot vs exchange timestamp. The stream gives no ordering
        # guarantee, so these come apart: `created_at` is when the stream
        # handed us the event, while the payload keeps the true exchange time
        # that every staleness and ordering decision must use.
        closed_at = at + timedelta(seconds=3)
        created_at = at + timedelta(milliseconds=extras.get("late_delivery_ms", 0))
        if extras.get("close_first"):
            created_at = closed_at + timedelta(seconds=1)
        payload = {
            "id": rfq_id, "symbol": combo_symbol,
            "rfqCreatorUserId": f"taker-{suffix}", "createdTime": _iso(at),
            "updatedTime": _iso(at), "restRemainder": False, "status": "OPEN",
            "comboLegs": leg_rows,
            "submissionDeadline": _iso(at + timedelta(seconds=3)), **size,
        }
        items.append(_event(f"{rfq_id}:created", "rfq_created", rfq_id, at,
                            payload, symbol=combo_symbol,
                            delivered_at=created_at))
        if extras.get("duplicate"):
            items.append(_event(f"{rfq_id}:created", "rfq_created", rfq_id, at,
                                payload, symbol=combo_symbol,
                                delivered_at=at + timedelta(milliseconds=120)))

        if extras.get("cancel"):
            cancelled_at = at + timedelta(seconds=1)
            items.append(_event(f"{rfq_id}:cancelled", "rfq_cancelled", rfq_id,
                                cancelled_at,
                                {"id": rfq_id, "status": "CANCELLED",
                                 "updatedTime": _iso(cancelled_at)}))
            continue        # cancelled RFQs never settle
        if extras.get("expire"):
            expired_at = at + timedelta(seconds=3)
            items.append(_event(f"{rfq_id}:expired", "rfq_expired", rfq_id,
                                expired_at,
                                {"id": rfq_id, "status": "EXPIRED",
                                 "updatedTime": _iso(expired_at)}))
            continue
        items.append(_event(f"{rfq_id}:closed", "rfq_closed", rfq_id, closed_at,
                            {"id": rfq_id, "status": "CLOSED",
                             "updatedTime": _iso(closed_at)}))
        settle_rows[rfq_id] = [(leg["symbol"], leg["side"], game.game_id)
                               for leg in leg_rows]

    # Mid-slate disconnect, between the second and third game's windows.
    items.append({"t": _t(kickoffs["2025_01_NYJ_NE"] + timedelta(minutes=30)),
                  "kind": "disconnect"})

    # Settlements: stream-invisible, after each game ends (later for overtime).
    for rfq_id, rows in settle_rows.items():
        game_ids = {game_id for _, _, game_id in rows}
        # A cross-game combo settles once every game it touches has finished.
        touched = [g for g in GAMES if g.game_id in game_ids]
        last = max(touched, key=lambda g: kickoffs[g.game_id]
                   + (_OT_EXTRA if g.overtime else timedelta(0)))
        settled_at = (kickoffs[last.game_id] + _SETTLE_DELAY
                      + (_OT_EXTRA if last.overtime else timedelta(0)))
        legs_out = []
        for symbol, side, game_id in rows:
            market = registry.get(symbol)
            row: Dict[str, Any] = {"symbol": symbol, "side": side}
            if market is not None:
                game = next(g for g in GAMES if g.game_id == market.game_id)
                price = settlement_price(market, game.home_score, game.away_score)
                if price is not None:
                    row["settlementPrice"] = price
            legs_out.append(row)
        items.append(_event(f"{rfq_id}:settled", "rfq_updated", rfq_id, settled_at,
                            {"id": rfq_id, "updatedTime": _iso(settled_at),
                             "comboLegs": legs_out}, stream=False))

    items.sort(key=lambda i: (i["t"], i["kind"], i.get("symbol", "")))
    return items, [combos[s] for s in sorted(combos)]

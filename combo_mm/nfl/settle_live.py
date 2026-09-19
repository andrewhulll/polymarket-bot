"""Settling priced live NFL same-game quotes against final scores.

Pure functions only: no file reads, no network, no clock. The runner
(``scripts/settle_live_quotes.py``) loads the priced quotes, the combo
catalog cache and the cached nflverse pull, then feeds them to
:func:`settle_quote`. Tests hit these directly.

Conventions (all from the live code, restated here for auditability):

- Leg identity -> market meaning: ``catalog_markets.parse_catalog_leg``
  maps ``(slug, outcome_index)`` from the combo catalog to
  ``(NflLegMarket, side)``.
- Leg settlement: ``markets.settlement_price`` with the default
  ``push_rule="void"`` -- the raw YES result (``"1"`` / ``"0"``), never
  inverted; ``None`` means the leg voids (a push), which voids the whole
  combo.
- Combo fold: any void leg voids the combo; otherwise ``1.0`` when every
  leg wins, else ``0.0`` (the backtest's convention in
  ``week_backtest.py``).
- The counterfactual edges (``hypo_edge_bid`` / ``hypo_edge_ask``) are NOT
  P&L: no live quote was ever submitted, so nothing was ever at risk.
  Only an ``accepted_quotes`` row (the pick-to-quote flow) supports a
  realized-P&L figure, and it is computed separately, never here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from combo_mm.nfl.catalog_markets import (
    parse_catalog_leg,
    parse_game_slug,
    season_of,
    unsupported_reason,
)
from combo_mm.nfl.ingest import FRANCHISE_MAP, Game
from combo_mm.nfl.markets import PUSH_VOID, settlement_price

__all__ = [
    "LegResolution",
    "GameJoin",
    "LegSettlement",
    "resolve_leg",
    "join_game",
    "settle_leg_against",
    "fold_combo",
    "settlement_metrics",
    "settle_quote",
]

FOUND, PENDING, UNRESOLVED = "found", "pending", "unresolved"

_QUOTE_STATUSES = ("SETTLED", "VOID", "PENDING", "UNRESOLVED")


# ---------------------------------------------------------------------------
# Leg resolution through the catalog cache
# ---------------------------------------------------------------------------


def _norm_team(code: Optional[str]) -> str:
    """Franchise code with relocations mapped (OAK->LV, SD->LAC, STL->LA)."""
    code = (code or "").upper()
    return FRANCHISE_MAP.get(code, code)


@dataclass(frozen=True)
class LegResolution:
    """A detail leg joined to its catalog market: what the slug means."""

    position_id: str
    resolved: bool
    slug: str = ""
    outcome_index: int = 0
    game_key: str = ""
    away: str = ""          # franchise-normalized, upper case
    home: str = ""          # franchise-normalized, upper case
    date: str = ""          # slug kickoff date (YYYY-MM-DD)
    reason: str = ""        # set when not resolved


def resolve_leg(position_id: str,
                catalog_index: Dict[str, Tuple[str, int]]) -> LegResolution:
    """Resolve a priced leg's ``position_id`` to the game it belongs to.

    ``catalog_index`` maps ``position_id`` -> ``(slug, outcome_index)`` and
    comes from the combo catalog cache -- the only catalog copy; no second
    mapping is consulted.
    """
    pid = str(position_id or "")
    entry = catalog_index.get(pid)
    if entry is None:
        return LegResolution(position_id=pid, resolved=False,
                             reason=f"position_id {pid[:24]}... not in the combo catalog")
    slug, outcome_index = entry
    parsed = parse_game_slug(slug)
    if parsed is None:
        return LegResolution(position_id=pid, resolved=False, slug=slug,
                             outcome_index=outcome_index,
                             reason=f"slug {slug!r} is not an NFL game market")
    game_key, away, home, game_date, _suffix = parsed
    return LegResolution(position_id=pid, resolved=True, slug=slug,
                         outcome_index=outcome_index, game_key=game_key,
                         away=_norm_team(away), home=_norm_team(home),
                         date=game_date)


# ---------------------------------------------------------------------------
# The score join: game slug -> cached nflverse game
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameJoin:
    """One leg's join to a cached game."""

    status: str                      # "found" | "pending" | "unresolved"
    game: Optional[Game] = None
    reason: str = ""


def _iso(d: str) -> date:
    return date(int(d[0:4]), int(d[5:7]), int(d[8:10]))


def join_game(resolution: LegResolution, games: List[Game]) -> GameJoin:
    """Join a resolved leg to exactly one cached game.

    Join key is ``(season, away, home)`` with franchise normalization on
    both sides; the slug kickoff date and nflverse ``gameday`` may drift by
    up to a day (slug dates are rendered in whatever timezone produced them,
    ``gameday`` is ET). Zero candidates or two-or-more candidates are
    UNRESOLVED -- never a guess. A single candidate that has not kicked off
    is PENDING, never a loss.
    """
    season = season_of(resolution.date)
    slug_day = _iso(resolution.date)
    candidates = [
        g for g in games
        if g.season == season
        and _norm_team(g.away) == resolution.away
        and _norm_team(g.home) == resolution.home
        and abs((_iso(g.gameday) - slug_day).days) <= 1
    ]
    if not candidates:
        return GameJoin(UNRESOLVED, reason=(
            f"no game in the cached pull matches {resolution.away}/{resolution.home} "
            f"season {season} within ±1 day of {resolution.date}"))
    if len(candidates) > 1:
        ids = ", ".join(g.game_id for g in candidates)
        return GameJoin(UNRESOLVED, reason=(
            f"ambiguous: {len(candidates)} candidate games match "
            f"{resolution.away}/{resolution.home} {resolution.date}: {ids}"))
    game = candidates[0]
    if not game.played:
        return GameJoin(PENDING, game=game,
                        reason=f"game {game.game_id} not played yet")
    return GameJoin(FOUND, game=game)


# ---------------------------------------------------------------------------
# Leg settlement against the final score
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegSettlement:
    """A leg settled against the final score of the game it joined to."""

    position_id: str
    side: str = ""                            # what this position pays on: YES/NO
    settlement_price: Optional[str] = None    # raw YES result "1"/"0"; None when void
    won: Optional[bool] = None
    void: bool = False
    game_id: str = ""
    reason: str = ""


def settle_leg_against(resolution: LegResolution, game: Game,
                       *, push_rule: str = PUSH_VOID) -> LegSettlement:
    """Settle one leg: catalog meaning + final score -> win/loss/void.

    The YES/NO inversion happens here (and only here): the raw value from
    ``settlement_price`` is the YES result, so a leg whose side is ``"NO"``
    wins when the raw value is ``"0"``.
    """
    parsed = parse_catalog_leg(resolution.slug, resolution.outcome_index)
    if parsed is None:
        why = unsupported_reason(resolution.slug) or "unparsed NFL market"
        return LegSettlement(position_id=resolution.position_id,
                             game_id=game.game_id, reason=why)
    market, side = parsed
    raw = settlement_price(market, game.home_score, game.away_score,
                           push_rule=push_rule)
    if raw is None:
        return LegSettlement(position_id=resolution.position_id, side=side,
                             void=True, game_id=game.game_id,
                             reason="leg pushed (voids the combo)")
    if raw == "1":
        return LegSettlement(position_id=resolution.position_id, side=side,
                             settlement_price="1", won=side == "YES",
                             game_id=game.game_id)
    if raw == "0":
        return LegSettlement(position_id=resolution.position_id, side=side,
                             settlement_price="0", won=side == "NO",
                             game_id=game.game_id)
    # "0.5" only arises under half push/tie rules; settle_live runs with
    # void rules, so treat it as a void defensively rather than a win.
    return LegSettlement(position_id=resolution.position_id, side=side,
                         void=True, game_id=game.game_id,
                         reason=f"half settlement {raw!r} treated as void")


def fold_combo(legs: List[LegSettlement]) -> Tuple[str, Optional[float]]:
    """Combo fold: any void leg voids the combo; else 1.0 iff all legs won."""
    if any(leg.void for leg in legs):
        return "VOID", None
    return "SETTLED", 1.0 if all(leg.won for leg in legs) else 0.0


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def settlement_metrics(*, fair: Optional[float], naive: Optional[float],
                       combo_value: Optional[float],
                       bid: Optional[float], ask: Optional[float]
                       ) -> Dict[str, Optional[float]]:
    """Brier pair + counterfactual edges for a settled combo.

    ``brier`` / ``naive_brier`` are the headline pair: the only
    fill-free answer to whether the correlation adjustment improved the
    forecast. ``hypo_edge_bid`` / ``hypo_edge_ask`` are counterfactual --
    what one unit would have returned had the quote we never sent been
    lifted -- and must never be labelled P&L. All are None for a VOID
    combo: no counterfactual edge is recorded for a void.
    """
    if combo_value is None:
        return {"brier": None, "naive_brier": None, "edge_vs_naive": None,
                "hypo_edge_bid": None, "hypo_edge_ask": None}
    brier = (fair - combo_value) ** 2 if fair is not None else None
    naive_brier = (naive - combo_value) ** 2 if naive is not None else None
    edge_vs_naive = (naive_brier - brier
                     if brier is not None and naive_brier is not None else None)
    return {
        "brier": brier,
        "naive_brier": naive_brier,
        "edge_vs_naive": edge_vs_naive,          # > 0 means the joint model won
        "hypo_edge_bid": combo_value - bid if bid is not None else None,
        "hypo_edge_ask": ask - combo_value if ask is not None else None,
    }


# ---------------------------------------------------------------------------
# One priced quote -> one settlement row
# ---------------------------------------------------------------------------


def settle_quote(*, rfq_id: str, trigger: str,
                 legs: List[Dict[str, Any]],
                 fair: Optional[float], naive: Optional[float],
                 bid: Optional[float], ask: Optional[float],
                 model_version: Optional[str], params_version: Optional[str],
                 catalog_index: Dict[str, Tuple[str, int]],
                 games: List[Game], scores_vintage: str,
                 push_rule: str = PUSH_VOID) -> Dict[str, Any]:
    """Settle one priced quote to a ``quote_settlements`` row.

    Status precedence: any unresolvable leg -> UNRESOLVED; any void leg ->
    VOID; any game not yet played -> PENDING; otherwise SETTLED. Re-running
    after a newer nflverse pull is the normal path for flipping
    PENDING/UNRESOLVED -> SETTLED/VOID.
    """
    settlements: List[LegSettlement] = []
    problems: List[Tuple[str, str]] = []   # (position_id, reason) for UNRESOLVED legs

    for leg in legs:
        pid = str((leg or {}).get("position_id") or "")
        if not pid:
            problems.append(("", "leg has no position_id"))
            settlements.append(LegSettlement(position_id="", reason="leg has no position_id"))
            continue
        res = resolve_leg(pid, catalog_index)
        if not res.resolved:
            problems.append((pid, res.reason))
            settlements.append(LegSettlement(position_id=pid, reason=res.reason))
            continue
        join = join_game(res, games)
        if join.status == UNRESOLVED:
            problems.append((pid, join.reason))
            settlements.append(LegSettlement(position_id=pid, reason=join.reason))
            continue
        game = join.game
        assert game is not None
        if join.status == PENDING:
            settlements.append(LegSettlement(position_id=pid, game_id=game.game_id,
                                             reason=join.reason))
            continue
        ls = settle_leg_against(res, game, push_rule=push_rule)
        if ls.reason and not ls.void:
            # Joined a game but the market cannot be settled from a final
            # score (e.g. a period market): UNRESOLVED, never a guess.
            problems.append((pid, ls.reason))
        settlements.append(ls)

    return _row(rfq_id=rfq_id, trigger=trigger, settlements=settlements,
                unresolved=dict(problems), fair=fair, naive=naive,
                bid=bid, ask=ask, model_version=model_version,
                params_version=params_version, scores_vintage=scores_vintage)


def _row(*, rfq_id: str, trigger: str, settlements: List[LegSettlement],
         unresolved: Dict[str, str],
         fair: Optional[float], naive: Optional[float],
         bid: Optional[float], ask: Optional[float],
         model_version: Optional[str], params_version: Optional[str],
         scores_vintage: str) -> Dict[str, Any]:
    leg_unresolved = [s for s in settlements if s.position_id in unresolved]
    pending = [s for s in settlements
               if s.position_id not in unresolved and s.reason
               and s.settlement_price is None and not s.void]
    void = [s for s in settlements if s.void]
    n_legs = len(settlements)
    n_settled = sum(1 for s in settlements if s.settlement_price in ("1", "0"))
    legs_json = [{"position_id": s.position_id, "settlement_price": s.settlement_price,
                  "game_id": s.game_id, "side": s.side or None} for s in settlements]

    if leg_unresolved:
        status = "UNRESOLVED"
        reason_detail = "; ".join(f"{pid or '?'}: {why}" for pid, why in unresolved.items())
        combo_value: Optional[float] = None
    elif void:
        status = "VOID"
        reason_detail = "; ".join(f"{s.position_id}: {s.reason}" for s in void)
        combo_value = None
    elif pending:
        status = "PENDING"
        reason_detail = "; ".join(f"{s.position_id}: {s.reason}" for s in pending)
        combo_value = None
    else:
        status, combo_value = fold_combo(settlements)
        reason_detail = None
    if status == "SETTLED":
        metrics = settlement_metrics(fair=fair, naive=naive, combo_value=combo_value,
                                     bid=bid, ask=ask)
    else:
        # VOID/PENDING/UNRESOLVED: combo_value is NULL and no counterfactual
        # edge is recorded -- nothing was ever at risk.
        metrics = settlement_metrics(fair=None, naive=None, combo_value=None,
                                     bid=None, ask=None)
    assert status in _QUOTE_STATUSES
    return {
        "rfq_id": rfq_id, "trigger": trigger, "status": status,
        "reason_detail": reason_detail, "combo_value": combo_value,
        "n_legs": n_legs, "n_legs_settled": n_settled, "legs": legs_json,
        "fair": fair, "bid": bid, "ask": ask, "naive": naive,
        "brier": metrics["brier"], "naive_brier": metrics["naive_brier"],
        "edge_vs_naive": metrics["edge_vs_naive"],
        "hypo_edge_bid": metrics["hypo_edge_bid"],
        "hypo_edge_ask": metrics["hypo_edge_ask"],
        "scores_vintage": scores_vintage,
        "model_version": model_version, "params_version": params_version,
    }

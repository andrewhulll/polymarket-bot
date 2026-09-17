"""Settle stored live quotes against final scores (plan: ``docs/settlement-tracking.md``).

The live quoter records what the NFL correlation model *would* have quoted;
this module answers whether it was right. It is pure: no network, no sqlite, no
clock beyond what the caller passes, so every rule below is directly testable.

The pipeline for one stored quote:

1. Each stored leg carries a ``position_id``. A resolver (the combo catalog,
   loaded from its on-disk cache) maps that to a :class:`LegMarket`, whose slug
   and outcome index :func:`~combo_mm.nfl.catalog_markets.parse_catalog_leg`
   turns into an :class:`~combo_mm.nfl.markets.NflLegMarket` plus the side the
   position pays on.
2. The leg's game is matched to an nflverse :class:`~combo_mm.nfl.ingest.Game`
   on ``(season, away, home)``, allowing a day of kickoff-date drift.
3. :func:`~combo_mm.nfl.markets.settlement_price` settles the leg. That value
   is the raw YES wire value, so a position on the NO side is inverted here.
4. The legs fold into a combo value with the convention the backtest already
   uses (``combo_mm/nfl/week_backtest.py``): **any void leg voids the whole
   combo**; otherwise the combo pays 1 only if every leg pays 1.

Statuses, and the order they win in:

``VOID``
    A leg voided (a push under ``push_rule="void"``, or an ML tie). Void is
    absorbing -- one void leg voids the combo whatever the other legs did, so
    this outranks not-yet-known legs.
``UNSETTLEABLE``
    A leg cannot be settled from a final score at all -- a player prop, a
    half/quarter market, or a non-NFL leg the screen allowed through as an
    independent multiplier. The screen only requires an NFL same-game block;
    other legs ride along, and roughly a fifth of live quotable RFQs carry
    one. No future pull will settle these, so the status is terminal and the
    job stops retrying them.
``UNRESOLVED``
    A leg could not be mapped to a market or to a game *yet* -- an unknown
    position id, or a game missing from the pull. Retried, because the catalog
    grows and pulls refresh. Never guessed.
``PENDING``
    Every leg resolved, but a game has not been played yet. Explicitly not a
    loss: a quote priced on Thursday must not settle to 0 because the job ran
    before kickoff.
``SETTLED``
    Every leg settled. ``combo_value`` is 1.0 or 0.0.

Scoring is deliberately fill-free. Nothing here was traded -- ``LiveQuoter``
has no submit path -- so the headline metric is the Brier pair (model against
the naive independent-leg price) and the bid/ask figures are labelled
``hypo_`` because they are counterfactual.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from combo_mm.nfl.catalog_markets import parse_catalog_leg, unsupported_reason
from combo_mm.nfl.ingest import FRANCHISE_MAP, Game
from combo_mm.nfl.markets import PUSH_VOID, NflLegMarket, settlement_price

__all__ = [
    "PENDING",
    "SETTLED",
    "UNRESOLVED",
    "UNSETTLEABLE",
    "VOID",
    "GameIndex",
    "LegSettlement",
    "QuoteSettlement",
    "find_game",
    "settle_quote",
]

SETTLED, VOID, PENDING = "SETTLED", "VOID", "PENDING"
UNRESOLVED, UNSETTLEABLE = "UNRESOLVED", "UNSETTLEABLE"

# Statuses a re-run can never change, so the job stops considering them.
TERMINAL = (SETTLED, VOID, UNSETTLEABLE)

# How many days the slug's kickoff date may differ from nflverse's ET gameday.
# A late kickoff crosses midnight UTC, so the slug can name the following day.
_DATE_TOLERANCE_DAYS = 1


def _norm_team(code: Optional[str]) -> str:
    code = (code or "").strip().upper()
    return FRANCHISE_MAP.get(code, code)


def _parse_date(value: str) -> Optional[date]:
    try:
        return date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except (TypeError, ValueError, IndexError):
        return None


class GameIndex:
    """``(season, away, home)`` -> games, for joining a leg to its final score."""

    def __init__(self, games: Iterable[Game]) -> None:
        self._by_key: Dict[Tuple[int, str, str], List[Game]] = {}
        for game in games:
            key = (int(game.season), _norm_team(game.away), _norm_team(game.home))
            self._by_key.setdefault(key, []).append(game)

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_key.values())

    def candidates(self, season: int, away: str, home: str) -> List[Game]:
        return list(self._by_key.get((int(season), _norm_team(away), _norm_team(home)), ()))


def find_game(index: GameIndex, market: NflLegMarket) -> Tuple[Optional[Game], str]:
    """``(game, reason)`` for one leg. ``reason`` is empty when a game matched.

    Matches on season and the team pair, then narrows by kickoff date within
    :data:`_DATE_TOLERANCE_DAYS`. Two candidates within tolerance is an
    ambiguity we refuse to guess at.
    """
    candidates = index.candidates(market.season, market.away, market.home)
    if not candidates:
        return None, (f"no {market.season} game {market.away} at {market.home} in the pull")
    want = _parse_date(market.kickoff_utc)
    if want is None:
        return (candidates[0], "") if len(candidates) == 1 else (
            None, f"{len(candidates)} games for {market.away} at {market.home}, no usable date")
    near = [g for g in candidates
            if (d := _parse_date(g.gameday)) is not None
            and abs((d - want).days) <= _DATE_TOLERANCE_DAYS]
    if len(near) == 1:
        return near[0], ""
    if not near:
        return None, (f"{market.away} at {market.home} found, but no kickoff within "
                      f"{_DATE_TOLERANCE_DAYS}d of {want.isoformat()}")
    return None, (f"{len(near)} candidate games for {market.away} at {market.home} "
                  f"near {want.isoformat()}; refusing to guess")


@dataclass(frozen=True)
class LegSettlement:
    """One leg's outcome, in the terms of the position the RFQ actually held."""

    position_id: str
    status: str
    slug: Optional[str] = None
    game_id: Optional[str] = None
    side: Optional[str] = None                  # YES | NO -- the position's side
    label: Optional[str] = None
    settlement_price: Optional[str] = None      # raw YES wire value ("1"/"0"/"0.5")
    value: Optional[float] = None               # after NO inversion; None when void
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"position_id": self.position_id, "status": self.status, "slug": self.slug,
                "game_id": self.game_id, "side": self.side, "label": self.label,
                "settlement_price": self.settlement_price, "value": self.value,
                "detail": self.detail}


@dataclass(frozen=True)
class QuoteSettlement:
    """What one stored quote turned out to be worth, and how the model scored."""

    rfq_id: str
    trigger: str
    status: str
    settled_at: str
    reason_detail: str = ""
    combo_value: Optional[float] = None         # requested side, 1.0 / 0.0
    combo_yes: Optional[float] = None           # combo's own YES, before side inversion
    n_legs: int = 0
    n_legs_settled: int = 0
    legs: List[LegSettlement] = field(default_factory=list)
    side: str = "YES"
    fair: Optional[float] = None
    naive: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    brier: Optional[float] = None
    naive_brier: Optional[float] = None
    edge_vs_naive: Optional[float] = None
    hypo_edge_bid: Optional[float] = None
    hypo_edge_ask: Optional[float] = None
    realized_pnl: Optional[float] = None        # only where an accepted fill exists
    scores_vintage: Optional[str] = None
    model_version: Optional[str] = None
    params_version: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        """Re-running the job can never change this row."""
        return self.status in TERMINAL

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["legs"] = [leg.to_dict() for leg in self.legs]
        return d


def _settle_one_leg(stored: Dict[str, Any], resolve: Callable[[str], Optional[Any]],
                    index: GameIndex, push_rule: str) -> LegSettlement:
    position_id = str(stored.get("position_id") or "")
    if not position_id:
        return LegSettlement(position_id="", status=UNRESOLVED, detail="leg has no position id")
    label = stored.get("label")
    market_row = resolve(position_id)
    if market_row is None:
        return LegSettlement(position_id=position_id, status=UNRESOLVED, label=label,
                             slug=stored.get("slug"),
                             detail=f"position {position_id} is not in the catalog cache")
    parsed = parse_catalog_leg(market_row.slug, market_row.outcome_index)
    if parsed is None:
        # Known market, but no final score settles it: a prop, a period market,
        # or a non-NFL leg the screen let through as an independent multiplier.
        why = unsupported_reason(market_row.slug) or "not a full-game NFL leg"
        return LegSettlement(position_id=position_id, status=UNSETTLEABLE, label=label,
                             slug=market_row.slug, detail=f"{market_row.slug}: {why}")
    market, side = parsed
    game, reason = find_game(index, market)
    if game is None:
        return LegSettlement(position_id=position_id, status=UNRESOLVED, label=label,
                             slug=market_row.slug, side=side, detail=reason)
    if not game.played:
        return LegSettlement(position_id=position_id, status=PENDING, label=label,
                             slug=market_row.slug, game_id=game.game_id, side=side,
                             detail=f"{game.game_id} has no final score yet")
    price = settlement_price(market, int(game.home_score), int(game.away_score),
                             push_rule=push_rule)
    if price is None:
        return LegSettlement(position_id=position_id, status=VOID, label=label,
                             slug=market_row.slug, game_id=game.game_id, side=side,
                             detail="leg pushed (voids the combo)")
    value = float(price) if side == "YES" else 1.0 - float(price)
    return LegSettlement(position_id=position_id, status=SETTLED, label=label,
                         slug=market_row.slug, game_id=game.game_id, side=side,
                         settlement_price=price, value=value)


def _fold(legs: Sequence[LegSettlement]) -> Tuple[str, Optional[float], str]:
    """``(status, combo_yes, reason)`` -- void is absorbing, then unknowns, then the product.

    A leg we cannot settle blocks the combo even when another leg already lost:
    that unknown leg could itself void, and a void combo is not a losing combo.
    Only a void is absorbing, which is the backtest's convention.
    """
    for status in (VOID, UNSETTLEABLE, UNRESOLVED, PENDING):
        blocking = [leg for leg in legs if leg.status == status]
        if blocking:
            return status, None, blocking[0].detail or f"a leg is {status}"
    won = all((leg.value or 0.0) >= 1.0 for leg in legs)
    return SETTLED, (1.0 if won else 0.0), ""


def settle_quote(quote: Dict[str, Any], resolve: Callable[[str], Optional[Any]],
                 index: GameIndex, *, settled_at: str, push_rule: str = PUSH_VOID,
                 scores_vintage: Optional[str] = None,
                 fill: Optional[Dict[str, Any]] = None) -> QuoteSettlement:
    """Settle one row from ``priced_quotes`` (as :meth:`list_priced_quotes` returns it).

    ``resolve`` maps a position id to a catalog ``LegMarket`` (or None);
    ``index`` holds the games to settle against. ``fill`` is an optional
    ``accepted_quotes`` row, which is the only case where realized P&L means
    anything -- everything else here is counterfactual.
    """
    rfq_id = str(quote.get("rfq_id") or "")
    trigger = str(quote.get("trigger") or "auto")
    detail = quote.get("detail") or {}
    stored_legs = list(detail.get("legs") or [])
    side = str(quote.get("side") or detail.get("side") or "YES").upper()
    fair, naive = _num(quote.get("fair")), _num(quote.get("naive"))
    bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
    common = dict(rfq_id=rfq_id, trigger=trigger, settled_at=settled_at, side=side,
                  fair=fair, naive=naive, bid=bid, ask=ask, scores_vintage=scores_vintage,
                  model_version=quote.get("model_version"),
                  params_version=quote.get("params_version"))

    if not stored_legs:
        return QuoteSettlement(status=UNRESOLVED, reason_detail="quote stored no legs", **common)

    legs = [_settle_one_leg(leg, resolve, index, push_rule) for leg in stored_legs]
    status, combo_yes, reason = _fold(legs)
    n_settled = sum(1 for leg in legs if leg.status == SETTLED)
    out = QuoteSettlement(status=status, reason_detail=reason, combo_yes=combo_yes,
                          n_legs=len(legs), n_legs_settled=n_settled, legs=legs, **common)
    if status != SETTLED or combo_yes is None:
        return out

    realized = combo_yes if side == "YES" else 1.0 - combo_yes
    brier = None if fair is None else round((fair - realized) ** 2, 8)
    naive_brier = None if naive is None else round((naive - realized) ** 2, 8)
    edge = (None if brier is None or naive_brier is None
            else round(naive_brier - brier, 8))
    realized_pnl = None
    if fill is not None:
        price, qty = _num(fill.get("price")), _num(fill.get("size"))
        if price is not None and qty is not None:
            sign = 1.0 if str(fill.get("direction") or "BUY").upper() == "BUY" else -1.0
            realized_pnl = round(sign * (realized - price) * qty, 8)
    return QuoteSettlement(
        status=SETTLED, reason_detail="", combo_value=realized, combo_yes=combo_yes,
        n_legs=len(legs), n_legs_settled=n_settled, legs=legs,
        brier=brier, naive_brier=naive_brier, edge_vs_naive=edge,
        hypo_edge_bid=None if bid is None else round(realized - bid, 8),
        hypo_edge_ask=None if ask is None else round(ask - realized, 8),
        realized_pnl=realized_pnl, **common)


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

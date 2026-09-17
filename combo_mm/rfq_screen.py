"""Screen live combo RFQs for the ones we quote right now.

Rule (NFL same-game correlation is the only correlation we price today):

- At least one NFL game contributes two or more legs -- the correlated
  same-game block the NFL joint model prices.
- No other sports game contributes two or more legs: a same-game soccer
  (or any non-NFL) block would need a correlation model we do not have.
- Any other leg (a single leg from another game, NFL or not, or a non-game
  market such as politics) is priced as independent and multiplied in.
- Every leg must resolve in the combo catalog; until then the RFQ is
  ``UNRESOLVED`` (an unknown leg could hide a second same-game leg).

``rank`` orders the dashboard's default view: quotable first, then RFQs with
any NFL leg, then everything else.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from combo_mm.combo_markets import LegMarket

__all__ = [
    "NO_NFL_SAME_GAME",
    "OTHER_SAME_GAME",
    "QUOTABLE",
    "RfqScreen",
    "UNRESOLVED",
    "screen_legs",
]

QUOTABLE = "QUOTABLE"
UNRESOLVED = "UNRESOLVED"
OTHER_SAME_GAME = "OTHER_SAME_GAME"
NO_NFL_SAME_GAME = "NO_NFL_SAME_GAME"

RANK_QUOTABLE, RANK_HAS_NFL, RANK_OTHER = 0, 1, 2


@dataclass(frozen=True)
class RfqScreen:
    screen: str
    rank: int
    n_legs: int
    n_resolved: int
    n_nfl_legs: int
    nfl_same_games: Dict[str, int] = field(default_factory=dict)
    other_same_games: Dict[str, int] = field(default_factory=dict)

    @property
    def quotable(self) -> bool:
        return self.screen == QUOTABLE


def screen_legs(legs: Sequence[Optional[LegMarket]]) -> RfqScreen:
    """Classify one RFQ from its resolved legs (None = not in the catalog)."""
    resolved: List[LegMarket] = [leg for leg in legs if leg is not None]
    per_game = Counter(leg.game for leg in resolved if leg.game)
    nfl_games = {leg.game for leg in resolved if leg.is_nfl and leg.game}
    nfl_same = {g: n for g, n in per_game.items() if n >= 2 and g in nfl_games}
    other_same = {g: n for g, n in per_game.items() if n >= 2 and g not in nfl_games}
    n_nfl = sum(1 for leg in resolved if leg.is_nfl)

    if other_same:
        screen = OTHER_SAME_GAME
    elif len(resolved) < len(legs):
        screen = UNRESOLVED
    elif nfl_same:
        screen = QUOTABLE
    else:
        screen = NO_NFL_SAME_GAME
    rank = RANK_QUOTABLE if screen == QUOTABLE else (RANK_HAS_NFL if n_nfl else RANK_OTHER)
    return RfqScreen(screen=screen, rank=rank, n_legs=len(legs), n_resolved=len(resolved),
                     n_nfl_legs=n_nfl, nfl_same_games=nfl_same, other_same_games=other_same)

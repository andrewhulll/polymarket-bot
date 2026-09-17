"""NFL leg-market registry: live Polymarket combo legs -> canonical score legs (issue #15).

The quoter gateway names each RFQ leg by its on-chain position id; the combo
catalog (:mod:`combo_mm.combo_markets`) resolves that to a market ``slug``,
its ``outcomes`` and the leg's ``outcome_index``. This module parses the
slug grammar Polymarket uses for full-game NFL markets and maps the leg to a
:class:`combo_mm.nfl.joint.Leg` the joint model can price.

Slug grammar (verified against the live catalog, 2026-09-17; the slug lists
the **away** team first)::

    nfl-{away}-{home}-{yyyy-mm-dd}                     moneyline   outcomes [away, home]
    nfl-{away}-{home}-{date}-spread-home-{x}pt{y}      home -x.y   outcomes [home, away]
    nfl-{away}-{home}-{date}-spread-away-{x}pt{y}      away -x.y   outcomes [away, home]
    nfl-{away}-{home}-{date}-total-{x}pt{y}            game O/U    outcomes [Over, Under]
    nfl-{away}-{home}-{date}-team-total-{team}-{x}pt{y}  team O/U  outcomes [Over, Under]

Everything else on an NFL game -- first-half / quarter markets (``1h-``,
``1q-``), player props, exact margin, first touchdown -- is **not** a
function of the final score pair and is reported as unsupported (Phase B).

Mapping to the joint model (``x`` > 0 is the favourite's handicap; the joint
model's spread line is the home expected margin):

=====================  ========  =============================
market                 outcome   joint leg
=====================  ========  =============================
moneyline              0 (away)  ``away_ml()``
moneyline              1 (home)  ``home_ml()``
spread-home-x          0 (home)  ``home_cover(+x)``  (home margin > x)
spread-home-x          1 (away)  ``away_cover(+x)``  (home margin < x)
spread-away-x          0 (away)  ``away_cover(-x)``  (home margin < -x)
spread-away-x          1 (home)  ``home_cover(-x)``  (home margin > -x)
total-x                0 / 1     ``over(x)`` / ``under(x)``
team-total-{home}-x    0 / 1     ``home_team_over(x)`` / ``home_team_under(x)``
team-total-{away}-x    0 / 1     ``away_team_over(x)`` / ``away_team_under(x)``
=====================  ========  =============================

Settlement semantics are the joint model's: integer lines (and moneyline
ties) push, and prices are conditional on no push. Polymarket's rule for NFL
ties (moneyline resolves 50-50) and integer-line pushes is still an open
question (README); every catalog spread/total seen so far is a half point.

Stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from combo_mm.nfl import joint

__all__ = [
    "ML",
    "SPR",
    "TOT",
    "TT",
    "NflLegMarket",
    "parse_nfl_slug",
    "parse_leg",
    "to_joint_leg",
    "unsupported_reason",
]

ML, SPR, TOT, TT = "ML", "SPR", "TOT", "TT"

_GAME = re.compile(r"^nfl-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})(?:-(.+))?$")
_LINE = r"(\d+)pt(\d+)"
_SPREAD = re.compile(rf"^spread-(home|away)-{_LINE}$")
_TOTAL = re.compile(rf"^total-{_LINE}$")
_TEAM_TOTAL = re.compile(rf"^team-total-([a-z]+)-{_LINE}$")
_PERIOD = re.compile(r"^\d+[hq]-")


@dataclass(frozen=True)
class NflLegMarket:
    """One outcome of a full-game NFL market, parsed from its slug."""

    slug: str
    game_key: str           # "nfl-{away}-{home}-{date}", shared by every market on the game
    date: str               # slug date (UTC date of kickoff)
    home: str               # team code, upper case (nflverse codes: LA, WAS, JAX, ...)
    away: str
    kind: str               # ML | SPR | TOT | TT
    outcome_index: int      # 0 or 1 (catalog position order)
    favourite: Optional[str] = None   # SPR: "home" or "away" (the side laying the points)
    line: Optional[float] = None      # SPR: handicap > 0; TOT/TT: points
    team: Optional[str] = None        # TT: team code

    @property
    def is_main_candidate(self) -> bool:
        """Full-game spread or total: usable to locate the score distribution."""
        return self.kind in (SPR, TOT)

    def describe(self) -> str:
        if self.kind == ML:
            return f"{self.away if self.outcome_index == 0 else self.home} ML"
        if self.kind == SPR:
            fav = self.home if self.favourite == "home" else self.away
            dog = self.away if self.favourite == "home" else self.home
            return (f"{fav} -{self.line:g}" if self.outcome_index == 0
                    else f"{dog} +{self.line:g}")
        if self.kind == TOT:
            return f"{'Over' if self.outcome_index == 0 else 'Under'} {self.line:g}"
        return f"{self.team} {'over' if self.outcome_index == 0 else 'under'} {self.line:g}"


def _line(whole: str, frac: str) -> float:
    return float(f"{whole}.{frac}")


def parse_nfl_slug(slug: str) -> Optional[Tuple[str, str, str, str]]:
    """``(game_key, away, home, suffix)`` for any NFL game slug, else None."""
    m = _GAME.match(slug or "")
    if not m:
        return None
    away, home, date, suffix = m.group(1), m.group(2), m.group(3), m.group(4) or ""
    return f"nfl-{away}-{home}-{date}", away.upper(), home.upper(), suffix


def unsupported_reason(slug: str) -> Optional[str]:
    """Why an NFL game market cannot be priced by the score model (None if it can)."""
    parsed = parse_nfl_slug(slug)
    if parsed is None:
        return "not an NFL game market"
    suffix = parsed[3]
    if suffix == "" or _SPREAD.match(suffix) or _TOTAL.match(suffix) or _TEAM_TOTAL.match(suffix):
        return None
    if _PERIOD.match(suffix):
        return "period market (half/quarter) is not a final-score leg"
    return "player/game prop is not a final-score leg (Phase B)"


def parse_leg(slug: str, outcome_index: int) -> Optional[NflLegMarket]:
    """Parse one outcome of a full-game NFL market; None if unsupported."""
    parsed = parse_nfl_slug(slug)
    if parsed is None or outcome_index not in (0, 1):
        return None
    game_key, away, home, suffix = parsed
    date = game_key[-10:]
    base = dict(slug=slug, game_key=game_key, date=date, home=home, away=away,
                outcome_index=outcome_index)
    if suffix == "":
        return NflLegMarket(kind=ML, **base)
    m = _SPREAD.match(suffix)
    if m:
        return NflLegMarket(kind=SPR, favourite=m.group(1), line=_line(m.group(2), m.group(3)), **base)
    m = _TOTAL.match(suffix)
    if m:
        return NflLegMarket(kind=TOT, line=_line(m.group(1), m.group(2)), **base)
    m = _TEAM_TOTAL.match(suffix)
    if m:
        team = m.group(1).upper()
        if team not in (home, away):
            return None
        return NflLegMarket(kind=TT, team=team, line=_line(m.group(2), m.group(3)), **base)
    return None


def to_joint_leg(market: NflLegMarket) -> joint.Leg:
    """The canonical score leg this outcome wins on (see module table)."""
    first = market.outcome_index == 0
    if market.kind == ML:
        return joint.away_ml() if first else joint.home_ml()
    assert market.line is not None
    if market.kind == SPR:
        if market.favourite == "home":
            return joint.home_cover(market.line) if first else joint.away_cover(market.line)
        return joint.away_cover(-market.line) if first else joint.home_cover(-market.line)
    if market.kind == TOT:
        return joint.over(market.line) if first else joint.under(market.line)
    if market.team == market.home:
        return joint.home_team_over(market.line) if first else joint.home_team_under(market.line)
    return joint.away_team_over(market.line) if first else joint.away_team_under(market.line)

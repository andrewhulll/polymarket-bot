"""Live Polymarket combo legs -> :class:`combo_mm.nfl.markets.NflLegMarket` (stdlib only).

:mod:`combo_mm.nfl.markets` (#15) owns the NFL leg model: what YES means, the
canonical mapping to :mod:`combo_mm.nfl.joint`, and settlement. It builds its
markets from nflverse games under an internal symbol grammar. Live RFQs arrive
the other way round: the quoter gateway names each leg by its on-chain position
id, and the combo catalog (:mod:`combo_mm.combo_markets`) resolves that to a
market ``slug``, its ``outcomes`` and the leg's ``outcome_index``. This module
is that bridge, so live pricing reuses the same leg model and the same tested
mapping rather than a parallel one.

Slug grammar (verified against the live catalog, 2026-09-17; the slug lists the
**away** team first)::

    nfl-{away}-{home}-{yyyy-mm-dd}                       moneyline  outcomes [away, home]
    nfl-{away}-{home}-{date}-spread-home-{x}pt{y}        home -x.y  outcomes [home, away]
    nfl-{away}-{home}-{date}-spread-away-{x}pt{y}        away -x.y  outcomes [away, home]
    nfl-{away}-{home}-{date}-total-{x}pt{y}              game O/U   outcomes [Over, Under]
    nfl-{away}-{home}-{date}-team-total-{team}-{x}pt{y}  team O/U   outcomes [Over, Under]

Each position is one *outcome* of such a market, so :func:`parse_catalog_leg`
returns the leg market plus the side of it that position pays on: outcome 0 of
a spread is the favourite laying the points (subject = that team, negative
line, side ``YES``), outcome 1 is the other team taking them (subject = the
other team, positive line, side ``YES``), and outcome 1 of a total or team
total is the ``NO`` side of the same market.

Anything else on an NFL game -- first-half / quarter markets (``1h-``, ``1q-``),
player props, exact margin, first touchdown -- is not a function of the final
score pair, so it is reported as unsupported (Phase B) rather than approximated.

Season and week are not in the slug. They are metadata on ``NflLegMarket``
(the canonical mapping does not use them), so a caller that knows the slate --
the live pricer passes the current weekly params file -- can supply the real
nflverse ``game_id`` / season / week; otherwise the season is derived from the
kickoff date and the week is left 0.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from combo_mm.nfl.markets import ML, SPR, TOT, TT, NflLegMarket

__all__ = [
    "CATALOG_KINDS",
    "parse_game_slug",
    "parse_catalog_leg",
    "unsupported_reason",
    "describe_leg",
    "season_of",
]

CATALOG_KINDS = (ML, SPR, TOT, TT)

_GAME = re.compile(r"^nfl-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})(?:-(.+))?$")
_LINE = r"(\d+)pt(\d+)"
_SPREAD = re.compile(rf"^spread-(home|away)-{_LINE}$")
_TOTAL = re.compile(rf"^total-{_LINE}$")
_TEAM_TOTAL = re.compile(rf"^team-total-([a-z]+)-{_LINE}$")
_PERIOD = re.compile(r"^\d+[hq]-")


def _line(whole: str, frac: str) -> float:
    return float(f"{whole}.{frac}")


def season_of(date: str) -> int:
    """NFL season of a kickoff date: January and February belong to the prior season."""
    year, month = int(date[:4]), int(date[5:7])
    return year - 1 if month <= 2 else year


def parse_game_slug(slug: str) -> Optional[Tuple[str, str, str, str, str]]:
    """``(game_key, away, home, date, suffix)`` for any NFL game slug, else None."""
    m = _GAME.match(slug or "")
    if not m:
        return None
    away, home, date, suffix = m.group(1), m.group(2), m.group(3), m.group(4) or ""
    return f"nfl-{away}-{home}-{date}", away.upper(), home.upper(), date, suffix


def unsupported_reason(slug: str) -> Optional[str]:
    """Why this market cannot be priced by the score model (None when it can)."""
    parsed = parse_game_slug(slug)
    if parsed is None:
        return "not an NFL game market"
    suffix = parsed[4]
    if suffix == "" or _SPREAD.match(suffix) or _TOTAL.match(suffix) or _TEAM_TOTAL.match(suffix):
        return None
    if _PERIOD.match(suffix):
        return "period market (half/quarter) is not a final-score leg"
    return "player/game prop is not a final-score leg (Phase B)"


def parse_catalog_leg(slug: str, outcome_index: int, *, kickoff_utc: str = "",
                      game_id: Optional[str] = None, season: Optional[int] = None,
                      week: int = 0) -> Optional[Tuple[NflLegMarket, str]]:
    """``(market, side)`` for one outcome of a full-game NFL market, else None.

    ``side`` is what this position pays on: ``"YES"`` except the under side of
    a total or team total. Pass ``game_id`` / ``season`` / ``week`` when the
    slate is known; they are metadata and never change the canonical mapping.
    """
    parsed = parse_game_slug(slug)
    if parsed is None or outcome_index not in (0, 1):
        return None
    game_key, away, home, date, suffix = parsed
    first = outcome_index == 0
    base = dict(symbol=f"{slug}#{outcome_index}",
                game_id=game_id or game_key,
                season=season if season is not None else season_of(date),
                week=week, kickoff_utc=kickoff_utc or f"{date}T00:00:00Z",
                kickoff_estimated=not kickoff_utc, home=home, away=away)

    if suffix == "":
        # Outcome order follows the slug: away team first.
        return NflLegMarket(kind=ML, subject=away if first else home, line=None, **base), "YES"
    m = _SPREAD.match(suffix)
    if m:
        favourite = home if m.group(1) == "home" else away
        underdog = away if m.group(1) == "home" else home
        line = _line(m.group(2), m.group(3))
        # Outcome 0 is the favourite laying the points (negative line in its
        # own terms); outcome 1 is the underdog taking them.
        subject, signed = (favourite, -line) if first else (underdog, line)
        return NflLegMarket(kind=SPR, subject=subject, line=signed, **base), "YES"
    m = _TOTAL.match(suffix)
    if m:
        return (NflLegMarket(kind=TOT, subject=None, line=_line(m.group(1), m.group(2)), **base),
                "YES" if first else "NO")
    m = _TEAM_TOTAL.match(suffix)
    if m:
        team = m.group(1).upper()
        if team not in (home, away):
            return None
        return (NflLegMarket(kind=TT, subject=team, line=_line(m.group(2), m.group(3)), **base),
                "YES" if first else "NO")
    return None


def describe_leg(market: NflLegMarket, side: str) -> str:
    """Short human label: ``"BUF -4.5"``, ``"Over 54.5"``, ``"BUF ML"``."""
    if market.kind == ML:
        return f"{market.subject} ML"
    if market.kind == SPR:
        return f"{market.subject} {market.line:+g}"
    if market.kind == TOT:
        return f"{'Over' if side == 'YES' else 'Under'} {market.line:g}"
    return f"{market.subject} {'over' if side == 'YES' else 'under'} {market.line:g}"

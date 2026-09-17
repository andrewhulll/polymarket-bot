"""NFL leg-market registry: RFQ leg symbols <-> canonical score legs (stdlib only).

Nothing in the pipeline could previously turn an RFQ leg ``symbol`` into
"which game, which market, which line". :mod:`combo_mm.nfl.joint` needs a
canonical :class:`~combo_mm.nfl.joint.Leg`; the store only carries opaque
symbols. This module is that bridge, and it is the single place where the
NFL market conventions live:

- **Symbol grammar** (:func:`build_symbol` / :func:`parse_symbol`), internal
  and deterministic, so a dataset can be regenerated and a live resolver
  (#11) can produce the same :class:`NflLegMarket` from Polymarket token ids.
  Everything downstream stays symbol-agnostic.
- **Canonical mapping** (:func:`to_joint_leg`): nflverse ``spread_line`` is
  the *home expected margin*, while a spread symbol carries its line in the
  *subject team's* terms, so the two differ by a sign for home subjects.
  Every row of that mapping is pinned by a unit test.
- **Settlement** (:func:`settlement_price`): the raw YES result as the wire
  carries it (``"1"``/``"0"``/``"0.5"``, or ``None`` for a void leg), never
  inverted for NO -- the YES/NO inversion belongs to the pricer and
  :func:`combo_mm.paper_backtest.combo_settlement_value`.

Settlement assumptions (open questions until confirmed against Polymarket's
NFL market rules, see ``docs/rfq-simulation.md``):

- An integer spread/total line can **push**. ``push_rule="void"`` (default)
  settles the leg ``None``, which voids the combo; ``"half"`` settles
  ``"0.5"``.
- An ML **tie** (~0.2% of NFL games) uses the market's own ``tie_rule``:
  ``"void"`` (default) -> ``None``, ``"half"`` -> ``"0.5"``, ``"no"`` -> the
  team did not win, so ``"0"``.

Both are config knobs precisely because the real rules are unverified; #5
reports sensitivity to them.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from combo_mm.nfl import joint
from combo_mm.nfl.estimate import implied_means
from combo_mm.nfl.ingest import Game, kickoff_utc

__all__ = [
    "ML", "SPR", "TOT", "TT", "KINDS",
    "PUSH_VOID", "PUSH_HALF", "PUSH_RULES",
    "TIE_VOID", "TIE_HALF", "TIE_NO", "TIE_RULES",
    "ALT_SPREAD_OFFSETS", "ALT_TOTAL_OFFSETS",
    "MarketSymbolError",
    "NflLegMarket",
    "LegRegistry",
    "build_symbol",
    "parse_symbol",
    "round_half",
    "half_point",
    "game_markets",
    "to_joint_leg",
    "settlement_price",
]

ML, SPR, TOT, TT = "ML", "SPR", "TOT", "TT"
KINDS = (ML, SPR, TOT, TT)

PUSH_VOID, PUSH_HALF = "void", "half"
PUSH_RULES = (PUSH_VOID, PUSH_HALF)

TIE_VOID, TIE_HALF, TIE_NO = "void", "half", "no"
TIE_RULES = (TIE_VOID, TIE_HALF, TIE_NO)

ALT_SPREAD_OFFSETS = (-7.0, -3.0, 3.0, 7.0)
ALT_TOTAL_OFFSETS = (-7.0, -3.0, 3.0, 7.0)

MAX_ABS_LINE = 100.0

_SYMBOL_RE = re.compile(
    r"^NFL-(?P<season>\d{4})-W(?P<week>\d{2})-(?P<away>[A-Z]{2,3})-(?P<home>[A-Z]{2,3})"
    r"-(?P<rest>.+)$"
)
_SIGNED_RE = re.compile(r"^(?P<sign>[MP])(?P<mag>\d+(?:\.\d+)?)$")
_UNSIGNED_RE = re.compile(r"^\d+(?:\.\d+)?$")


class MarketSymbolError(ValueError):
    """A leg symbol does not parse as an NFL leg market."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NflLegMarket:
    """One binary NFL leg market: what YES means, and how it settles."""

    symbol: str
    game_id: str                 # nflverse game_id, e.g. "2024_05_BUF_KC"
    season: int
    week: int
    kickoff_utc: str             # ISO-8601 Z
    home: str
    away: str
    kind: str                    # ML | SPR | TOT | TT
    subject: Optional[str]       # team code for ML/SPR/TT; None for TOT
    line: Optional[float]        # SPR: in the SUBJECT's terms (-3.5 = subject favoured by 3.5)
    is_main_line: bool = False   # the spread/total #2 calibrates against
    tie_rule: str = TIE_VOID     # ML only
    kickoff_estimated: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise MarketSymbolError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if self.tie_rule not in TIE_RULES:
            raise MarketSymbolError(f"tie_rule must be one of {TIE_RULES}, got {self.tie_rule!r}")
        if self.kind == TOT:
            if self.subject is not None:
                raise MarketSymbolError("TOT markets have no subject team")
        elif self.subject not in (self.home, self.away):
            raise MarketSymbolError(
                f"{self.kind} subject {self.subject!r} is neither {self.home} nor {self.away}")
        if self.kind == ML:
            if self.line not in (None, 0.0):
                raise MarketSymbolError("ML markets carry no line")
        elif self.line is None or not math.isfinite(self.line) or abs(self.line) > MAX_ABS_LINE:
            raise MarketSymbolError(f"{self.kind} line {self.line!r} missing or implausible")
        if self.kind in (TOT, TT) and self.line is not None and self.line <= 0:
            raise MarketSymbolError(f"{self.kind} line must be positive, got {self.line}")

    @property
    def subject_is_home(self) -> bool:
        """Is YES's subject the home team? False for TOT, which has no subject."""
        return self.subject == self.home

    @property
    def pushable(self) -> bool:
        """Can this leg land exactly on its line (an integer line)?"""
        if self.kind == ML:
            return True                       # a tie is the ML push
        return float(self.line).is_integer()  # type: ignore[arg-type]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "game_id": self.game_id, "season": self.season,
            "week": self.week, "kickoff_utc": self.kickoff_utc,
            "kickoff_estimated": self.kickoff_estimated, "home": self.home,
            "away": self.away, "kind": self.kind, "subject": self.subject,
            "line": self.line, "is_main_line": self.is_main_line,
            "tie_rule": self.tie_rule,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "NflLegMarket":
        return cls(
            symbol=str(raw["symbol"]), game_id=str(raw["game_id"]),
            season=int(raw["season"]), week=int(raw["week"]),
            kickoff_utc=str(raw["kickoff_utc"]),
            kickoff_estimated=bool(raw.get("kickoff_estimated", False)),
            home=str(raw["home"]), away=str(raw["away"]), kind=str(raw["kind"]),
            subject=(None if raw.get("subject") is None else str(raw["subject"])),
            line=(None if raw.get("line") is None else float(raw["line"])),
            is_main_line=bool(raw.get("is_main_line", False)),
            tie_rule=str(raw.get("tie_rule", TIE_VOID)),
        )


# ---------------------------------------------------------------------------
# Symbol grammar
# ---------------------------------------------------------------------------

def _fmt_line(value: float) -> str:
    """Canonical unsigned magnitude: ``3.5`` -> ``"3.5"``, ``7.0`` -> ``"7"``."""
    text = f"{abs(float(value)):g}"
    if "e" in text or "E" in text:
        raise MarketSymbolError(f"line {value!r} out of representable range")
    return text


def build_symbol(season: int, week: int, away: str, home: str, kind: str,
                 subject: Optional[str] = None, line: Optional[float] = None) -> str:
    """Canonical leg symbol. See :func:`parse_symbol` for the grammar."""
    head = f"NFL-{season:04d}-W{week:02d}-{away}-{home}"
    if kind == ML:
        return f"{head}-ML-{subject}"
    if kind == SPR:
        sign = "M" if float(line) < 0 else "P"   # type: ignore[arg-type]
        return f"{head}-SPR-{subject}-{sign}{_fmt_line(line)}"  # type: ignore[arg-type]
    if kind == TOT:
        return f"{head}-TOT-{_fmt_line(line)}"   # type: ignore[arg-type]
    if kind == TT:
        return f"{head}-TT-{subject}-{_fmt_line(line)}"  # type: ignore[arg-type]
    raise MarketSymbolError(f"unknown kind {kind!r}")


def parse_symbol(symbol: str) -> Tuple[int, int, str, str, str, Optional[str], Optional[float]]:
    """``(season, week, away, home, kind, subject, line)`` from a leg symbol.

    Grammar (``M``/``P`` encode the sign so no ``-`` appears inside a field)::

        NFL-{season}-W{week:02d}-{AWAY}-{HOME}-ML-{TEAM}
        NFL-{season}-W{week:02d}-{AWAY}-{HOME}-SPR-{TEAM}-{M|P}{x}
        NFL-{season}-W{week:02d}-{AWAY}-{HOME}-TOT-{x}
        NFL-{season}-W{week:02d}-{AWAY}-{HOME}-TT-{TEAM}-{x}

    YES means: TEAM wins (ML); TEAM's margin plus its signed line is positive
    (SPR, so ``M3.5`` = the team is favoured by 3.5); the game total exceeds
    ``x`` (TOT); TEAM's own points exceed ``x`` (TT).

    Raises :class:`MarketSymbolError` on anything else -- an unknown symbol is
    never guessed at.
    """
    m = _SYMBOL_RE.match(symbol or "")
    if not m:
        raise MarketSymbolError(f"not an NFL leg symbol: {symbol!r}")
    season, week = int(m.group("season")), int(m.group("week"))
    away, home = m.group("away"), m.group("home")
    parts = m.group("rest").split("-")
    kind = parts[0]
    if kind == ML and len(parts) == 2:
        return season, week, away, home, ML, parts[1], None
    if kind == SPR and len(parts) == 3:
        signed = _SIGNED_RE.match(parts[2])
        if signed:
            line = float(signed.group("mag"))
            return (season, week, away, home, SPR, parts[1],
                    -line if signed.group("sign") == "M" else line)
    if kind == TOT and len(parts) == 2 and _UNSIGNED_RE.match(parts[1]):
        return season, week, away, home, TOT, None, float(parts[1])
    if kind == TT and len(parts) == 3 and _UNSIGNED_RE.match(parts[2]):
        return season, week, away, home, TT, parts[1], float(parts[2])
    raise MarketSymbolError(f"not an NFL leg symbol: {symbol!r}")


# ---------------------------------------------------------------------------
# Canonical mapping and settlement
# ---------------------------------------------------------------------------

def to_joint_leg(market: NflLegMarket, side: str = "YES") -> joint.Leg:
    """Canonical score leg for ``side`` of ``market``.

    A spread symbol's line is in the subject's terms, while
    :func:`combo_mm.nfl.joint.home_cover` takes the *home expected margin*,
    so a home subject's line flips sign. NO is the opposite-direction leg on
    the same line (conditional on no push, which is how
    :mod:`combo_mm.nfl.joint` already conditions).
    """
    if side not in ("YES", "NO"):
        raise MarketSymbolError(f"side must be YES or NO, got {side!r}")
    yes = side == "YES"
    if market.kind == TOT:
        return joint.over(market.line) if yes else joint.under(market.line)  # type: ignore[arg-type]
    if market.kind == TT:
        if market.subject_is_home:
            return (joint.home_team_over(market.line) if yes           # type: ignore[arg-type]
                    else joint.home_team_under(market.line))           # type: ignore[arg-type]
        return (joint.away_team_over(market.line) if yes               # type: ignore[arg-type]
                else joint.away_team_under(market.line))               # type: ignore[arg-type]
    # ML and SPR both resolve on the margin: pick which team's side we are on.
    home_side = market.subject_is_home == yes
    if market.kind == ML:
        return joint.home_ml() if home_side else joint.away_ml()
    home_margin_line = -market.line if market.subject_is_home else market.line  # type: ignore[operator]
    return (joint.home_cover(home_margin_line) if home_side
            else joint.away_cover(home_margin_line))


def settlement_price(market: NflLegMarket, home_score: int, away_score: int,
                     push_rule: str = PUSH_VOID) -> Optional[str]:
    """Raw YES settlement of ``market``: ``"1"``, ``"0"``, ``"0.5"`` or ``None``.

    Never inverted for NO -- this is the wire value
    (``comboLegs[].settlementPrice``), and the pricer owns the inversion.
    ``None`` means the leg voids (a push under ``push_rule="void"``, or an ML
    tie under ``tie_rule="void"``), which voids the whole combo.
    """
    if push_rule not in PUSH_RULES:
        raise MarketSymbolError(f"push_rule must be one of {PUSH_RULES}, got {push_rule!r}")
    result = joint.settle_leg(to_joint_leg(market, "YES"), home_score, away_score)
    if result == joint.WIN:
        return "1"
    if result == joint.LOSE:
        return "0"
    rule = market.tie_rule if market.kind == ML else push_rule
    if rule == TIE_HALF:      # == PUSH_HALF
        return "0.5"
    if rule == TIE_NO:        # ML only: a tie is not a win
        return "0"
    return None


# ---------------------------------------------------------------------------
# Building a game's market set
# ---------------------------------------------------------------------------

def round_half(value: float) -> float:
    """Nearest 0.5 (ties away from zero, so 0.25 -> 0.5)."""
    scaled = abs(float(value)) * 2.0
    rounded = math.floor(scaled + 0.5) / 2.0
    return math.copysign(rounded, value)


def half_point(value: float) -> float:
    """Nearest x.5 at or below ``|value|``, keeping the sign: never an integer.

    Forcing listed lines off integers is what makes pushes rare; it moves the
    key numbers (3 -> 3.5, 7 -> 7.5) by half a point, which is a documented
    assumption of the simulated dataset, not a market fact.
    """
    magnitude = math.floor(abs(float(value))) + 0.5
    return math.copysign(magnitude, value)


def game_markets(game: Game, *, force_half_point_lines: bool = True,
                 alt_lines: bool = True, tie_rule: str = TIE_VOID) -> List[NflLegMarket]:
    """Every leg market listed for ``game``, main lines first.

    Main markets are both ML sides, both sides of the closing spread and the
    closing total. With ``alt_lines`` the listing adds alternate spreads and
    totals at +-3 and +-7 points and both teams' team totals at their
    closing-line implied points. ``force_half_point_lines`` snaps every
    listed line to a half point.
    """
    if not game.has_lines:
        raise MarketSymbolError(f"{game.game_id}: no closing lines to list markets from")
    kickoff, estimated = kickoff_utc(game)
    fav, dog = ((game.home, game.away) if game.spread_line > 0
                else (game.away, game.home))
    snap = half_point if force_half_point_lines else round_half
    spread_mag = snap(abs(game.spread_line)) if game.spread_line else snap(0.5)
    total = snap(game.total_line)

    def make(kind: str, subject: Optional[str], line: Optional[float],
             main: bool) -> NflLegMarket:
        return NflLegMarket(
            symbol=build_symbol(game.season, game.week, game.away, game.home,
                                kind, subject, line),
            game_id=game.game_id, season=game.season, week=game.week,
            kickoff_utc=kickoff, kickoff_estimated=estimated,
            home=game.home, away=game.away, kind=kind, subject=subject,
            line=line, is_main_line=main, tie_rule=tie_rule,
        )

    markets = [
        make(ML, game.home, None, True),
        make(ML, game.away, None, True),
        make(SPR, fav, -spread_mag, True),
        make(SPR, dog, spread_mag, True),
        make(TOT, None, total, True),
    ]
    if alt_lines:
        for offset in ALT_SPREAD_OFFSETS:
            alt = spread_mag + offset
            if alt <= 0:
                continue          # the favourite's line crossed sides; skip
            markets.append(make(SPR, fav, -alt, False))
            markets.append(make(SPR, dog, alt, False))
        for offset in ALT_TOTAL_OFFSETS:
            alt = total + offset
            if alt <= 0:
                continue
            markets.append(make(TOT, None, alt, False))
        mu_home, mu_away = implied_means(game.spread_line, game.total_line)
        for team, mu in ((game.home, mu_home), (game.away, mu_away)):
            markets.append(make(TT, team, snap(mu), False))
    return markets


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class LegRegistry:
    """Symbol -> :class:`NflLegMarket`, persisted as canonical JSON.

    A dataset ships its registry next to its session so a replay resolves
    exactly the markets the generator listed; the manifest pins the file by
    sha256.
    """

    def __init__(self, markets: Iterable[NflLegMarket] = ()) -> None:
        self._by_symbol: Dict[str, NflLegMarket] = {}
        self._by_game: Dict[str, List[str]] = {}
        for market in markets:
            self.add(market)

    def __len__(self) -> int:
        return len(self._by_symbol)

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._by_symbol

    def add(self, market: NflLegMarket) -> None:
        existing = self._by_symbol.get(market.symbol)
        if existing is not None:
            if existing != market:
                raise MarketSymbolError(
                    f"{market.symbol}: already registered with different terms")
            return
        self._by_symbol[market.symbol] = market
        self._by_game.setdefault(market.game_id, []).append(market.symbol)

    def get(self, symbol: str) -> Optional[NflLegMarket]:
        """The market for ``symbol``, or None -- an unknown leg is never guessed."""
        return self._by_symbol.get(symbol)

    def game_ids(self) -> List[str]:
        return sorted(self._by_game)

    def markets_for_game(self, game_id: str) -> List[NflLegMarket]:
        return [self._by_symbol[s] for s in sorted(self._by_game.get(game_id, ()))]

    def main_markets(self, game_id: str) -> Dict[str, str]:
        """``{"ML": sym, "SPR": sym, "TOT": sym}`` -- the markets #2 calibrates to.

        ML and SPR are the **home-referenced** sides, so a caller can read
        their prices as ``P(home wins)`` / ``P(home covers)`` without
        re-deriving which team is favoured. Missing kinds are omitted.
        """
        out: Dict[str, str] = {}
        for market in self.markets_for_game(game_id):
            if not market.is_main_line:
                continue
            if market.kind in (ML, SPR) and market.subject_is_home:
                out[market.kind] = market.symbol
            elif market.kind == TOT:
                out[TOT] = market.symbol
        return out

    def games_for_symbols(self, symbols: Iterable[str]) -> Tuple[List[str], List[str]]:
        """``(game_ids, unknown_symbols)`` -- the same-game grouping key for #2."""
        games: List[str] = []
        unknown: List[str] = []
        for symbol in symbols:
            market = self.get(symbol)
            if market is None:
                unknown.append(symbol)
            elif market.game_id not in games:
                games.append(market.game_id)
        return games, unknown

    def with_tie_rule(self, tie_rule: str) -> "LegRegistry":
        """Copy with every ML market's ``tie_rule`` replaced (#5 sensitivity)."""
        return LegRegistry(replace(m, tie_rule=tie_rule) if m.kind == ML else m
                           for m in self._by_symbol.values())

    # -- persistence -----------------------------------------------------
    def to_json_bytes(self) -> bytes:
        payload = {
            "schema_version": 1,
            "sport": "nfl",
            "markets": [self._by_symbol[s].to_dict() for s in sorted(self._by_symbol)],
        }
        return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")

    def dump(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.to_json_bytes())
        return path

    @classmethod
    def load(cls, path: Path | str) -> "LegRegistry":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("sport") != "nfl" or payload.get("schema_version") != 1:
            raise MarketSymbolError(f"{path}: not a v1 NFL leg registry")
        return cls(NflLegMarket.from_dict(raw) for raw in payload["markets"])

    @classmethod
    def from_games(cls, games: Iterable[Game], **kwargs: Any) -> "LegRegistry":
        registry = cls()
        for game in games:
            for market in game_markets(game, **kwargs):
                registry.add(market)
        return registry

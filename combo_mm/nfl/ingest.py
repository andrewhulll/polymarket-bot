"""nflverse schedule/score/closing-line ingestion (stdlib only).

Source: ``nflverse/nfldata`` ``games.csv`` -- one row per game since 1999 with
final scores, overtime flag, closing ``spread_line`` / ``total_line`` and
(since 2006) closing moneyline / spread / total prices. Fetched over plain
HTTPS; the deprecated ``nfl_data_py`` wrapper is not needed.

Conventions (verified against the data, see tests):

- ``spread_line`` is the **home team's expected margin** (positive = home
  favored). The home side covers iff ``home_score - away_score > spread_line``.
- ``home_spread_odds`` prices the home side of that spread,
  ``over_odds``/``under_odds`` the total, ``*_moneyline`` the outright winner.
  All American odds.
- Relocated franchises are mapped to their current code (``OAK -> LV``,
  ``SD -> LAC``, ``STL -> LA``) so per-team history is continuous.

Raw pulls are cached immutably under ``<cache_root>/nflverse_<pulldate>/``
with a ``manifest.json`` (source URL, pull date, SHA-256, row count) so every
params file can pin its data vintage.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "NFLVERSE_GAMES_URL",
    "FRANCHISE_MAP",
    "REQUIRED_COLUMNS",
    "Game",
    "RawPull",
    "ValidationReport",
    "IngestError",
    "pull_games",
    "latest_pull",
    "load_pull",
    "load_games",
    "write_pull",
    "parse_games_csv",
    "validate_games",
    "american_to_prob",
    "devig_pair",
]

NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

FRANCHISE_MAP = {"OAK": "LV", "SD": "LAC", "STL": "LA"}

REQUIRED_COLUMNS = (
    "game_id", "season", "game_type", "week", "gameday", "away_team",
    "away_score", "home_team", "home_score", "location", "overtime",
    "spread_line", "total_line", "away_moneyline", "home_moneyline",
    "away_spread_odds", "home_spread_odds", "under_odds", "over_odds",
    "div_game",
)

# Plausibility bounds for validation.
MAX_SCORE = 80
MAX_ABS_SPREAD = 30.0
TOTAL_RANGE = (25.0, 70.0)


class IngestError(ValueError):
    """Raised when raw data fails fatal validation."""


@dataclass(frozen=True)
class Game:
    game_id: str
    season: int
    week: int
    game_type: str
    gameday: str
    home: str                     # franchise code (relocations mapped)
    away: str
    home_score: Optional[int]     # None => not yet played
    away_score: Optional[int]
    overtime: bool
    neutral: bool
    spread_line: Optional[float]  # home expected margin
    total_line: Optional[float]
    home_moneyline: Optional[float] = None
    away_moneyline: Optional[float] = None
    home_spread_odds: Optional[float] = None
    away_spread_odds: Optional[float] = None
    over_odds: Optional[float] = None
    under_odds: Optional[float] = None
    div_game: bool = False

    @property
    def played(self) -> bool:
        return self.home_score is not None and self.away_score is not None

    @property
    def has_lines(self) -> bool:
        return self.spread_line is not None and self.total_line is not None

    @property
    def order_key(self) -> Tuple[int, int]:
        return (self.season, self.week)

    @property
    def margin(self) -> Optional[int]:
        return None if not self.played else self.home_score - self.away_score  # type: ignore[operator]

    @property
    def total(self) -> Optional[int]:
        return None if not self.played else self.home_score + self.away_score  # type: ignore[operator]


@dataclass
class ValidationReport:
    n_rows: int = 0
    n_played: int = 0
    n_scheduled: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, object]:
        return {
            "n_rows": self.n_rows, "n_played": self.n_played,
            "n_scheduled": self.n_scheduled, "ok": self.ok,
            "errors": list(self.errors), "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RawPull:
    directory: Path
    csv_path: Path
    manifest: Dict[str, object]

    @property
    def pull_date(self) -> str:
        return str(self.manifest["pull_date"])

    @property
    def sha256(self) -> str:
        return str(self.manifest["sha256"])


# ---------------------------------------------------------------------------
# Odds helpers
# ---------------------------------------------------------------------------

def american_to_prob(odds: Optional[float]) -> Optional[float]:
    """Implied probability (with vig) of American odds; None if unusable."""
    if odds is None or not math.isfinite(odds) or abs(odds) < 100:
        return None
    return -odds / (-odds + 100.0) if odds < 0 else 100.0 / (odds + 100.0)


def devig_pair(odds_a: Optional[float], odds_b: Optional[float]) -> Optional[float]:
    """De-vigged probability of side A by normalizing the overround."""
    pa, pb = american_to_prob(odds_a), american_to_prob(odds_b)
    if pa is None or pb is None or pa + pb <= 0:
        return None
    return pa / (pa + pb)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _opt_float(raw: str) -> Optional[float]:
    raw = (raw or "").strip()
    if raw == "" or raw.upper() == "NA":
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def _opt_int(raw: str) -> Optional[int]:
    value = _opt_float(raw)
    if value is None:
        return None
    if value != int(value):
        raise ValueError(f"non-integer score {raw!r}")
    return int(value)


def _team(raw: str) -> str:
    code = (raw or "").strip()
    return FRANCHISE_MAP.get(code, code)


def parse_games_csv(text: str) -> Tuple[List[Game], ValidationReport]:
    """Parse ``games.csv`` text. Row-level problems become report errors."""
    report = ValidationReport()
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        report.errors.append(f"missing required columns: {missing}")
        return [], report
    games: List[Game] = []
    for line_no, row in enumerate(reader, start=2):
        report.n_rows += 1
        try:
            game = Game(
                game_id=row["game_id"].strip(),
                season=int(row["season"]),
                week=int(row["week"]),
                game_type=row["game_type"].strip(),
                gameday=row["gameday"].strip(),
                home=_team(row["home_team"]),
                away=_team(row["away_team"]),
                home_score=_opt_int(row["home_score"]),
                away_score=_opt_int(row["away_score"]),
                overtime=(_opt_float(row["overtime"]) or 0.0) > 0,
                neutral=row["location"].strip().lower() == "neutral",
                spread_line=_opt_float(row["spread_line"]),
                total_line=_opt_float(row["total_line"]),
                home_moneyline=_opt_float(row["home_moneyline"]),
                away_moneyline=_opt_float(row["away_moneyline"]),
                home_spread_odds=_opt_float(row["home_spread_odds"]),
                away_spread_odds=_opt_float(row["away_spread_odds"]),
                over_odds=_opt_float(row["over_odds"]),
                under_odds=_opt_float(row["under_odds"]),
                div_game=(_opt_float(row["div_game"]) or 0.0) > 0,
            )
        except (ValueError, KeyError) as exc:
            report.errors.append(f"line {line_no}: unparseable row ({exc})")
            continue
        games.append(game)
    return games, report


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _expected_reg_games(season: int) -> int:
    return 17 if season >= 2021 else 16


def validate_games(games: Iterable[Game], report: Optional[ValidationReport] = None) -> ValidationReport:
    """Schema/consistency checks. Errors are fatal; warnings are recorded.

    Errors: duplicate game ids, a team scheduled twice in one week, one score
    present without the other, negative/implausible scores, implausible
    lines, a played game without closing lines.
    Warnings: a *completed* regular season where a team's game count differs
    from the schedule length (e.g. the cancelled 2022 BUF-CIN game).
    """
    report = report or ValidationReport()
    games = list(games)
    seen_ids: Dict[str, int] = {}
    team_week: Dict[Tuple[int, int, str], str] = {}
    reg_counts: Dict[Tuple[int, str], int] = {}
    seasons_with_playoffs = set()
    for g in games:
        if g.game_id in seen_ids:
            report.errors.append(f"duplicate game_id {g.game_id}")
        seen_ids[g.game_id] = 1
        for team in (g.home, g.away):
            key = (g.season, g.week, team)
            if key in team_week:
                report.errors.append(
                    f"{team} scheduled twice in {g.season} week {g.week} "
                    f"({team_week[key]}, {g.game_id})"
                )
            team_week[key] = g.game_id
        if (g.home_score is None) != (g.away_score is None):
            report.errors.append(f"{g.game_id}: only one score present")
        if g.played:
            report.n_played += 1
            for s in (g.home_score, g.away_score):
                if s < 0 or s > MAX_SCORE:  # type: ignore[operator]
                    report.errors.append(f"{g.game_id}: implausible score {s}")
            if not g.has_lines:
                report.errors.append(f"{g.game_id}: played game missing closing lines")
            if g.game_type == "REG":
                for team in (g.home, g.away):
                    reg_counts[(g.season, team)] = reg_counts.get((g.season, team), 0) + 1
            else:
                seasons_with_playoffs.add(g.season)
        else:
            report.n_scheduled += 1
        if g.spread_line is not None and abs(g.spread_line) > MAX_ABS_SPREAD:
            report.errors.append(f"{g.game_id}: implausible spread_line {g.spread_line}")
        if g.total_line is not None and not (TOTAL_RANGE[0] <= g.total_line <= TOTAL_RANGE[1]):
            report.errors.append(f"{g.game_id}: implausible total_line {g.total_line}")
    for (season, team), n in sorted(reg_counts.items()):
        if season in seasons_with_playoffs and n != _expected_reg_games(season):
            report.warnings.append(
                f"{season} {team}: {n} regular-season games (expected {_expected_reg_games(season)})"
            )
    return report


# ---------------------------------------------------------------------------
# Pull + cache
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def pull_games(cache_root: Path | str = "data/raw", pull_date: Optional[str] = None,
               url: str = NFLVERSE_GAMES_URL, force: bool = False,
               timeout_s: float = 60.0) -> RawPull:
    """Download ``games.csv`` into an immutable, dated cache directory.

    An existing pull for the same date is reused unless ``force``. The fetch
    is validated before the manifest is written, so a manifest always marks a
    usable pull.
    """
    pull_date = pull_date or _today()
    directory = Path(cache_root) / f"nflverse_{pull_date}"
    manifest_path = directory / "manifest.json"
    if manifest_path.exists() and not force:
        return load_pull(directory)
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 (fixed https URL)
        payload = resp.read()
    return write_pull(payload, directory, pull_date=pull_date, source_url=url)


def write_pull(payload: bytes, directory: Path | str, pull_date: str,
               source_url: str = NFLVERSE_GAMES_URL) -> RawPull:
    """Validate raw bytes and persist them with a manifest."""
    directory = Path(directory)
    text = payload.decode("utf-8")
    games, report = parse_games_csv(text)
    validate_games(games, report)
    if not report.ok:
        raise IngestError("raw pull failed validation: " + "; ".join(report.errors[:10]))
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / "games.csv"
    csv_path.write_bytes(payload)
    manifest = {
        "source_url": source_url,
        "pull_date": pull_date,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "validation": report.to_dict(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return RawPull(directory=directory, csv_path=csv_path, manifest=manifest)


def load_pull(directory: Path | str) -> RawPull:
    """Open a cached pull, verifying the CSV still matches its manifest hash."""
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    csv_path = directory / "games.csv"
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    if digest != manifest["sha256"]:
        raise IngestError(f"{csv_path}: SHA-256 mismatch with manifest (cache corrupted)")
    return RawPull(directory=directory, csv_path=csv_path, manifest=manifest)


def latest_pull(cache_root: Path | str = "data/raw") -> Optional[RawPull]:
    root = Path(cache_root)
    if not root.exists():
        return None
    dirs = sorted(p for p in root.glob("nflverse_*") if (p / "manifest.json").exists())
    return load_pull(dirs[-1]) if dirs else None


def load_games(pull: RawPull) -> List[Game]:
    """Parse + validate a cached pull; raises :class:`IngestError` on errors."""
    games, report = parse_games_csv(pull.csv_path.read_text(encoding="utf-8"))
    validate_games(games, report)
    if not report.ok:
        raise IngestError("; ".join(report.errors[:10]))
    return games


"""Versioned NFL covariance parameter files (stdlib only -- the pricer reads these).

The RFQ hot path never touches raw history: it loads the current
``params/nfl_<season>_w<ww>.json`` and calls :func:`matchup_covariance` with
the market-implied score means. Everything here is pure ``math`` + ``json``.

File contract (``schema_version`` 1)::

    {
      "schema_version": 1, "sport": "nfl", "model_version": "nfl-phaseA-1",
      "season": 2026, "week": 2,
      "as_of": {"season": 2026, "week": 2},       # uses games strictly before
      "data_vintage": {"pull_date": "...", "sha256": "..."},
      "estimator": {...EstimatorConfig...},
      "league": {"variance_model": "mean_linear", "var_intercept": .., "var_slope": ..,
                 "rho": .., "sigma_min": .., "sigma_max": .., "mean_points": ..,
                 "residual_bias": .., "n_games": .., "seasons": [..]},
      "teams": {"KC": {"off_var_factor": .., "def_var_factor": .., "off_mean": ..,
                       "def_mean": .., "n_current": .., "n_prior": ..}, ...},
      "games": [{"game_id", "home", "away", "spread_line", "total_line",
                 "mu_home", "mu_away", "sigma_home", "sigma_away", "rho",
                 "var_margin", "var_total", "cov_margin_total",
                 "corr_margin_total"}, ...]
    }

Variance models (see ``docs/correlation-model.md``):

- ``league_constant``: ``sigma^2`` is one league number for every team.
- ``mean_linear``: ``sigma^2(mu) = var_intercept + var_slope * mu`` -- score
  variance grows with the team's implied points. This is what makes a
  favorite's margin positively correlated with the game total:
  ``Cov(M, T) = sigma_home^2 - sigma_away^2``.
- ``mean_linear_team``: ``mean_linear`` times shrunk per-team offensive
  (scoring team) and defensive (opponent) variance factors.

Serialization is canonical (sorted keys, floats rounded to 6 dp, trailing
newline) so identical inputs give byte-identical files.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "SCHEMA_VERSION",
    "VARIANCE_MODELS",
    "ParamsError",
    "MatchupCovariance",
    "params_filename",
    "parse_params_filename",
    "canonical_json_bytes",
    "write_params",
    "load_params",
    "latest_params",
    "list_params",
    "validate_params",
    "team_sigma",
    "matchup_covariance",
]

SCHEMA_VERSION = 1
VARIANCE_MODELS = ("league_constant", "mean_linear", "mean_linear_team")
FLOAT_DP = 6

_FILENAME_RE = re.compile(r"^nfl_(\d{4})_w(\d{2})\.json$")


class ParamsError(ValueError):
    """A params file is malformed or out of range."""


@dataclass(frozen=True)
class MatchupCovariance:
    sigma_home: float
    sigma_away: float
    rho: float

    @property
    def var_home(self) -> float:
        return self.sigma_home ** 2

    @property
    def var_away(self) -> float:
        return self.sigma_away ** 2

    @property
    def cov_home_away(self) -> float:
        return self.rho * self.sigma_home * self.sigma_away

    @property
    def var_margin(self) -> float:
        return self.var_home + self.var_away - 2 * self.cov_home_away

    @property
    def var_total(self) -> float:
        return self.var_home + self.var_away + 2 * self.cov_home_away

    @property
    def cov_margin_total(self) -> float:
        return self.var_home - self.var_away

    @property
    def corr_margin_total(self) -> float:
        return self.cov_margin_total / math.sqrt(self.var_margin * self.var_total)

    def matrix(self) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        """2x2 covariance of ``(S_home, S_away)``."""
        c = self.cov_home_away
        return ((self.var_home, c), (c, self.var_away))

    def to_dict(self) -> Dict[str, float]:
        return {
            "sigma_home": self.sigma_home, "sigma_away": self.sigma_away,
            "rho": self.rho, "var_margin": self.var_margin,
            "var_total": self.var_total, "cov_margin_total": self.cov_margin_total,
            "corr_margin_total": self.corr_margin_total,
        }


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------

def params_filename(season: int, week: int) -> str:
    return f"nfl_{season}_w{week:02d}.json"


def parse_params_filename(name: str) -> Optional[Tuple[int, int]]:
    m = _FILENAME_RE.match(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def list_params(directory: Path | str) -> List[Tuple[Tuple[int, int], Path]]:
    """All params files in ``directory`` sorted by (season, week)."""
    root = Path(directory)
    if not root.exists():
        return []
    found = []
    for p in root.iterdir():
        key = parse_params_filename(p.name)
        if key is not None and p.is_file():
            found.append((key, p))
    return sorted(found)


def latest_params(directory: Path | str, before: Optional[Tuple[int, int]] = None) -> Optional[Path]:
    """Newest params file, optionally strictly before ``(season, week)``."""
    files = [p for key, p in list_params(directory) if before is None or key < before]
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _canonicalize(obj: Any) -> Any:
    if isinstance(obj, bool) or obj is None or isinstance(obj, (str, int)):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ParamsError(f"non-finite float {obj!r} in params")
        value = round(obj, FLOAT_DP)
        return 0.0 if value == 0 else value  # no "-0.0"
    if isinstance(obj, dict):
        return {str(k): _canonicalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(v) for v in obj]
    # numpy scalars and friends
    if hasattr(obj, "item"):
        return _canonicalize(obj.item())
    raise ParamsError(f"unserializable value of type {type(obj).__name__}")


def canonical_json_bytes(params: Dict[str, Any]) -> bytes:
    return (json.dumps(_canonicalize(params), sort_keys=True, indent=2) + "\n").encode("utf-8")


def write_params(params: Dict[str, Any], path: Path | str) -> Path:
    """Validate then write canonically. Returns the path written."""
    validate_params(params)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(params))
    return path


def load_params(path: Path | str) -> Dict[str, Any]:
    params = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_params(params)
    return params


# ---------------------------------------------------------------------------
# Evaluation (hot-path safe)
# ---------------------------------------------------------------------------

def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def team_sigma(league: Dict[str, Any], teams: Dict[str, Any], mu: float,
               offense: Optional[str] = None, defense: Optional[str] = None) -> float:
    """Score standard deviation for a team expected to score ``mu`` points."""
    model = league["variance_model"]
    var = float(league["var_intercept"])
    if model in ("mean_linear", "mean_linear_team"):
        var += float(league["var_slope"]) * mu
    if model == "mean_linear_team":
        off = teams.get(offense or "", {}).get("off_var_factor", 1.0)
        dfn = teams.get(defense or "", {}).get("def_var_factor", 1.0)
        var *= float(off) * float(dfn)
    sigma = math.sqrt(max(var, 1e-9))
    return _clamp(sigma, float(league["sigma_min"]), float(league["sigma_max"]))


def matchup_covariance(params: Dict[str, Any], home: str, away: str,
                       mu_home: float, mu_away: float,
                       corr_scale: float = 1.0) -> MatchupCovariance:
    """Score covariance for one game given the (market-implied) score means.

    ``corr_scale`` scales every source of leg dependence together -- used for
    the sensitivity-to-correlation report: ``rho -> c * rho`` and the
    home/away variance asymmetry ``sigma_i^2 - mean(sigma^2) -> c * (...)``.
    ``c = 0`` makes margin and total independent (the naive product for
    margin x total combos); ``c = 1`` is the fitted model.
    """
    league = params["league"]
    teams = params.get("teams", {})
    sh = team_sigma(league, teams, mu_home, offense=home, defense=away)
    sa = team_sigma(league, teams, mu_away, offense=away, defense=home)
    rho = float(league["rho"])
    if corr_scale != 1.0:
        vbar = 0.5 * (sh * sh + sa * sa)
        vh = max(vbar + corr_scale * (sh * sh - vbar), 1e-6)
        va = max(vbar + corr_scale * (sa * sa - vbar), 1e-6)
        sh, sa = math.sqrt(vh), math.sqrt(va)
        rho = _clamp(corr_scale * rho, -0.95, 0.95)
    return MatchupCovariance(sigma_home=sh, sigma_away=sa, rho=rho)


# ---------------------------------------------------------------------------
# Schema validation (also the refresh "range sanity" gate)
# ---------------------------------------------------------------------------

_LEAGUE_KEYS = ("variance_model", "var_intercept", "var_slope", "rho", "sigma_min",
                "sigma_max", "mean_points", "residual_bias", "n_games", "seasons")
_TEAM_KEYS = ("off_var_factor", "def_var_factor", "off_mean", "def_mean", "n_current", "n_prior")
_GAME_KEYS = ("game_id", "home", "away", "mu_home", "mu_away", "sigma_home", "sigma_away",
              "rho", "var_margin", "var_total", "cov_margin_total", "corr_margin_total")

SIGMA_BOUNDS = (4.0, 20.0)      # plausible NFL team-score SD, points
FACTOR_BOUNDS = (0.3, 3.0)
MEAN_POINTS_BOUNDS = (10.0, 40.0)


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_params(params: Dict[str, Any]) -> None:
    """Raise :class:`ParamsError` listing every schema/range violation."""
    problems: List[str] = []

    def need(container: Dict[str, Any], keys, where: str) -> bool:
        missing = [k for k in keys if k not in container]
        if missing:
            problems.append(f"{where}: missing {missing}")
        return not missing

    if not isinstance(params, dict):
        raise ParamsError("params must be a JSON object")
    need(params, ("schema_version", "sport", "model_version", "season", "week", "as_of",
                  "data_vintage", "estimator", "league", "teams", "games"), "root")
    if params.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version {params.get('schema_version')!r} != {SCHEMA_VERSION}")
    if params.get("sport") != "nfl":
        problems.append("sport must be 'nfl'")

    league = params.get("league", {})
    if isinstance(league, dict) and need(league, _LEAGUE_KEYS, "league"):
        if league["variance_model"] not in VARIANCE_MODELS:
            problems.append(f"league.variance_model {league['variance_model']!r} unknown")
        for k in ("var_intercept", "var_slope", "rho", "sigma_min", "sigma_max", "mean_points", "residual_bias"):
            if not _num(league[k]):
                problems.append(f"league.{k} not a finite number")
        if _num(league["rho"]) and not -1.0 < league["rho"] < 1.0:
            problems.append(f"league.rho {league['rho']} outside (-1, 1)")
        if _num(league["sigma_min"]) and _num(league["sigma_max"]):
            if not SIGMA_BOUNDS[0] <= league["sigma_min"] < league["sigma_max"] <= SIGMA_BOUNDS[1]:
                problems.append("league.sigma_min/sigma_max outside plausible bounds")
        if _num(league["mean_points"]) and not MEAN_POINTS_BOUNDS[0] <= league["mean_points"] <= MEAN_POINTS_BOUNDS[1]:
            problems.append(f"league.mean_points {league['mean_points']} implausible")
        if _num(league.get("n_games")) and league["n_games"] <= 0:
            problems.append("league.n_games must be positive")

    teams = params.get("teams", {})
    if isinstance(teams, dict):
        for code, t in teams.items():
            if not need(t, _TEAM_KEYS, f"teams.{code}"):
                continue
            for k in ("off_var_factor", "def_var_factor"):
                if not _num(t[k]) or not FACTOR_BOUNDS[0] <= t[k] <= FACTOR_BOUNDS[1]:
                    problems.append(f"teams.{code}.{k} {t[k]!r} out of bounds")
            for k in ("off_mean", "def_mean"):
                if not _num(t[k]) or not 0 < t[k] < 60:
                    problems.append(f"teams.{code}.{k} {t[k]!r} implausible")

    games = params.get("games", [])
    if isinstance(games, list):
        for i, g in enumerate(games):
            if not need(g, _GAME_KEYS, f"games[{i}]"):
                continue
            for k in _GAME_KEYS[3:]:
                if not _num(g[k]):
                    problems.append(f"games[{i}].{k} not a finite number")
            if _num(g["sigma_home"]) and _num(g["sigma_away"]):
                for k in ("sigma_home", "sigma_away"):
                    if not SIGMA_BOUNDS[0] <= g[k] <= SIGMA_BOUNDS[1]:
                        problems.append(f"games[{i}].{k} {g[k]} outside {SIGMA_BOUNDS}")
            if _num(g["rho"]) and not -1.0 < g["rho"] < 1.0:
                problems.append(f"games[{i}].rho outside (-1, 1)")
            if _num(g["var_margin"]) and g["var_margin"] <= 0:
                problems.append(f"games[{i}].var_margin not positive")
            if _num(g["var_total"]) and g["var_total"] <= 0:
                problems.append(f"games[{i}].var_total not positive")
            if _num(g["corr_margin_total"]) and not -1.0 < g["corr_margin_total"] < 1.0:
                problems.append(f"games[{i}].corr_margin_total outside (-1, 1)")
    else:
        problems.append("games must be a list")

    if problems:
        raise ParamsError("; ".join(problems))

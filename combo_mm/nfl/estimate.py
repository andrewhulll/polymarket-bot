"""Walk-forward NFL score-covariance estimator ("history for shape").

How correlations are estimated
------------------------------
The pricer models a game's final scores as bivariate normal,
``(S_home, S_away) ~ BVN(mu, Sigma)``. The market sets ``mu`` (location); this
module estimates ``Sigma`` (shape) from history.

1. **Residuals against the closing line, not raw scores.** Raw home/away
   scores mix within-game noise with between-matchup differences in expected
   scoring (a 55-total game vs a 38-total game), which biases both variance
   (up) and correlation (towards negative). Each team's closing-line implied
   points are ``mu_home = (total_line + spread_line) / 2`` and
   ``mu_away = (total_line - spread_line) / 2``; the residual is
   ``e = score - mu``. The closing line is the best public estimate of the
   mean, so residual moments are the conditional moments the pricer needs.
2. **Recency-weighted trailing window.** Only games strictly before the
   ``as_of`` (season, week) cutoff are used -- no future information by
   construction. Within the trailing ``window_seasons`` (``None`` = all prior
   history) each game gets weight
   ``0.5 ** (age_in_seasons / half_life_seasons)``. At least ``min_seasons``
   of prior seasons are required.
3. **Variance model.** ``league_constant``: one weighted variance.
   ``mean_linear``: weighted least squares of ``e^2`` on ``[1, mu]`` --
   ``sigma^2(mu) = a + b*mu``. Empirically ``b > 0``: teams expected to score
   more have noisier scores, so favorites have larger ``sigma``. Because
   ``Cov(margin, total) = sigma_home^2 - sigma_away^2``, this is the mechanism
   behind "favorite covers & over" being positively correlated.
   ``mean_linear_team`` additionally multiplies by per-team offensive and
   defensive variance factors (below).
4. **Within-game correlation.** ``rho`` is the weighted correlation of the
   *standardized* residuals ``z = e / sigma(mu)`` of the two teams in each
   game. It is estimated league-wide only: per-team ``rho`` from ~17 games a
   season is noise.
5. **Per-team shrinkage.** For team factors (variance ratio
   ``e^2 / sigma^2(mu)`` averaged over the team's games on offense or defense)
   and team scoring means:

   - prior = recency-weighted value from previous seasons in the window,
     regressed toward the league value by ``offseason_regression``
     (``prior = league + (1 - r) * (raw_prior - league)``), with a
     ``shrink_k_games`` pseudo-count toward league;
   - current = the team's games so far this season;
   - estimate = ``w * current + (1 - w) * prior`` with ``w = n / (n + k)``.
     Scoring means use ``k = shrink_k_games = 5`` (Weeks 1-4 mostly prior,
     mostly data by Week 8, per the issue). Variance factors use
     ``k * var_shrink_multiplier = 15`` and winsorize each game's ratio at
     ``ratio_cap = 9`` (a 3-sigma residual): a single squared residual is
     chi-square(1) noise (SD ~1.4x its mean), so one blowout would otherwise
     swing a team's variance by 50%+ in Week 2.

Every output is deterministic for a fixed game list and config.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from combo_mm.nfl import MODEL_VERSION
from combo_mm.nfl.ingest import Game
from combo_mm.nfl.params_io import (
    SCHEMA_VERSION,
    VARIANCE_MODELS,
    matchup_covariance,
)

__all__ = [
    "EstimatorConfig",
    "InsufficientHistory",
    "ResidualTable",
    "estimate_params",
    "implied_means",
]

WEEKS_PER_SEASON = 22.0   # REG + playoffs, used only to age games within a season


class InsufficientHistory(ValueError):
    """Not enough prior seasons before the cutoff to estimate parameters."""


@dataclass(frozen=True)
class EstimatorConfig:
    variance_model: str = "mean_linear"
    window_seasons: Optional[int] = 4   # trailing full seasons (+ current season to date); None = all history
    min_seasons: int = 3             # prior seasons required in the window
    half_life_seasons: float = 2.0
    shrink_k_games: float = 5.0
    var_shrink_multiplier: float = 3.0   # variance factors shrink with k * this
    ratio_cap: float = 9.0               # winsorize per-game e^2/sigma^2 at a 3-sigma residual
    offseason_regression: float = 0.5
    sigma_min: float = 5.0
    sigma_max: float = 16.0
    factor_min: float = 0.6
    factor_max: float = 1.6

    def __post_init__(self) -> None:
        if self.variance_model not in VARIANCE_MODELS:
            raise ValueError(f"variance_model must be one of {VARIANCE_MODELS}")
        if self.min_seasons < 1 or (self.window_seasons is not None and self.window_seasons < self.min_seasons):
            raise ValueError("need 1 <= min_seasons <= window_seasons")
        if self.var_shrink_multiplier < 0 or self.ratio_cap <= 1.0:
            raise ValueError("var_shrink_multiplier must be >= 0 and ratio_cap > 1")
        if self.half_life_seasons <= 0 or self.shrink_k_games < 0:
            raise ValueError("half_life_seasons must be > 0 and shrink_k_games >= 0")
        if not 0.0 <= self.offseason_regression <= 1.0:
            raise ValueError("offseason_regression must be in [0, 1]")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def implied_means(spread_line: float, total_line: float) -> Tuple[float, float]:
    """Closing-line implied (home, away) points."""
    return (total_line + spread_line) / 2.0, (total_line - spread_line) / 2.0


def _np():
    import numpy as np  # offline dependency, imported lazily
    return np


@dataclass
class ResidualTable:
    """Columnar view of all played games with lines (built once, sliced per cutoff)."""

    games: List[Game]
    season: Any = field(repr=False)
    week: Any = field(repr=False)
    home_idx: Any = field(repr=False)
    away_idx: Any = field(repr=False)
    mu_home: Any = field(repr=False)
    mu_away: Any = field(repr=False)
    home_score: Any = field(repr=False)
    away_score: Any = field(repr=False)
    teams: List[str] = field(default_factory=list)

    @classmethod
    def build(cls, games: Iterable[Game]) -> "ResidualTable":
        np = _np()
        usable = sorted(
            (g for g in games if g.played and g.has_lines),
            key=lambda g: (g.season, g.week, g.game_id),
        )
        teams = sorted({g.home for g in usable} | {g.away for g in usable})
        index = {t: i for i, t in enumerate(teams)}
        mh = np.array([implied_means(g.spread_line, g.total_line)[0] for g in usable], dtype=float)
        ma = np.array([implied_means(g.spread_line, g.total_line)[1] for g in usable], dtype=float)
        return cls(
            games=usable,
            season=np.array([g.season for g in usable], dtype=int),
            week=np.array([g.week for g in usable], dtype=int),
            home_idx=np.array([index[g.home] for g in usable], dtype=int),
            away_idx=np.array([index[g.away] for g in usable], dtype=int),
            mu_home=mh,
            mu_away=ma,
            home_score=np.array([g.home_score for g in usable], dtype=float),
            away_score=np.array([g.away_score for g in usable], dtype=float),
            teams=teams,
        )

    def before(self, season: int, week: int):
        """Boolean mask of games strictly before (season, week)."""
        np = _np()
        return (self.season < season) | ((self.season == season) & (self.week < week))


def _wmean(np, x, w) -> float:
    sw = float(w.sum())
    return float((w * x).sum() / sw) if sw > 0 else float("nan")


def _fit_variance(np, mu, e2, w, model: str) -> Tuple[float, float]:
    """Weighted fit of E[e^2 | mu]: constant, or ``a + b*mu`` (WLS)."""
    if model == "league_constant":
        return _wmean(np, e2, w), 0.0
    X = np.stack([np.ones_like(mu), mu], axis=1)
    XtW = X.T * w
    a, b = np.linalg.solve(XtW @ X, XtW @ e2)
    return float(a), float(b)


def _shrunk(current_sum: float, n_current: int, prior_value: float, k: float) -> float:
    if n_current <= 0:
        return prior_value
    current = current_sum / n_current
    w = n_current / (n_current + k) if (n_current + k) > 0 else 1.0
    return w * current + (1.0 - w) * prior_value


def _prior(weighted_sum: float, weight: float, league_value: float, k: float,
           cfg: EstimatorConfig) -> float:
    """Prior-season value: pseudo-count shrink, then offseason regression to league."""
    raw = (weighted_sum + k * league_value) / (weight + k) if (weight + k) > 0 else league_value
    return league_value + (1.0 - cfg.offseason_regression) * (raw - league_value)


def estimate_params(games_or_table, season: int, week: int,
                    config: Optional[EstimatorConfig] = None,
                    data_vintage: Optional[Dict[str, Any]] = None,
                    schedule: Optional[Sequence[Game]] = None) -> Dict[str, Any]:
    """Estimate the params dict for ``(season, week)`` from games strictly before it.

    ``schedule`` (optional) lists the games to emit per-game entries for --
    usually the games of the target week. Their ``mu`` comes from their
    closing/current lines when present, else the league mean.
    """
    np = _np()
    cfg = config or EstimatorConfig()
    table = games_or_table if isinstance(games_or_table, ResidualTable) else ResidualTable.build(games_or_table)

    first_season = season - cfg.window_seasons if cfg.window_seasons is not None else -10 ** 6
    mask = table.before(season, week) & (table.season >= first_season)
    prior_seasons = sorted({int(s) for s in table.season[mask] if s < season})
    if len(prior_seasons) < cfg.min_seasons:
        raise InsufficientHistory(
            f"{season} w{week}: {len(prior_seasons)} prior seasons in window, need {cfg.min_seasons}"
        )

    s = table.season[mask]
    wk = table.week[mask]
    age = (season - s) + (week - wk) / WEEKS_PER_SEASON
    w_game = 0.5 ** (age / cfg.half_life_seasons)

    mh, ma = table.mu_home[mask], table.mu_away[mask]
    eh = table.home_score[mask] - mh
    ea = table.away_score[mask] - ma
    hi, ai = table.home_idx[mask], table.away_idx[mask]

    # Team-rows: one per (game, scoring team).
    mu = np.concatenate([mh, ma])
    e = np.concatenate([eh, ea])
    w = np.concatenate([w_game, w_game])
    off_idx = np.concatenate([hi, ai])
    def_idx = np.concatenate([ai, hi])
    row_season = np.concatenate([s, s])
    points = mu + e

    bias = _wmean(np, e, w)
    ec = e - bias
    e2 = ec * ec
    a, b = _fit_variance(np, mu, e2, w, cfg.variance_model)
    base_var = np.clip(a + b * mu, cfg.sigma_min ** 2, cfg.sigma_max ** 2)

    z = ec / np.sqrt(base_var)
    n = len(eh)
    zh, za = z[:n], z[n:]
    rho = float((w_game * zh * za).sum() / np.sqrt((w_game * zh * zh).sum() * (w_game * za * za).sum()))

    mean_points = _wmean(np, points, w)
    ratio = np.minimum(e2 / base_var, cfg.ratio_cap)
    k_mean = cfg.shrink_k_games
    k_var = cfg.shrink_k_games * cfg.var_shrink_multiplier

    teams_out: Dict[str, Dict[str, Any]] = {}
    for t_i, team in enumerate(table.teams):
        entry: Dict[str, Any] = {}
        for role, idx in (("off", off_idx), ("def", def_idx)):
            sel = idx == t_i
            cur = sel & (row_season == season)
            pri = sel & (row_season < season)
            n_cur = int(cur.sum())
            f_prior = _prior(float((w[pri] * ratio[pri]).sum()), float(w[pri].sum()), 1.0, k_var, cfg)
            factor = _shrunk(float(ratio[cur].sum()), n_cur, f_prior, k_var)
            m_prior = _prior(float((w[pri] * points[pri]).sum()), float(w[pri].sum()), mean_points, k_mean, cfg)
            mean_val = _shrunk(float(points[cur].sum()), n_cur, m_prior, k_mean)
            entry[f"{role}_var_factor"] = min(max(factor, cfg.factor_min), cfg.factor_max)
            entry[f"{role}_mean"] = mean_val
            if role == "off":
                entry["n_current"] = n_cur
                entry["n_prior"] = int(pri.sum())
        if entry["n_current"] or entry["n_prior"]:
            teams_out[team] = entry

    params: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sport": "nfl",
        "model_version": MODEL_VERSION,
        "season": int(season),
        "week": int(week),
        "as_of": {"season": int(season), "week": int(week), "rule": "games strictly before"},
        "data_vintage": dict(data_vintage or {}),
        "estimator": cfg.to_dict(),
        "league": {
            "variance_model": cfg.variance_model,
            "var_intercept": a,
            "var_slope": b,
            "rho": rho,
            "sigma_min": cfg.sigma_min,
            "sigma_max": cfg.sigma_max,
            "mean_points": mean_points,
            "residual_bias": bias,
            "sigma_at_mean_points": float(np.sqrt(np.clip(a + b * mean_points, cfg.sigma_min ** 2, cfg.sigma_max ** 2))),
            "n_games": int(n),
            "seasons": sorted({int(x) for x in s}),
        },
        "teams": teams_out,
        "games": [],
    }

    for g in sorted(schedule or [], key=lambda g: g.game_id):
        if g.has_lines:
            mu_h, mu_a = implied_means(g.spread_line, g.total_line)
        else:
            mu_h = mu_a = mean_points
        cov = matchup_covariance(params, g.home, g.away, mu_h, mu_a)
        params["games"].append({
            "game_id": g.game_id, "home": g.home, "away": g.away,
            "spread_line": g.spread_line, "total_line": g.total_line,
            "lines_available": g.has_lines,
            "mu_home": mu_h, "mu_away": mu_a, **cov.to_dict(),
        })
    return params

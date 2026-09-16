"""Joint probability of NFL score legs under the bivariate-normal score model.

Canonical form (issue #2): every Phase A leg is a linear inequality on the
final scores ``S = (S_home, S_away)``:

=====================  ==========  =============================
leg                    row ``a``   wins iff
=====================  ==========  =============================
home moneyline         (1, -1)     ``a.S > 0``
away moneyline         (1, -1)     ``a.S < 0``
home covers ``x``      (1, -1)     ``a.S > x`` (x = home expected margin line)
away covers ``x``      (1, -1)     ``a.S < x``
over ``U``             (1, 1)      ``a.S > U``
under ``U``            (1, 1)      ``a.S < U``
home team over ``t``   (1, 0)      ``a.S > t``
away team over ``t``   (0, 1)      ``a.S > t``
=====================  ==========  =============================

Discreteness: scores are integers and every row has integer coefficients, so
``a.S`` is an integer. "``a.S > L``" is therefore ``a.S >= floor(L) + 1``,
evaluated on the continuous model as ``a.S > floor(L) + 0.5`` (continuity
correction). Integer lines can **push** (``a.S == L``, the band
``(L - 0.5, L + 0.5)``). Pushed combos are dropped from the backtest (settlement
rule TBD), so model probabilities are **conditional on no leg pushing** --
the same basis as de-vigged sportsbook prices, where pushes refund.

A combo's joint probability is a Gaussian probability over a convex polygon
in 2-D (intersection of half-planes) -- computed by integrating the
conditional normal of one whitened coordinate over a dense grid of the other
(error ~1e-5), which handles any number of same-game legs, including
degenerate stacks the scipy MVN CDF cannot (3+ legs on a 2-D score).
The no-push probability uses inclusion-exclusion over pushable legs.

"Market for location": :func:`calibrate_means` solves for ``(mu_home,
mu_away)`` such that the model's spread and total marginals match the
market's de-vigged prices, holding the historical covariance fixed. Margin
depends only on ``mu_home - mu_away`` and total only on ``mu_home +
mu_away``, so each is a 1-D monotone root find; when the covariance itself
depends on ``mu`` (``mean_linear`` variance), the two are iterated to a fixed
point.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from combo_mm.nfl.params_io import MatchupCovariance

__all__ = [
    "MARGIN",
    "TOTAL",
    "HOME_POINTS",
    "AWAY_POINTS",
    "Leg",
    "GameModel",
    "home_ml",
    "away_ml",
    "home_cover",
    "away_cover",
    "over",
    "under",
    "home_team_over",
    "away_team_over",
    "home_team_under",
    "away_team_under",
    "settle_leg",
    "leg_probability",
    "joint_probability",
    "region_probability",
    "CalibrationResult",
    "calibrate_means",
]

MARGIN = (1.0, -1.0)
TOTAL = (1.0, 1.0)
HOME_POINTS = (1.0, 0.0)
AWAY_POINTS = (0.0, 1.0)

WIN, LOSE, PUSH = "win", "lose", "push"

_GRID_N = 1201
_GRID_Z = 8.5
# Fixed irrational rotation of the whitened plane: keeps every canonical row
# off the integration axis so the integrand is continuous (O(h^2) error).
_ROT = 0.4112339  # radians; any angle not aligned with a canonical row works


@dataclass(frozen=True)
class Leg:
    name: str
    row: Tuple[float, float]
    line: float
    direction: int          # +1: wins iff a.S > line ; -1: wins iff a.S < line

    @property
    def pushable(self) -> bool:
        return float(self.line).is_integer()

    def win_bounds(self) -> Tuple[float, float]:
        if self.direction > 0:
            return (math.floor(self.line) + 0.5, math.inf)
        return (-math.inf, math.ceil(self.line) - 0.5)

    def push_bounds(self) -> Tuple[float, float]:
        return (self.line - 0.5, self.line + 0.5)


def home_ml() -> Leg:
    return Leg("home_ml", MARGIN, 0.0, +1)


def away_ml() -> Leg:
    return Leg("away_ml", MARGIN, 0.0, -1)


def home_cover(spread_line: float) -> Leg:
    """Home side of a spread quoted as the home expected margin (nflverse ``spread_line``)."""
    return Leg(f"home_cover({spread_line:g})", MARGIN, float(spread_line), +1)


def away_cover(spread_line: float) -> Leg:
    return Leg(f"away_cover({spread_line:g})", MARGIN, float(spread_line), -1)


def over(total_line: float) -> Leg:
    return Leg(f"over({total_line:g})", TOTAL, float(total_line), +1)


def under(total_line: float) -> Leg:
    return Leg(f"under({total_line:g})", TOTAL, float(total_line), -1)


def home_team_over(line: float) -> Leg:
    return Leg(f"home_team_over({line:g})", HOME_POINTS, float(line), +1)


def away_team_over(line: float) -> Leg:
    return Leg(f"away_team_over({line:g})", AWAY_POINTS, float(line), +1)


def home_team_under(line: float) -> Leg:
    return Leg(f"home_team_under({line:g})", HOME_POINTS, float(line), -1)


def away_team_under(line: float) -> Leg:
    return Leg(f"away_team_under({line:g})", AWAY_POINTS, float(line), -1)


def settle_leg(leg: Leg, home_score: int, away_score: int) -> str:
    value = leg.row[0] * home_score + leg.row[1] * away_score
    if value == leg.line:
        return PUSH
    won = value > leg.line if leg.direction > 0 else value < leg.line
    return WIN if won else LOSE


# ---------------------------------------------------------------------------
# Probability engine
# ---------------------------------------------------------------------------

Constraint = Tuple[Tuple[float, float], float, float]


def _norm_cdf(x: float) -> float:
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


_GRID_CACHE: dict = {}


def _grid():
    key = (_GRID_N, _GRID_Z)
    if key not in _GRID_CACHE:
        import numpy as np
        z = np.linspace(-_GRID_Z, _GRID_Z, _GRID_N)
        h = z[1] - z[0]
        w = np.full(_GRID_N, h)
        w[0] = w[-1] = h / 2
        w *= np.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
        _GRID_CACHE[key] = (z, w)
    return _GRID_CACHE[key]


class GameModel:
    """Score distribution of one game: ``S ~ N(mean, cov)``.

    Memoizes region probabilities, so pricing many combos on the same game
    (which share push bands and often legs) pays for each distinct region
    once.
    """

    def __init__(self, mean: Sequence[float], cov: MatchupCovariance) -> None:
        self.mean = (float(mean[0]), float(mean[1]))
        self.cov = cov
        (v11, v12), (_, v22) = cov.matrix()
        l11 = math.sqrt(v11)
        l21 = v12 / l11
        l22 = math.sqrt(max(v22 - l21 * l21, 1e-12))
        c, s = math.cos(_ROT), math.sin(_ROT)
        # S = mean + L R u with u ~ N(0, I); columns of L R.
        self._col1 = (l11 * c, l21 * c + l22 * s)
        self._col2 = (-l11 * s, -l21 * s + l22 * c)
        self._cache: dict = {}

    def region(self, constraints: Sequence[Constraint]) -> float:
        """``P(lo < a.S < hi for every (a, lo, hi))``."""
        if not constraints:
            return 1.0
        key = tuple(sorted(constraints))
        hit = self._cache.get(key)
        if hit is None:
            hit = self._cache[key] = self._integrate(key)
        return hit

    def _integrate(self, constraints: Sequence[Constraint]) -> float:
        import numpy as np
        from scipy.special import ndtr  # offline dependency, imported lazily
        z1, w = _grid()
        lower = np.full_like(z1, -np.inf)
        upper = np.full_like(z1, np.inf)
        mask = None
        for (a0, a1), lo, hi in constraints:
            centre = a0 * self.mean[0] + a1 * self.mean[1]
            g1 = a0 * self._col1[0] + a1 * self._col1[1]
            g2 = a0 * self._col2[0] + a1 * self._col2[1]
            base = centre + g1 * z1
            if abs(g2) < 1e-12:
                ok = (base > lo) & (base < hi)
                mask = ok if mask is None else mask & ok
                continue
            t_lo = (lo - base) / g2
            t_hi = (hi - base) / g2
            if g2 < 0:
                t_lo, t_hi = t_hi, t_lo
            np.maximum(lower, t_lo, out=lower)
            np.minimum(upper, t_hi, out=upper)
        live = lower < upper
        if mask is not None:
            live &= mask
        if not live.any():
            return 0.0
        inner = ndtr(upper[live]) - ndtr(lower[live])
        return float(min(max((w[live] * inner).sum(), 0.0), 1.0))

    def push_free(self, legs: Sequence[Leg]) -> float:
        """P(no leg pushes), inclusion-exclusion over distinct push bands."""
        bands = sorted({(leg.row, leg.line) for leg in legs if leg.pushable})
        total = 1.0
        for r in range(1, len(bands) + 1):
            for subset in itertools.combinations(bands, r):
                total += (-1) ** r * self.region([(row, line - 0.5, line + 0.5) for row, line in subset])
        return total

    def joint(self, legs: Sequence[Leg], condition_on_no_push: bool = True) -> float:
        """P(every leg wins [| no leg pushes])."""
        if not legs:
            return 1.0
        p = self.region([(leg.row, *leg.win_bounds()) for leg in legs])
        if condition_on_no_push and any(leg.pushable for leg in legs):
            denom = self.push_free(legs)
            p = p / denom if denom > 1e-12 else 0.0
        return min(max(p, 0.0), 1.0)

    def leg(self, leg: Leg, condition_on_no_push: bool = True) -> float:
        return leg_probability(leg, self.mean, self.cov, condition_on_no_push)


def region_probability(mean: Sequence[float], cov: MatchupCovariance,
                       constraints: Sequence[Constraint]) -> float:
    """``P(lo < a.S < hi for every (a, lo, hi))`` for ``S ~ N(mean, cov)``."""
    return GameModel(mean, cov).region(constraints)


def joint_probability(legs: Sequence[Leg], mean: Sequence[float], cov: MatchupCovariance,
                      condition_on_no_push: bool = True) -> float:
    """P(every leg wins [| no leg pushes]) for one game."""
    return GameModel(mean, cov).joint(legs, condition_on_no_push)


def leg_probability(leg: Leg, mean: Sequence[float], cov: MatchupCovariance,
                    condition_on_no_push: bool = True) -> float:
    """Closed-form single-leg probability (1-D normal)."""
    (v11, v12), (_, v22) = cov.matrix()
    a0, a1 = leg.row
    m = a0 * mean[0] + a1 * mean[1]
    sd = math.sqrt(max(a0 * a0 * v11 + 2 * a0 * a1 * v12 + a1 * a1 * v22, 1e-12))
    lo, hi = leg.win_bounds()
    p = _norm_cdf((hi - m) / sd) - _norm_cdf((lo - m) / sd)
    if condition_on_no_push and leg.pushable:
        plo, phi = leg.push_bounds()
        push = _norm_cdf((phi - m) / sd) - _norm_cdf((plo - m) / sd)
        p = p / (1.0 - push) if push < 1.0 else 0.0
    return min(max(p, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Market-implied means ("market for location")
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalibrationResult:
    mu_home: float
    mu_away: float
    cov: MatchupCovariance
    iterations: int
    converged: bool


def _solve_monotone(f: Callable[[float], float], target: float, lo: float, hi: float,
                    tol: float = 1e-9) -> float:
    """Root of increasing ``f(x) = target`` on [lo, hi] (Brent); clamps outside."""
    from scipy.optimize import brentq  # offline dependency, imported lazily
    flo, fhi = f(lo) - target, f(hi) - target
    if flo >= 0:
        return lo
    if fhi <= 0:
        return hi
    return float(brentq(lambda x: f(x) - target, lo, hi, xtol=tol))


def calibrate_means(spread_line: float, p_home_cover: float, total_line: float, p_over: float,
                    cov_fn: Callable[[float, float], MatchupCovariance],
                    tol: float = 1e-7, max_iter: int = 50) -> CalibrationResult:
    """Solve ``(mu_home, mu_away)`` so model spread/total marginals hit market prices.

    ``cov_fn(mu_home, mu_away)`` returns the covariance at those means (the
    params-file evaluation, possibly mean-dependent). Starts from the
    closing-line implied means.
    """
    p_home_cover = min(max(p_home_cover, 1e-4), 1 - 1e-4)
    p_over = min(max(p_over, 1e-4), 1 - 1e-4)
    mu_m, mu_t = float(spread_line), float(total_line)
    cover_leg, over_leg = home_cover(spread_line), over(total_line)
    cov = cov_fn((mu_t + mu_m) / 2, (mu_t - mu_m) / 2)
    converged = False
    it = 0
    for it in range(1, max_iter + 1):
        def p_cover(m: float, cov=cov, t=mu_t) -> float:
            return leg_probability(cover_leg, ((t + m) / 2, (t - m) / 2), cov)

        new_m = _solve_monotone(p_cover, p_home_cover, spread_line - 40.0, spread_line + 40.0)

        def p_ov(t: float, cov=cov, m=new_m) -> float:
            return leg_probability(over_leg, ((t + m) / 2, (t - m) / 2), cov)

        new_t = _solve_monotone(p_ov, p_over, total_line - 60.0, total_line + 60.0)
        delta = max(abs(new_m - mu_m), abs(new_t - mu_t))
        mu_m, mu_t = new_m, new_t
        cov = cov_fn((mu_t + mu_m) / 2, (mu_t - mu_m) / 2)
        if delta < tol:
            converged = True
            break
    return CalibrationResult(
        mu_home=(mu_t + mu_m) / 2, mu_away=(mu_t - mu_m) / 2,
        cov=cov, iterations=it, converged=converged,
    )

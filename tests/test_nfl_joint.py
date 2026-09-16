"""Joint probability engine and market-implied mean solver."""
import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
from scipy.stats import multivariate_normal as mvn  # noqa: E402

from combo_mm.nfl.joint import (  # noqa: E402
    PUSH, WIN, LOSE, GameModel, away_cover, away_ml, calibrate_means, home_cover, home_ml,
    home_team_over, joint_probability, leg_probability, over, settle_leg, under,
)
from combo_mm.nfl.params_io import MatchupCovariance, matchup_covariance  # noqa: E402

COV = MatchupCovariance(10.2, 8.4, 0.08)
MEAN = (26.0, 19.0)


def _bvn_margin_total_upper(mu_m, mu_t, x, u, cov):
    vm, vt, c = cov.var_margin, cov.var_total, cov.cov_margin_total
    joint = mvn(mean=[mu_m, mu_t], cov=[[vm, c], [c, vt]]).cdf([x, u])
    return 1 - mvn(mu_m, vm).cdf(x) - mvn(mu_t, vt).cdf(u) + joint


@pytest.mark.parametrize("mu_m, spread", [(7.0, 3.5), (2.0, 6.5), (-3.0, 2.5), (12.0, 13.5)])
def test_matches_exact_bivariate_normal(mu_m, spread):
    mean = ((45 + mu_m) / 2, (45 - mu_m) / 2)
    p = joint_probability([home_cover(spread), over(44.5)], mean, COV)
    assert p == pytest.approx(_bvn_margin_total_upper(mu_m, 45, spread, 44.5, COV), abs=1e-5)


def test_three_legs_with_pushes_match_monte_carlo():
    rng = np.random.default_rng(0)
    # Continuity-corrected model: simulate the continuous scores, settle on the half-lattice.
    s = rng.multivariate_normal(MEAN, COV.matrix(), 1_000_000)
    legs = [home_ml(), under(47), home_team_over(24.5)]
    m, t = s[:, 0] - s[:, 1], s[:, 0] + s[:, 1]
    win = (m > 0.5) & (t < 46.5) & (s[:, 0] > 24.5)
    push = (np.abs(m) < 0.5) | (np.abs(t - 47) < 0.5)
    mc = win.sum() / (~push).sum()
    assert joint_probability(legs, MEAN, COV) == pytest.approx(mc, abs=0.002)


def test_single_leg_grid_matches_closed_form():
    for leg in (home_ml(), home_cover(3.0), under(44.5), home_team_over(27.5)):
        assert GameModel(MEAN, COV).joint([leg]) == pytest.approx(leg_probability(leg, MEAN, COV), abs=1e-6)


def test_impossible_and_nested_combos():
    assert joint_probability([home_ml(), away_ml()], MEAN, COV) == 0.0
    assert joint_probability([over(44.5), under(44.5)], MEAN, COV) == 0.0
    # Covering -3.5 implies winning: P(win & cover) == P(cover).
    both = joint_probability([home_ml(), home_cover(3.5)], MEAN, COV, condition_on_no_push=False)
    assert both == pytest.approx(leg_probability(home_cover(3.5), MEAN, COV, condition_on_no_push=False), abs=1e-6)


def test_family_partitions_sum_to_one_with_push_conditioning():
    gm = GameModel(MEAN, COV)
    total = sum(gm.joint([ml, tot]) for ml in (home_ml(), away_ml()) for tot in (over(45), under(45)))
    assert total == pytest.approx(1.0, abs=1e-5)


def test_independence_when_margin_and_total_uncorrelated():
    cov = MatchupCovariance(9.0, 9.0, 0.0)  # Cov(M, T) = 0 and jointly normal -> independent
    for legs in ([home_cover(3.5), over(44.5)], [away_ml(), under(47)]):
        p = joint_probability(legs, MEAN, cov)
        prod = math.prod(leg_probability(leg, MEAN, cov) for leg in legs)
        assert p == pytest.approx(prod, abs=1e-5)


def test_correlation_sign_follows_variance_asymmetry():
    """Cov(M, T) = var_home - var_away: noisier favorite => fav cover & over above independence."""
    noisy_home = MatchupCovariance(11.0, 8.0, 0.0)
    legs = [home_cover(6.5), over(44.5)]
    prod = math.prod(leg_probability(leg, MEAN, noisy_home) for leg in legs)
    assert joint_probability(legs, MEAN, noisy_home) > prod + 0.005
    legs = [home_cover(6.5), under(44.5)]
    prod = math.prod(leg_probability(leg, MEAN, noisy_home) for leg in legs)
    assert joint_probability(legs, MEAN, noisy_home) < prod - 0.005


def test_positive_rho_raises_over_and_team_total_joint():
    lo = joint_probability([over(44.5), home_team_over(25.5)], MEAN, MatchupCovariance(9.0, 9.0, 0.0))
    hi = joint_probability([over(44.5), home_team_over(25.5)], MEAN, MatchupCovariance(9.0, 9.0, 0.4))
    assert hi > lo


def test_settle_leg():
    assert settle_leg(home_cover(3.0), 24, 21) == PUSH
    assert settle_leg(home_cover(2.5), 24, 21) == WIN
    assert settle_leg(away_cover(2.5), 24, 21) == LOSE
    assert settle_leg(home_ml(), 20, 20) == PUSH
    assert settle_leg(under(45.5), 24, 21) == WIN


_PARAMS = {"league": {"variance_model": "mean_linear", "var_intercept": 58.0, "var_slope": 1.07,
                      "rho": 0.05, "sigma_min": 5.0, "sigma_max": 16.0}, "teams": {}}


@pytest.mark.parametrize("true, spread, total", [
    ((27.3, 20.1), 6.5, 46.0),      # integer total: push-conditioned target
    ((17.0, 24.5), -7.0, 41.5),     # away favorite, integer spread
    ((31.0, 10.0), 17.5, 44.5),     # big favorite, lines off the true means
])
def test_calibration_recovers_means(true, spread, total):
    def cov_fn(h, a):
        return matchup_covariance(_PARAMS, "H", "A", h, a)

    cov = cov_fn(*true)
    p_cover = leg_probability(home_cover(spread), true, cov)
    p_over = leg_probability(over(total), true, cov)
    res = calibrate_means(spread, p_cover, total, p_over, cov_fn)
    assert res.converged
    assert res.mu_home == pytest.approx(true[0], abs=1e-4)
    assert res.mu_away == pytest.approx(true[1], abs=1e-4)
    # And the calibrated model reprices the market exactly.
    assert leg_probability(home_cover(spread), (res.mu_home, res.mu_away), res.cov) == pytest.approx(p_cover, abs=1e-7)


def test_calibration_at_fair_prices_centres_on_lines():
    def cov_fn(h, a):
        return matchup_covariance(_PARAMS, "H", "A", h, a)

    res = calibrate_means(3.5, 0.5, 44.5, 0.5, cov_fn)
    assert res.mu_home - res.mu_away == pytest.approx(3.5, abs=1e-6)
    assert res.mu_home + res.mu_away == pytest.approx(44.5, abs=1e-6)

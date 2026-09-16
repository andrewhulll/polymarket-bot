"""Covariance estimator: parameter recovery, walk-forward cutoff, shrinkage, determinism."""
import dataclasses

import pytest

pytest.importorskip("numpy")

from combo_mm.nfl.estimate import EstimatorConfig, InsufficientHistory, ResidualTable, estimate_params
from combo_mm.nfl.params_io import canonical_json_bytes, validate_params
from nfl_synthetic import TRUE_A, TRUE_B, TRUE_RHO, make_games


@pytest.fixture(scope="module")
def big_games():
    # ~5400 continuous-score games with a wide spread of implied points. The
    # slope of E[e^2 | mu] is statistically hard: e^2 has SD ~ sqrt(2)*sigma^2,
    # so its SE is ~0.2 here (and ~0.6 on four real NFL seasons).
    return make_games(seasons=range(2000, 2015), weeks=30, seed=11, integer_scores=False,
                      spread_sd=8.0, total_sd=8.0)


def test_recovers_known_variance_function_and_rho(big_games):
    cfg = EstimatorConfig(window_seasons=15, min_seasons=3, half_life_seasons=1000.0)
    p = estimate_params(big_games, 2015, 1, cfg)
    validate_params(p)
    lg = p["league"]
    assert lg["n_games"] == 15 * 30 * 6
    assert lg["var_slope"] == pytest.approx(TRUE_B, abs=0.6)
    for mu in (15.0, 22.5, 30.0):
        assert lg["var_intercept"] + lg["var_slope"] * mu == pytest.approx(TRUE_A + TRUE_B * mu, rel=0.05)
    assert lg["rho"] == pytest.approx(TRUE_RHO, abs=0.04)
    assert abs(lg["residual_bias"]) < 0.3


def test_league_constant_matches_average_variance(big_games):
    cfg = EstimatorConfig(variance_model="league_constant", window_seasons=15, half_life_seasons=1000.0)
    lg = estimate_params(big_games, 2015, 1, cfg)["league"]
    assert lg["var_slope"] == 0.0
    assert lg["var_intercept"] == pytest.approx(TRUE_A + TRUE_B * lg["mean_points"], rel=0.05)


def test_uses_only_games_strictly_before_cutoff():
    games = make_games(seasons=range(2010, 2015), weeks=10, seed=3)
    base = estimate_params(games, 2014, 5, EstimatorConfig())
    # Scramble every score at or after the cutoff: params must not move.
    future = [
        dataclasses.replace(g, home_score=g.away_score + 30, away_score=0)
        if (g.season, g.week) >= (2014, 5) else g
        for g in games
    ]
    assert canonical_json_bytes(estimate_params(future, 2014, 5, EstimatorConfig())) == canonical_json_bytes(base)
    # ...but a change just before the cutoff does move them.
    past = [
        dataclasses.replace(g, home_score=g.home_score + 40) if (g.season, g.week) == (2014, 4) else g
        for g in games
    ]
    assert canonical_json_bytes(estimate_params(past, 2014, 5, EstimatorConfig())) != canonical_json_bytes(base)


def test_insufficient_history():
    games = make_games(seasons=range(2010, 2012), weeks=6)
    with pytest.raises(InsufficientHistory):
        estimate_params(games, 2012, 1, EstimatorConfig(min_seasons=3))


def test_team_factor_moves_toward_current_season_as_games_accumulate():
    games = make_games(seasons=range(2010, 2015), weeks=16, seed=5)
    # T00's 2014 games get huge scoring swings on offense.
    noisy = [
        dataclasses.replace(g, home_score=g.home_score + (25 if g.week % 2 else -g.home_score))
        if g.season == 2014 and g.home == "T00" else g
        for g in games
    ]
    cfg = EstimatorConfig(variance_model="mean_linear_team")
    early = estimate_params(noisy, 2014, 3, cfg)["teams"]["T00"]
    late = estimate_params(noisy, 2014, 16, cfg)["teams"]["T00"]
    assert late["n_current"] > early["n_current"]
    assert late["off_var_factor"] > early["off_var_factor"] >= 1.0
    assert late["off_var_factor"] <= cfg.factor_max


def test_week_one_uses_prior_seasons_regressed_to_league():
    games = make_games(seasons=range(2010, 2015), weeks=12, seed=9)
    no_regress = estimate_params(games, 2015, 1, EstimatorConfig(offseason_regression=0.0))
    full_regress = estimate_params(games, 2015, 1, EstimatorConfig(offseason_regression=1.0))
    league = full_regress["league"]["mean_points"]
    for team, t in full_regress["teams"].items():
        assert t["n_current"] == 0
        assert t["off_var_factor"] == pytest.approx(1.0)
        assert t["off_mean"] == pytest.approx(league)
    spread = [abs(t["off_mean"] - league) for t in no_regress["teams"].values()]
    assert max(spread) > 0.1


def test_schedule_entries_and_determinism():
    games = make_games(seasons=range(2010, 2015), weeks=8, unplayed_from=(2014, 8), seed=2)
    schedule = [g for g in games if (g.season, g.week) == (2014, 8)]
    table = ResidualTable.build(games)
    p1 = estimate_params(table, 2014, 8, EstimatorConfig(), data_vintage={"pull_date": "d"}, schedule=schedule)
    p2 = estimate_params(games, 2014, 8, EstimatorConfig(), data_vintage={"pull_date": "d"}, schedule=schedule)
    assert canonical_json_bytes(p1) == canonical_json_bytes(p2)
    assert len(p1["games"]) == len(schedule)
    g = p1["games"][0]
    assert g["var_margin"] > 0 and -1 < g["corr_margin_total"] < 1
    validate_params(p1)


def test_config_validation():
    with pytest.raises(ValueError):
        EstimatorConfig(variance_model="nope")
    with pytest.raises(ValueError):
        EstimatorConfig(min_seasons=5, window_seasons=3)
    with pytest.raises(ValueError):
        EstimatorConfig(offseason_regression=1.5)

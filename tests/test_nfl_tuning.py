"""Train/test split and train-only hyperparameter tuning."""
import dataclasses

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")
pd = pytest.importorskip("pandas")

from combo_mm.nfl import synthetic_backtest as bt  # noqa: E402
from combo_mm.nfl.estimate import EstimatorConfig  # noqa: E402
from combo_mm.nfl.tuning import load_selection, tune, write_selection  # noqa: E402
from nfl_synthetic import make_games  # noqa: E402

GRID = {"window_seasons": (3, None), "half_life_seasons": (2.0,), "var_shrink_multiplier": (3.0,)}


@pytest.fixture(scope="module")
def games():
    return make_games(seasons=range(2010, 2017), weeks=4, seed=31)


@pytest.fixture(scope="module")
def config():
    return bt.BacktestConfig(first_season=2013, train_last_season=2015, last_season=2016)


def test_rows_are_tagged_train_or_test(games, config):
    out = bt.run_backtest(games, dataclasses.replace(config, corr_scales=(1.0,)), workers=1)
    split = out.combos.groupby("season")["split"].unique().map(list).to_dict()
    assert split == {2013: ["train"], 2014: ["train"], 2015: ["train"], 2016: ["test"]}
    assert set(out.games["split"]) == {"train", "test"}
    assert out.meta["split"] == {"train": [2013, 2015], "test": [2016, 2016]}


def test_test_period_walk_forward_still_uses_all_prior_games(games, config):
    """Test-season params are estimated from every earlier game, including train-period ones."""
    cfg = dataclasses.replace(config, corr_scales=(1.0,), variance_models=("mean_linear",))
    base = bt.run_backtest(games, cfg, workers=1)
    changed = [dataclasses.replace(g, home_score=g.home_score + 17) if g.season == 2015 else g for g in games]
    again = bt.run_backtest(changed, cfg, workers=1)
    test_before = base.combos[base.combos["split"] == "test"]["p_mean_linear"].reset_index(drop=True)
    test_after = again.combos[again.combos["split"] == "test"]["p_mean_linear"].reset_index(drop=True)
    assert not test_before.equals(test_after)


def test_tuning_never_sees_test_seasons(games, config):
    result = tune(games, config, grid=GRID, workers=1)
    scrambled = [
        dataclasses.replace(g, home_score=g.away_score + 35, away_score=0) if g.season > config.train_last_season else g
        for g in games
    ]
    again = tune(scrambled, config, grid=GRID, workers=1)
    pd.testing.assert_frame_equal(result.grid, again.grid)
    assert result.selected == again.selected
    assert result.train_seasons == (2013, 2015)


def test_tuning_grid_and_selection(tmp_path, games, config):
    result = tune(games, config, grid=GRID, workers=1)
    # 2 windows x 3 variance models (single shrink multiplier)
    assert len(result.grid) == 6
    assert result.grid["brier"].is_monotonic_increasing
    best = result.grid.iloc[0]
    assert result.selected["variance_model"] == best["variance_model"]
    assert result.selected["train_brier"] == pytest.approx(best["brier"])

    path = write_selection(result, tmp_path / "estimator.json", data_vintage={"pull_date": "x"})
    estimator, payload = load_selection(path)
    assert isinstance(estimator, EstimatorConfig)
    assert estimator.variance_model == best["variance_model"]
    assert payload["train_seasons"] == [2013, 2015]
    expected_window = None if pd.isna(best["window_seasons"]) else int(best["window_seasons"])
    assert estimator.window_seasons == expected_window
    assert load_selection(tmp_path / "missing.json") is None


def test_shrink_multiplier_only_expands_team_model(games, config):
    grid = {"window_seasons": (3,), "half_life_seasons": (2.0,), "var_shrink_multiplier": (1.0, 3.0)}
    result = tune(games, config, grid=grid, workers=1)
    counts = result.grid["variance_model"].value_counts().to_dict()
    assert counts == {"mean_linear_team": 2, "league_constant": 1, "mean_linear": 1}


def test_all_history_window(games):
    from combo_mm.nfl.estimate import estimate_params
    p_all = estimate_params(games, 2016, 1, EstimatorConfig(window_seasons=None))
    p_3 = estimate_params(games, 2016, 1, EstimatorConfig(window_seasons=3))
    assert p_all["league"]["n_games"] > p_3["league"]["n_games"]
    assert p_all["league"]["seasons"][0] == 2010

"""Walk-forward same-game combo backtest: combo universe, no leakage, summaries."""
import dataclasses

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")
pd = pytest.importorskip("pandas")

from combo_mm.nfl import synthetic_backtest as bt  # noqa: E402
from nfl_synthetic import make_games  # noqa: E402


@pytest.fixture(scope="module")
def games():
    return make_games(seasons=range(2010, 2015), weeks=6, seed=21)


@pytest.fixture(scope="module")
def config():
    return bt.BacktestConfig(first_season=2014, last_season=2014)


@pytest.fixture(scope="module")
def output(games, config):
    return bt.run_backtest(games, config, workers=1)


def test_combo_universe_is_same_game_ml_spread_total():
    assert len(bt.COMBOS) == 17
    for name, (keys, family, nested) in bt.COMBOS.items():
        assert 2 <= len(keys) <= 3
        markets = [next(m for m, sides in bt.MARKETS if k in sides) for k in keys]
        assert len(set(markets)) == len(keys), name          # one side per market
        assert not ({"dog_ml", "fav_cover"} <= set(keys))     # impossible combo excluded
        assert nested == ({"fav_ml", "fav_cover"} <= set(keys) or {"dog_ml", "dog_cover"} <= set(keys))
    assert "fav_ml+dog_cover" in bt.COMBOS and not bt.COMBOS["fav_ml+dog_cover"][2]


def test_output_shape_and_push_handling(output, config):
    combos, games = output.combos, output.games
    n_games = len(games)
    assert n_games == 36 and len(combos) == 17 * n_games
    scored = combos[~combos["pushed"]]
    for col in bt.prob_columns(config):
        assert scored[col].notna().all()
        assert scored[col].between(0, 1).all()
    assert combos.loc[combos["pushed"], "realized"].isna().all()
    assert set(scored["realized"].unique()) <= {0.0, 1.0}
    assert output.meta["n_pushed"] == int(combos["pushed"].sum())


def test_family_partitions_sum_to_one_per_game(output):
    """Within a family the combos partition outcomes: model probs sum to 1 per game."""
    df = output.combos
    for family in ("ML x total", "spread x total", "ML x spread"):
        fam = df[df["family"] == family]
        complete = fam.groupby("game_id").filter(lambda g: not g["pushed"].any())
        sums = complete.groupby("game_id")["p_mean_linear"].sum()
        assert len(sums) > 0
        assert sums.round(4).eq(1.0).all(), family
        assert complete.groupby("game_id")["realized"].sum().eq(1).all()


def test_zero_correlation_reproduces_naive_on_spread_x_total(output):
    """Market-calibrated marginals + corr_scale 0 => independent margin/total => naive product."""
    st = output.combos[(output.combos["family"] == "spread x total") & ~output.combos["pushed"]]
    assert (st["p_scale_0"] - st["naive"]).abs().max() < 2e-4
    # ...while the fitted model (mean-dependent variance) departs from it.
    assert (st["p_mean_linear"] - st["naive"]).abs().max() > 1e-3


def test_walk_forward_has_no_future_leakage(games, config, output):
    """Changing scores from week 4 on cannot move any price for weeks 1-4."""
    altered = [
        dataclasses.replace(g, home_score=g.home_score + 21) if (g.season, g.week) >= (2014, 4) else g
        for g in games
    ]
    again = bt.run_backtest(altered, config, workers=1)
    cols = ["game_id", "combo"] + bt.prob_columns(config)
    before = output.combos[(output.combos["week"] <= 4)][cols].set_index(["game_id", "combo"])
    after = again.combos[(again.combos["week"] <= 4)][cols].set_index(["game_id", "combo"])
    # Week-4 rows may differ only through their own realized/push status, never through params.
    common = before.dropna().index.intersection(after.dropna().index)
    pd.testing.assert_frame_equal(before.loc[common], after.loc[common])
    later = output.combos["week"] >= 5
    assert not output.combos[later][bt.prob_columns(config)].equals(again.combos[later][bt.prob_columns(config)])


def test_summaries_run_and_are_consistent(output, config):
    combos, games = output.combos, output.games
    scores = bt.score_table(combos, bt.prob_columns(config))
    assert scores.iloc[0]["model"] == "naive"
    assert set(scores["model"]) >= {"mean_linear", "scale_0"}
    assert (scores["brier_diff_se"].iloc[1:] > 0).all()

    fam = bt.group_table(combos, "family", "p_mean_linear")
    assert fam["n"].sum() == int((~combos["pushed"]).sum())

    cal = bt.calibration_table(combos, "p_mean_linear")
    assert cal["n"].sum() == fam["n"].sum()
    assert (cal["predicted"] >= cal["bin_lo"] - 1e-9).all() and (cal["predicted"] <= cal["bin_hi"] + 1e-9).all()

    sens = bt.sensitivity_table(combos, config.corr_scales, config.primary_model, 0.01)
    assert list(sens["corr_scale"]) == sorted(config.corr_scales)

    trades = bt.edge_pnl(combos, "p_mean_linear", 0.01)
    assert trades["cum_pnl"].iloc[-1] == pytest.approx(trades["pnl"].sum())

    for frame in (bt.spread_bucket_structure(games), bt.sigma_vs_mu(games), bt.season_structure(games),
                  bt.outcome_lift(games), bt.moneyline_consistency(games, config.variance_models)):
        assert len(frame) > 0
    lift = bt.outcome_lift(games).set_index(["a", "b"])
    # Covering implies winning: strong positive empirical and model lift.
    assert lift.loc[("fav_win", "fav_cover"), "emp_lift"] > 0.1
    assert lift.loc[("fav_win", "fav_cover"), "model_lift"] > 0.1


def test_pnl_swings():
    trades = pd.DataFrame({"side": [1, 1, -1, 1, 0], "pnl": [1.0, -3.0, 0.5, 4.0, 0.0]})
    trades["cum_pnl"] = trades["pnl"].cumsum()
    stats = bt.pnl_stats(trades)
    assert stats["n_trades"] == 4
    assert stats["max_downswing"] == pytest.approx(3.0)       # 1 -> -2
    assert stats["max_upswing"] == pytest.approx(4.5)         # -2 -> 2.5
    assert stats["total_pnl"] == pytest.approx(2.5)


def test_outputs_roundtrip(tmp_path, output):
    bt.write_outputs(output, tmp_path)
    loaded = bt.load_outputs(tmp_path)
    assert loaded is not None
    assert len(loaded.combos) == len(output.combos)
    assert loaded.meta["n_games"] == output.meta["n_games"]
    assert bt.load_outputs(tmp_path / "missing") is None


def test_parallel_matches_serial(games):
    cfg = bt.BacktestConfig(first_season=2013, last_season=2014, variance_models=("mean_linear",),
                            corr_scales=(0.0, 1.0))
    serial = bt.run_backtest(games, cfg, workers=1)
    parallel = bt.run_backtest(games, cfg, workers=2)
    pd.testing.assert_frame_equal(serial.combos, parallel.combos)
    pd.testing.assert_frame_equal(serial.games, parallel.games)

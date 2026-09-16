"""Weekly refresh: target week, gates, promotion, rejection, offseason freeze."""
import json

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")

from combo_mm.nfl import refresh as refresh_mod  # noqa: E402
from combo_mm.nfl.params_io import load_params  # noqa: E402
from combo_mm.nfl.refresh import OFFSEASON, PROMOTED, REJECTED, next_target_week, refresh  # noqa: E402
from nfl_synthetic import make_games  # noqa: E402

VINTAGE = {"pull_date": "2026-09-16", "sha256": "deadbeef"}


@pytest.fixture(scope="module")
def season_in_progress():
    return make_games(seasons=range(2010, 2015), weeks=8, unplayed_from=(2014, 6), seed=4)


def test_next_target_week(season_in_progress):
    assert next_target_week(season_in_progress) == (2014, 6)
    assert next_target_week(make_games(seasons=range(2010, 2012), weeks=2)) is None


def test_promotes_when_gates_pass_and_is_deterministic(tmp_path, season_in_progress):
    res = refresh(season_in_progress, tmp_path, VINTAGE, gate_lookback_games=60)
    assert res.status == PROMOTED, res.to_dict()
    assert (res.season, res.week) == (2014, 6)
    assert [g.name for g in res.gates] == ["range_sanity", "no_regression", "determinism"]
    params = load_params(tmp_path / "nfl_2014_w06.json")
    assert params["data_vintage"] == VINTAGE
    assert len(params["games"]) == 6
    report = json.loads((tmp_path / "reports" / "nfl_2014_w06.gates.json").read_text())
    assert report["status"] == PROMOTED and report["path"] == "nfl_2014_w06.json"
    first = (tmp_path / "nfl_2014_w06.json").read_bytes()
    # Re-running on the same data reproduces the file byte for byte.
    again = refresh(season_in_progress, tmp_path, VINTAGE, season=2014, week=6, gate_lookback_games=60)
    assert again.status == PROMOTED
    assert (tmp_path / "nfl_2014_w06.json").read_bytes() == first


def test_second_week_compares_against_previous_file(tmp_path, season_in_progress):
    refresh(season_in_progress, tmp_path, VINTAGE, season=2014, week=5, gate_lookback_games=60)
    res = refresh(season_in_progress, tmp_path, VINTAGE, season=2014, week=6, gate_lookback_games=60)
    gate = next(g for g in res.gates if g.name == "no_regression")
    assert gate.detail["previous_file"] == "nfl_2014_w05.json"
    assert "previous_brier" in gate.detail


def test_failing_gate_rejects_and_keeps_previous_file(tmp_path, season_in_progress, monkeypatch):
    refresh(season_in_progress, tmp_path, VINTAGE, season=2014, week=5, gate_lookback_games=60)
    previous = (tmp_path / "nfl_2014_w05.json").read_bytes()

    import combo_mm.nfl.synthetic_backtest as bt
    real = bt.evaluate_params_on_games

    def worse_candidate(params, games, **kw):
        out = real(params, games, **kw)
        if params["week"] == 6:
            out["brier_model"] += 0.05
        return out

    monkeypatch.setattr(bt, "evaluate_params_on_games", worse_candidate)
    res = refresh(season_in_progress, tmp_path, VINTAGE, season=2014, week=6, gate_lookback_games=60)
    assert res.status == REJECTED
    assert not (tmp_path / "nfl_2014_w06.json").exists()
    assert (tmp_path / ".staging" / "nfl_2014_w06.json").exists()
    assert (tmp_path / "nfl_2014_w05.json").read_bytes() == previous
    report = json.loads((tmp_path / "reports" / "nfl_2014_w06.gates.json").read_text())
    assert report["status"] == REJECTED


def test_range_gate_failure_rejects(tmp_path, season_in_progress, monkeypatch):
    real_build = refresh_mod.build_params

    def broken(*args, **kwargs):
        p = real_build(*args, **kwargs)
        p["league"]["rho"] = 1.5
        return p

    monkeypatch.setattr(refresh_mod, "build_params", broken)
    res = refresh(season_in_progress, tmp_path, VINTAGE, gate_lookback_games=30)
    assert res.status == REJECTED
    assert not next(g for g in res.gates if g.name == "range_sanity").passed


def test_offseason_freezes_last_file(tmp_path):
    finished = make_games(seasons=range(2010, 2015), weeks=4, seed=8)
    res = refresh(finished, tmp_path, VINTAGE)
    assert res.status == OFFSEASON and res.path is None
    assert not any(tmp_path.iterdir())

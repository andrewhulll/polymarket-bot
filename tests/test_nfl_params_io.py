"""Params files: canonical serialization, schema/range validation, matchup covariance."""
import copy
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from combo_mm.nfl.params_io import (
    ParamsError,
    canonical_json_bytes,
    latest_params,
    list_params,
    load_params,
    matchup_covariance,
    params_filename,
    parse_params_filename,
    validate_params,
    write_params,
)


def _params(model="mean_linear", **league_over):
    league = {
        "variance_model": model, "var_intercept": 58.0, "var_slope": 1.07, "rho": 0.05,
        "sigma_min": 5.0, "sigma_max": 16.0, "mean_points": 22.7, "residual_bias": 0.5,
        "n_games": 1100, "seasons": [2022, 2023, 2024, 2025],
    }
    league.update(league_over)
    return {
        "schema_version": 1, "sport": "nfl", "model_version": "nfl-phaseA-1",
        "season": 2026, "week": 2, "as_of": {"season": 2026, "week": 2},
        "data_vintage": {"pull_date": "2026-09-16", "sha256": "abc"}, "estimator": {},
        "league": league,
        "teams": {"KC": {"off_var_factor": 1.2, "def_var_factor": 0.9, "off_mean": 26.0,
                         "def_mean": 19.0, "n_current": 1, "n_prior": 68}},
        "games": [],
    }


def test_filename_roundtrip():
    assert params_filename(2026, 7) == "nfl_2026_w07.json"
    assert parse_params_filename("nfl_2026_w07.json") == (2026, 7)
    assert parse_params_filename("nfl_2026_w07.gates.json") is None


def test_canonical_bytes_are_stable_and_rounded():
    p = _params()
    p["league"]["rho"] = 0.049999999999
    p["league"]["residual_bias"] = -0.0000000001
    raw = canonical_json_bytes(p)
    assert raw == canonical_json_bytes(copy.deepcopy(p))
    decoded = json.loads(raw)
    assert decoded["league"]["rho"] == 0.05
    assert b"-0.0" not in raw
    assert raw.endswith(b"\n")


def test_write_load_roundtrip_and_latest(tmp_path):
    for week in (1, 3, 2):
        p = _params()
        p["week"] = week
        write_params(p, tmp_path / params_filename(2026, week))
    (tmp_path / "notes.json").write_text("{}")
    assert [k for k, _ in list_params(tmp_path)] == [(2026, 1), (2026, 2), (2026, 3)]
    assert latest_params(tmp_path).name == "nfl_2026_w03.json"
    assert latest_params(tmp_path, before=(2026, 3)).name == "nfl_2026_w02.json"
    assert latest_params(tmp_path, before=(2026, 1)) is None
    assert load_params(tmp_path / "nfl_2026_w02.json")["week"] == 2


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p["league"].update(rho=1.0), "rho"),
    (lambda p: p["league"].update(var_slope=float("nan")), "var_slope"),
    (lambda p: p["league"].update(variance_model="garch"), "variance_model"),
    (lambda p: p["league"].pop("rho"), "missing"),
    (lambda p: p["teams"]["KC"].update(off_var_factor=9.0), "off_var_factor"),
    (lambda p: p.update(schema_version=2), "schema_version"),
    (lambda p: p["games"].append({"game_id": "g", "home": "KC", "away": "BUF", "mu_home": 25.0,
                                  "mu_away": 20.0, "sigma_home": 30.0, "sigma_away": 9.0, "rho": 0.05,
                                  "var_margin": 100.0, "var_total": 120.0, "cov_margin_total": 10.0,
                                  "corr_margin_total": 0.1}), "sigma_home"),
])
def test_validation_rejects(mutate, message):
    p = _params()
    mutate(p)
    with pytest.raises(ParamsError, match=message):
        validate_params(p)


def test_nonfinite_values_cannot_be_written(tmp_path):
    p = _params()
    p["data_vintage"]["x"] = float("inf")
    with pytest.raises(ParamsError):
        canonical_json_bytes(p)


def test_margin_total_covariance_identity():
    cov = matchup_covariance(_params(), "KC", "BUF", 27.0, 20.0)
    assert cov.cov_margin_total == pytest.approx(cov.var_home - cov.var_away)
    assert cov.var_margin == pytest.approx(cov.var_home + cov.var_away - 2 * cov.cov_home_away)
    assert cov.var_total == pytest.approx(cov.var_home + cov.var_away + 2 * cov.cov_home_away)
    # mean_linear: the side expected to score more is noisier -> positive corr for the favorite.
    assert cov.sigma_home > cov.sigma_away and cov.corr_margin_total > 0


def test_variance_models():
    const = matchup_covariance(_params("league_constant", var_slope=0.0), "KC", "BUF", 27.0, 20.0)
    assert const.sigma_home == const.sigma_away == pytest.approx(math.sqrt(58.0))
    team = matchup_covariance(_params("mean_linear_team"), "KC", "BUF", 27.0, 20.0)
    plain = matchup_covariance(_params(), "KC", "BUF", 27.0, 20.0)
    assert team.sigma_home == pytest.approx(plain.sigma_home * math.sqrt(1.2))   # KC offense factor
    assert team.sigma_away == pytest.approx(plain.sigma_away * math.sqrt(0.9))   # KC defense factor
    clamped = matchup_covariance(_params(var_intercept=900.0), "KC", "BUF", 27.0, 20.0)
    assert clamped.sigma_home == 16.0


def test_corr_scale_zero_removes_margin_total_dependence():
    cov = matchup_covariance(_params(), "KC", "BUF", 30.0, 14.0, corr_scale=0.0)
    assert cov.rho == 0.0
    assert cov.sigma_home == pytest.approx(cov.sigma_away)
    assert cov.corr_margin_total == pytest.approx(0.0, abs=1e-12)
    base = matchup_covariance(_params(), "KC", "BUF", 30.0, 14.0)
    double = matchup_covariance(_params(), "KC", "BUF", 30.0, 14.0, corr_scale=2.0)
    # average variance preserved, asymmetry and rho scaled
    assert double.var_home + double.var_away == pytest.approx(base.var_home + base.var_away)
    assert double.rho == pytest.approx(2 * base.rho)
    assert double.corr_margin_total > base.corr_margin_total


def test_params_loader_is_stdlib_only():
    """The pricer-facing loader must import without numpy/scipy/pandas."""
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys\n"
        "for m in ('numpy', 'scipy', 'pandas'):\n"
        "    sys.modules[m] = None\n"
        "import combo_mm.nfl.params_io as p\n"
        "print(p.matchup_covariance.__name__)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr

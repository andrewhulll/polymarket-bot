"""Estimator hyperparameter tuning on the **train period only**.

The walk-forward backtest re-estimates params every week from past games, but
the estimator's own settings -- variance model, trailing window, recency
half-life, variance-factor shrinkage -- are choices. Choosing them by looking
at the full backtest would leak the evaluation period into the model. So:

1. Every game after ``train_last_season`` is **removed from the input** before
   anything runs (not merely unscored) -- test-period scores cannot influence
   tuning by construction (tested by scrambling them).
2. Each grid point runs the same walk-forward backtest over the train seasons
   and is scored by Brier over all same-game combos (the prices a quoter
   would actually post), with game-clustered standard errors.
3. The lowest-Brier configuration is frozen to ``params/estimator.json``. The
   test period is then scored once with it (``scripts/nfl_backtest.py``) and
   the weekly refresh uses it (``scripts/refresh_params.py``).

``var_shrink_multiplier`` only affects ``mean_linear_team``, so the other
variance models are run once per (window, half-life).
"""
from __future__ import annotations

import dataclasses
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from combo_mm.nfl.estimate import EstimatorConfig
from combo_mm.nfl.ingest import Game
from combo_mm.nfl.params_io import VARIANCE_MODELS
from combo_mm.nfl import synthetic_backtest as bt

__all__ = ["DEFAULT_GRID", "TuningResult", "tune", "write_selection", "load_selection", "SELECTION_PATH"]

SELECTION_PATH = Path("params/estimator.json")

DEFAULT_GRID: Dict[str, Tuple[Any, ...]] = {
    "window_seasons": (4, 8, None),
    "half_life_seasons": (1.0, 2.0, 4.0),
    "var_shrink_multiplier": (1.0, 3.0),
}

METRIC = "brier_all_combos"


@dataclass
class TuningResult:
    grid: Any                         # pandas DataFrame, one row per candidate, best first
    selected: Dict[str, Any]          # {"variance_model", "estimator", train scores...}
    train_seasons: Tuple[int, int]
    search_space: Dict[str, Tuple[Any, ...]]
    metric: str = METRIC


def _candidates(grid: Dict[str, Sequence[Any]]):
    keys = list(grid)
    mults = list(grid.get("var_shrink_multiplier", (EstimatorConfig().var_shrink_multiplier,)))
    for values in itertools.product(*(grid[k] for k in keys)):
        point = dict(zip(keys, values))
        first_mult = point.get("var_shrink_multiplier", mults[0]) == mults[0]
        models = VARIANCE_MODELS if first_mult else ("mean_linear_team",)
        yield point, models


def tune(games: Sequence[Game], config: Optional[bt.BacktestConfig] = None,
         grid: Optional[Dict[str, Sequence[Any]]] = None, workers: Optional[int] = None,
         progress: Optional[Callable[[int, int, Dict[str, Any]], None]] = None) -> TuningResult:
    pd = bt._pd()
    config = config or bt.BacktestConfig()
    grid = grid or DEFAULT_GRID
    train_games = [g for g in games if g.season <= config.train_last_season]
    base = dataclasses.replace(config, last_season=config.train_last_season, corr_scales=(1.0,),
                               structure=False)
    points = list(_candidates(grid))
    rows = []
    for i, (point, models) in enumerate(points):
        estimator = dataclasses.replace(config.estimator, **point)
        run_cfg = dataclasses.replace(base, estimator=estimator, variance_models=tuple(models),
                                      primary_model=models[0])
        out = bt.run_backtest(train_games, run_cfg, workers=workers)
        cols = [bt.model_col(m) for m in models]
        all_scores = bt.score_table(out.combos, cols).set_index("model")
        non_nested = bt.score_table(out.combos[~out.combos["nested"].astype(bool)], cols).set_index("model")
        st = out.combos[out.combos["family"] == "spread x total"]
        st_scores = bt.score_table(st, cols).set_index("model")
        for m in models:
            rows.append({
                **point, "variance_model": m, "n_combos": int(all_scores.loc[m, "n"]),
                "brier": all_scores.loc[m, "brier"], "brier_naive": all_scores.loc["naive", "brier"],
                "brier_skill": all_scores.loc[m, "brier_skill_vs_naive"], "t_stat": all_scores.loc[m, "t_stat"],
                "brier_skill_non_nested": non_nested.loc[m, "brier_skill_vs_naive"],
                "brier_skill_spread_total": st_scores.loc[m, "brier_skill_vs_naive"],
                "log_loss": all_scores.loc[m, "log_loss"], "grid_index": len(rows),
            })
        if progress:
            progress(i + 1, len(points), point)
    frame = pd.DataFrame(rows).sort_values(["brier", "grid_index"], kind="stable").reset_index(drop=True)
    best = frame.iloc[0]
    chosen: Dict[str, Any] = {}
    for k in grid:
        value = best[k]
        if value is None or (isinstance(value, float) and pd.isna(value)):
            chosen[k] = None
        elif k == "window_seasons":
            chosen[k] = int(value)
        else:
            chosen[k] = float(value)
    estimator = dataclasses.replace(config.estimator, variance_model=best["variance_model"], **chosen)
    selected = {
        "variance_model": best["variance_model"],
        "estimator": estimator.to_dict(),
        "train_brier": float(best["brier"]),
        "train_brier_naive": float(best["brier_naive"]),
        "train_brier_skill": float(best["brier_skill"]),
        "runner_up_brier": float(frame.iloc[1]["brier"]) if len(frame) > 1 else None,
    }
    return TuningResult(grid=frame, selected=selected,
                        train_seasons=(config.first_season, config.train_last_season),
                        search_space={k: tuple(v) for k, v in grid.items()})


def _json_safe(value: Any) -> Any:
    return value.item() if hasattr(value, "item") else value


def write_selection(result: TuningResult, path: Path | str = SELECTION_PATH,
                    data_vintage: Optional[Dict[str, Any]] = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    est = {k: _json_safe(v) for k, v in result.selected["estimator"].items()}
    payload = {
        "selected_by": "scripts/nfl_tune.py (train period only)",
        "train_seasons": list(result.train_seasons),
        "metric": result.metric,
        "grid": {k: list(v) for k, v in result.search_space.items()},
        "n_candidates": int(len(result.grid)),
        "data_vintage": dict(data_vintage or {}),
        **{k: _json_safe(v) for k, v in result.selected.items() if k != "estimator"},
        "estimator": est,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def load_selection(path: Path | str = SELECTION_PATH) -> Optional[Tuple[EstimatorConfig, Dict[str, Any]]]:
    """Frozen estimator config from a tuning run, or None if the file is absent."""
    path = Path(path)
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    return EstimatorConfig(**payload["estimator"]), payload

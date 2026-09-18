"""Estimator hyperparameter tuning on the **train period only**.

The walk-forward backtest re-estimates params every week from past games, but
the estimator's own settings -- variance model, trailing window, recency
half-life, variance-factor shrinkage -- are choices. Choosing them by looking
at the full backtest would leak the evaluation period into the model. So:

1. Every game after ``train_last_season`` is **removed from the input** before
   anything runs (not merely unscored) -- test-period scores cannot influence
   tuning by construction (tested by scrambling them).
2. Each grid point runs the same walk-forward backtest over the train seasons
   and is scored by Brier over the **deployed combo universe**
   (:data:`combo_mm.nfl.synthetic_backtest.DEPLOYED_FAMILIES` -- moneyline x
   total and spread x total, the only same-game blocks
   :mod:`combo_mm.nfl.live_pricer` ever prices), on the **market_lift price**
   (:func:`combo_mm.nfl.synthetic_backtest.lifted`) rather than the model's
   raw joint, with game-clustered standard errors. Two fixes, both needed:
   - The full ``COMBOS`` universe (still scored and kept in the grid for
     visibility) is dominated by "ML x spread" and "ML x spread x total" --
     combos that share the margin dimension across legs and score
     double-digit Brier skill from that alone, and that no live RFQ has ever
     sent (verified against ``data/live/rfq_capture.db``). Selecting on it
     let a config with ~zero margin/total dependence (``league_constant``)
     win every grid search while doing nothing for the combos actually
     quoted -- see ``docs/correlation-model.md`` for the pre-fix numbers.
   - The model's raw joint is not what a live quote is: ``market_lift``
     keeps each leg's own market price and borrows only the lift ratio from
     the model. The two coincide for spread x total (its legs are
     calibrated to the market exactly) but not for ML x total, where the
     model's own moneyline marginal is a known ~2-point miss
     (``docs/correlation-model.md`` #4.4.2) that market_lift is built to
     avoid importing -- scoring the raw joint there penalizes a bias no
     live quote actually carries.
3. The lowest-deployed-Brier configuration is frozen to
   ``params/estimator.json``. The test period is then scored once with it
   (``scripts/nfl_backtest.py``) and the weekly refresh uses it
   (``scripts/refresh_params.py``).

``var_shrink_multiplier`` only affects ``mean_linear_team``, so the other
variance models are run once per (window, half-life).
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from combo_mm.nfl.estimate import EstimatorConfig
from combo_mm.nfl.ingest import Game
from combo_mm.nfl.params_io import VARIANCE_MODELS
from combo_mm.nfl import synthetic_backtest as bt

__all__ = ["DEFAULT_GRID", "DEFAULT_CORR_SCALE_GRID", "TuningResult", "CorrScaleResult", "tune",
          "tune_corr_scale", "write_selection", "load_selection", "SELECTION_PATH"]

SELECTION_PATH = Path("params/estimator.json")

DEFAULT_GRID: Dict[str, Tuple[Any, ...]] = {
    "window_seasons": (4, 8, None),
    "half_life_seasons": (1.0, 2.0, 4.0),
    "var_shrink_multiplier": (1.0, 3.0),
}

# 0.0 = no margin/total dependence (matches league_constant exactly); 1.0 =
# the estimator's raw fitted asymmetry, untrusted. See tune_corr_scale.
DEFAULT_CORR_SCALE_GRID: Tuple[float, ...] = (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0)

METRIC = "brier_deployed_combos"


def _lookup(scores, model: str, column: str) -> float:
    """``scores.loc[model, column]``, or NaN if ``scores`` has no rows for it."""
    if scores.empty or model not in scores.index:
        return float("nan")
    return float(scores.loc[model, column])


def _score_lifted(combos, cols: Sequence[str]):
    """``bt.score_table`` on each model's market_lift price, indexed by bare model name.

    market_lift (``combo_mm.nfl.pricing``'s default, and the live pricer's)
    is what every quote actually uses; scoring the raw joint instead would
    silently pick hyperparameters for a formula nothing ships.
    """
    lift_cols = [bt.lifted(c) for c in cols]
    scores = bt.score_table(combos, lift_cols)
    if not scores.empty:
        scores = scores.assign(model=scores["model"].str.replace(r"_lift$", "", regex=True))
    return scores.set_index("model")


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
        # Deployed-family metrics score the market_lift price (what a live
        # quote actually is), not the raw model joint used for "brier"/
        # "brier_skill" above: the two coincide for spread x total (its legs
        # are calibrated to the market exactly) but not for ML x total,
        # where the model's own moneyline marginal is known to miss the
        # market (docs/correlation-model.md #4.4.2) and market_lift is
        # specifically what avoids importing that bias.
        deployed = _score_lifted(out.combos[bt.deployed_mask(out.combos)], cols)
        st_scores = _score_lifted(out.combos[out.combos["family"] == "spread x total"], cols)
        mt_scores = _score_lifted(out.combos[out.combos["family"] == "ML x total"], cols)
        for m in models:
            rows.append({
                **point, "variance_model": m, "n_combos": int(all_scores.loc[m, "n"]),
                "brier": all_scores.loc[m, "brier"], "brier_naive": all_scores.loc["naive", "brier"],
                "brier_skill": all_scores.loc[m, "brier_skill_vs_naive"], "t_stat": all_scores.loc[m, "t_stat"],
                "brier_skill_non_nested": non_nested.loc[m, "brier_skill_vs_naive"],
                "brier_skill_spread_total": _lookup(st_scores, m, "brier_skill_vs_naive"),
                "brier_skill_ml_total": _lookup(mt_scores, m, "brier_skill_vs_naive"),
                "n_deployed": 0 if math.isnan(_lookup(deployed, m, "n")) else int(_lookup(deployed, m, "n")),
                "brier_deployed": _lookup(deployed, m, "brier"),
                "brier_naive_deployed": _lookup(deployed, "naive", "brier"),
                "brier_skill_deployed": _lookup(deployed, m, "brier_skill_vs_naive"),
                "t_stat_deployed": _lookup(deployed, m, "t_stat"),
                "log_loss": all_scores.loc[m, "log_loss"], "grid_index": len(rows),
            })
        if progress:
            progress(i + 1, len(points), point)
    frame = pd.DataFrame(rows).sort_values(["brier_deployed", "grid_index"], kind="stable").reset_index(drop=True)
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
        # Selection metric: Brier on the deployed combo universe (ML x total,
        # spread x total) -- the only same-game blocks live RFQs ever send.
        "train_brier": float(best["brier_deployed"]),
        "train_brier_naive": float(best["brier_naive_deployed"]),
        "train_brier_skill": float(best["brier_skill_deployed"]),
        "train_n_deployed": int(best["n_deployed"]),
        # Kept for comparison: the old (all-combo) metric this replaced.
        "train_brier_all_combos": float(best["brier"]),
        "train_brier_all_combos_naive": float(best["brier_naive"]),
        "train_brier_skill_all_combos": float(best["brier_skill"]),
        "runner_up_brier": float(frame.iloc[1]["brier_deployed"]) if len(frame) > 1 else None,
    }
    return TuningResult(grid=frame, selected=selected,
                        train_seasons=(config.first_season, config.train_last_season),
                        search_space={k: tuple(v) for k, v in grid.items()})


@dataclass
class CorrScaleResult:
    table: Any                # pandas DataFrame: corr_scale -> deployed Brier/skill (market_lift)
    corr_scale: float         # selected value
    brier_deployed: float
    brier_naive_deployed: float
    brier_skill_deployed: float


def tune_corr_scale(games: Sequence[Game], config: bt.BacktestConfig, estimator: EstimatorConfig,
                    scales: Sequence[float] = DEFAULT_CORR_SCALE_GRID,
                    workers: Optional[int] = None) -> CorrScaleResult:
    """Train-only selection of the pricing-time ``corr_scale`` for ``estimator``.

    ``corr_scale`` is not part of the weekly params file -- it scales
    ``matchup_covariance``'s margin/total dependence at PRICING time
    (``NflLivePricerConfig.corr_scale``, default 1.0: the raw fit, untrusted).
    A single global slope fit over 16 seasons has real sampling error (see
    ``combo_mm/nfl/estimate.py``'s note on the variance slope's standard
    error), so ``tune``'s own grid always scores each variance model at the
    unscaled fit -- which is why ``league_constant`` (identically zero
    dependence, so nothing to overfit) tends to win there even when a richer
    model's dependence is directionally real. This scans how much of that
    fit to actually trust, scoring the deployed combo universe's market_lift
    price (matching :func:`tune`'s own metric), train period only.

    Meaningless for ``league_constant``: ``Cov(margin, total) = sigma_home^2
    - sigma_away^2`` is exactly zero for it regardless of scale, so every
    point in the grid ties naive identically -- call this only for
    ``mean_linear`` / ``mean_linear_team``.

    This is a second stage on top of ``estimator``'s own (window, half_life,
    shrink) -- already chosen by :func:`tune` at the unscaled fit, not
    rechecked here for every scale. Their effect on Brier is an order of
    magnitude smaller than corr_scale's (see the tuning grid), so this
    two-stage approximation is far cheaper than a joint search and, on the
    evidence so far, does not change the ranking.
    """
    scales = tuple(scales)
    if 1.0 not in scales:
        scales = scales + (1.0,)
    train_games = [g for g in games if g.season <= config.train_last_season]
    run_cfg = dataclasses.replace(
        config, last_season=config.train_last_season, structure=False,
        variance_models=(estimator.variance_model,), primary_model=estimator.variance_model,
        sensitivity_model=estimator.variance_model, corr_scales=scales, estimator=estimator,
    )
    out = bt.run_backtest(train_games, run_cfg, workers=workers)
    deployed = out.combos[bt.deployed_mask(out.combos)]
    table = bt.sensitivity_table(deployed, scales, estimator.variance_model, use_lift=True)
    best = table.loc[table["brier"].idxmin()]
    naive_brier = float(bt.score_table(deployed, []).iloc[0]["brier"]) if not deployed.empty else float("nan")
    return CorrScaleResult(table=table, corr_scale=float(best["corr_scale"]),
                           brier_deployed=float(best["brier"]), brier_naive_deployed=naive_brier,
                           brier_skill_deployed=float(best["brier_skill_vs_naive"]))


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

#!/usr/bin/env python3
"""Tune the NFL covariance estimator on the TRAIN period only, then freeze it.

Two stages, both train-only (test seasons removed from the input before
anything runs) and both scored by Brier on the deployed combo universe's
market_lift price (moneyline x total, spread x total -- the only same-game
blocks a live RFQ ever sends; see ``combo_mm.nfl.tuning``):

1. Grid-search variance model x trailing window x recency half-life x
   variance-factor shrinkage. Writes the full grid to
   ``results/nfl_tuning/grid.csv``.
2. For each asymmetry-capable model's own best row from stage 1
   (``mean_linear``, ``mean_linear_team`` -- ``league_constant``'s
   margin/total dependence is exactly zero at every scale, so it has
   nothing to shrink), scan the pricing-time ``corr_scale`` that trusts the
   fitted dependence: a raw (``corr_scale=1``) fit is a noisy point estimate
   that tends to overshoot, and a partially-shrunk version can beat both
   "none" and "all of it". Kept only if it beats the stage-1 winner outright.

Writes the winner to ``params/estimator.json``, which
``scripts/nfl_backtest.py`` and ``scripts/refresh_params.py`` then use;
``corr_scale`` is not baked into the weekly params file (it applies at
pricing time, see ``combo_mm.nfl.live_pricer.NflLivePricerConfig``) and is
stored alongside as ``pricer_corr_scale``.

    python scripts/nfl_tune.py                  # ~10 minutes on 4 workers
    python scripts/nfl_tune.py --train-last-season 2021 --workers 4
    python scripts/nfl_tune.py --skip-corr-scale-tuning   # stage 1 only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.nfl import synthetic_backtest as bt  # noqa: E402
from combo_mm.nfl.estimate import EstimatorConfig  # noqa: E402
from combo_mm.nfl.ingest import latest_pull, load_games, pull_games  # noqa: E402
from combo_mm.nfl.tuning import SELECTION_PATH, tune, tune_corr_scale, write_selection  # noqa: E402

# league_constant's Cov(margin, total) is exactly zero at every corr_scale
# (see combo_mm.nfl.params_io.matchup_covariance): nothing to shrink.
_SHRINKABLE_MODELS = ("mean_linear", "mean_linear_team")


def _row_estimator(config: bt.BacktestConfig, search_space, row) -> EstimatorConfig:
    """Rebuild the EstimatorConfig a stage-1 grid row represents."""
    kwargs = {}
    for key in search_space:
        value = row[key]
        if value is None or (isinstance(value, float) and pd.isna(value)):
            kwargs[key] = None
        elif key == "window_seasons":
            kwargs[key] = int(value)
        else:
            kwargs[key] = float(value)
    return EstimatorConfig(**{**config.estimator.to_dict(), "variance_model": row["variance_model"], **kwargs})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-root", default="data/raw")
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--first-season", type=int, default=2006)
    ap.add_argument("--train-last-season", type=int, default=2021)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--out", default="results/nfl_tuning")
    ap.add_argument("--selection", default=str(SELECTION_PATH))
    ap.add_argument("--skip-corr-scale-tuning", action="store_true",
                    help="stage 1 only: freeze the winner at corr_scale=1 (unshrunk)")
    args = ap.parse_args(argv)

    pull = pull_games(args.raw_root) if args.pull else latest_pull(args.raw_root)
    if pull is None:
        print(f"No cached nflverse pull under {args.raw_root}; rerun with --pull.", file=sys.stderr)
        return 2
    games = load_games(pull)
    config = bt.BacktestConfig(first_season=args.first_season, train_last_season=args.train_last_season)
    print(f"Tuning on train seasons {config.first_season}-{config.train_last_season} "
          f"(games after {config.train_last_season} removed from the input)")

    def progress(done, total, point):
        print(f"  [{done}/{total}] {point}", flush=True)

    result = tune(games, config, workers=args.workers, progress=progress)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result.grid.to_csv(out / "grid.csv", index=False)

    selected = dict(result.selected)
    selected["pricer_corr_scale"] = 1.0
    best_brier = float(result.grid.iloc[0]["brier_deployed"])
    print(f"\nStage 1 winner: {selected['variance_model']} (brier_deployed={best_brier:.6f}, "
          f"skill={selected['train_brier_skill']:+.6f})")

    if not args.skip_corr_scale_tuning:
        for model in _SHRINKABLE_MODELS:
            rows = result.grid[result.grid["variance_model"] == model]
            if rows.empty:
                continue
            row = rows.iloc[0]  # this model's own best (window, half_life, shrink) at corr_scale=1
            estimator = _row_estimator(config, result.search_space, row)
            cs = tune_corr_scale(games, config, estimator, workers=args.workers)
            print(f"  corr_scale scan ({model}, {estimator.to_dict()}): "
                  f"best corr_scale={cs.corr_scale:g}, brier_deployed={cs.brier_deployed:.6f}, "
                  f"skill={cs.brier_skill_deployed:+.6f}")
            if cs.brier_deployed < best_brier:
                best_brier = cs.brier_deployed
                selected = {
                    "variance_model": model,
                    "estimator": estimator.to_dict(),
                    "pricer_corr_scale": cs.corr_scale,
                    "train_brier": cs.brier_deployed,
                    "train_brier_naive": cs.brier_naive_deployed,
                    "train_brier_skill": cs.brier_skill_deployed,
                    "train_n_deployed": result.selected["train_n_deployed"],
                    # Not recomputed for the corr_scale winner (would need
                    # its own all-combo/non-nested scoring pass); the
                    # deployed-combo numbers above are the selection metric.
                    "train_brier_all_combos": None,
                    "train_brier_all_combos_naive": None,
                    "train_brier_skill_all_combos": None,
                    "runner_up_brier": result.selected["train_brier"],
                }
    result.selected = selected
    path = write_selection(result, args.selection, data_vintage={"pull_date": pull.pull_date, "sha256": pull.sha256})

    pd.set_option("display.width", 200)
    print("\nTop candidates (train period, stage 1):")
    print(result.grid.drop(columns=["grid_index"]).head(10).round(5).to_string(index=False))
    print(f"\nSelected: {selected['variance_model']} corr_scale={selected['pricer_corr_scale']:g} "
          f"{selected['estimator']}")
    print(f"Deployed-combo skill vs naive: {selected['train_brier_skill']:+.6f} "
          f"(brier {selected['train_brier']:.6f} vs naive {selected['train_brier_naive']:.6f})")
    print(f"Frozen to {path}; grid -> {out / 'grid.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

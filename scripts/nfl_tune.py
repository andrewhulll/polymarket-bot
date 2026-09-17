#!/usr/bin/env python3
"""Tune the NFL covariance estimator on the TRAIN period only, then freeze it.

Grid-searches variance model x trailing window x recency half-life x
variance-factor shrinkage with the walk-forward backtest over the train
seasons (default 2006-2021; test seasons are removed from the input before
anything runs). Writes the full grid to ``results/nfl_tuning/grid.csv`` and
the winner to ``params/estimator.json``, which ``scripts/nfl_backtest.py``
and ``scripts/refresh_params.py`` then use.

    python scripts/nfl_tune.py                  # ~10 minutes on 4 workers
    python scripts/nfl_tune.py --train-last-season 2021 --workers 4
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.nfl import synthetic_backtest as bt  # noqa: E402
from combo_mm.nfl.ingest import latest_pull, load_games, pull_games  # noqa: E402
from combo_mm.nfl.tuning import SELECTION_PATH, tune, write_selection  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-root", default="data/raw")
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--first-season", type=int, default=2006)
    ap.add_argument("--train-last-season", type=int, default=2021)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--out", default="results/nfl_tuning")
    ap.add_argument("--selection", default=str(SELECTION_PATH))
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
    path = write_selection(result, args.selection, data_vintage={"pull_date": pull.pull_date, "sha256": pull.sha256})

    import pandas as pd
    pd.set_option("display.width", 200)
    print("\nTop candidates (train period):")
    print(result.grid.drop(columns=["grid_index"]).head(10).round(5).to_string(index=False))
    print(f"\nSelected: {result.selected['variance_model']} {result.selected['estimator']}")
    print(f"Frozen to {path}; grid -> {out / 'grid.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

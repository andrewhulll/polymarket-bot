#!/usr/bin/env python3
"""Walk-forward NFL same-game combo backtest (issue #6 validation).

Prices every same-game combo (one side of 2-3 of moneyline / spread / total)
for each historical game with walk-forward covariance params, against the
naive product of de-vigged closing prices and the realized payout. Writes
``combos.csv``, ``games.csv``, ``params_history.csv`` and ``meta.json`` to
``--out`` (the dashboard's "NFL correlation" tab reads them) and prints the
headline tables.

Offline only: needs numpy / scipy / pandas (``requirements-nfl.txt``).

    python scripts/nfl_tune.py                          # first: tune on 2006-2021 only
    python scripts/nfl_backtest.py                      # train 2006-2021, test 2022-2025
    python scripts/nfl_backtest.py --pull               # fetch today's nflverse data first
    python scripts/nfl_backtest.py --first-season 2020 --workers 4
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.nfl import synthetic_backtest as bt  # noqa: E402
from combo_mm.nfl.estimate import EstimatorConfig  # noqa: E402
from combo_mm.nfl.ingest import latest_pull, load_games, pull_games  # noqa: E402
from combo_mm.nfl.tuning import SELECTION_PATH, load_selection  # noqa: E402

DEFAULT_OUT = Path("results/nfl_backtest")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-root", default="data/raw", help="nflverse cache root")
    ap.add_argument("--pull", action="store_true", help="download a fresh nflverse pull first")
    ap.add_argument("--first-season", type=int, default=2006)
    ap.add_argument("--train-last-season", type=int, default=2021)
    ap.add_argument("--last-season", type=int, default=2025)
    ap.add_argument("--estimator-config", default=str(SELECTION_PATH),
                    help="frozen estimator from scripts/nfl_tune.py (train-period tuning)")
    ap.add_argument("--edge-threshold", type=float, default=0.01)
    ap.add_argument("--no-playoffs", action="store_true")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)

    pull = pull_games(args.raw_root) if args.pull else latest_pull(args.raw_root)
    if pull is None:
        print(f"No cached nflverse pull under {args.raw_root}; rerun with --pull.", file=sys.stderr)
        return 2
    games = load_games(pull)
    selection = load_selection(args.estimator_config)
    if selection is None:
        print(f"WARNING: {args.estimator_config} not found -- using untuned EstimatorConfig defaults. "
              "Run scripts/nfl_tune.py first so hyperparameters come from the train period only.")
        estimator, primary_model, tuned = EstimatorConfig(), "mean_linear", None
    else:
        estimator, payload = selection
        primary_model = estimator.variance_model
        tuned = {"path": args.estimator_config, "train_seasons": payload["train_seasons"],
                 "train_brier": payload["train_brier"]}
        if payload["train_seasons"][1] != args.train_last_season:
            print(f"WARNING: estimator was tuned through {payload['train_seasons'][1]} but the train period "
                  f"here ends {args.train_last_season}.")
    config = bt.BacktestConfig(
        first_season=args.first_season, last_season=args.last_season,
        train_last_season=args.train_last_season, primary_model=primary_model,
        sensitivity_model="mean_linear", edge_threshold=args.edge_threshold,
        include_playoffs=not args.no_playoffs, estimator=estimator,
    )
    print(f"nflverse pull {pull.pull_date} (sha256 {pull.sha256[:12]}), {len(bt.COMBOS)} combo types\n"
          f"train {config.split_summary()['train']}, test {config.split_summary()['test']}; "
          f"estimator: {estimator.to_dict()}")

    def progress(done: int, total: int) -> None:
        print(f"  seasons done: {done}/{total}", flush=True)

    out = bt.run_backtest(games, config, data_vintage={"pull_date": pull.pull_date, "sha256": pull.sha256},
                          progress=progress, workers=args.workers)
    out.meta["tuned_estimator"] = tuned
    directory = bt.write_outputs(out, args.out)

    import pandas as pd
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    m = out.meta
    print(f"\n{m['n_games']} games, {m['n_combo_rows']} combos ({m['n_pushed']} pushed, dropped), "
          f"{m['runtime_s']}s on {m['workers']} workers -> {directory}")
    primary = bt.model_col(config.primary_model)
    for split in (bt.TRAIN, bt.TEST):
        combos = out.combos[out.combos["split"] == split]
        if combos.empty:
            continue
        seasons = sorted(combos["season"].unique())
        print(f"\n===== {split.upper()} ({seasons[0]}-{seasons[-1]}, {combos['game_id'].nunique()} games) =====")
        print("Scores vs naive (all combos):")
        print(bt.score_table(combos, bt.prob_columns(config)).round(5).to_string(index=False))
        print("Scores vs naive (excluding nested combos):")
        print(bt.score_table(combos[~combos["nested"].astype(bool)], bt.prob_columns(config)).round(5).to_string(index=False))
        print(f"By family ({config.primary_model}):")
        print(bt.group_table(combos, "family", primary).round(4).to_string(index=False))
        print(f"Sensitivity to correlation ({config.scaled_model}; spread x total + ML x total):")
        cross = combos[combos["family"].isin(["spread x total", "ML x total"])]
        print(bt.sensitivity_table(cross, config.corr_scales, config.scaled_model,
                                   config.edge_threshold).round(4).to_string(index=False))
        games_split = out.games[out.games["split"] == split]
        print("Corr(favorite margin, total) by spread bucket:")
        print(bt.spread_bucket_structure(games_split).round(3).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Weekly NFL covariance params refresh (run Wednesdays, before the slate locks).

pull nflverse -> validate -> estimate (games strictly before the target week)
-> stage -> gates (range sanity, no regression, determinism) -> promote to
``params/nfl_<season>_w<ww>.json``. Offseason: freezes the last file.

    python scripts/refresh_params.py --pull                 # next unplayed week
    python scripts/refresh_params.py --season 2026 --week 2 # explicit target
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.nfl.estimate import EstimatorConfig  # noqa: E402
from combo_mm.nfl.ingest import latest_pull, load_games, pull_games  # noqa: E402
from combo_mm.nfl.params_io import VARIANCE_MODELS  # noqa: E402
from combo_mm.nfl.refresh import PROMOTED, refresh  # noqa: E402
from combo_mm.nfl.tuning import SELECTION_PATH, load_selection  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-root", default="data/raw")
    ap.add_argument("--pull", action="store_true", help="download today's nflverse data first")
    ap.add_argument("--params-dir", default="params")
    ap.add_argument("--season", type=int)
    ap.add_argument("--week", type=int)
    ap.add_argument("--estimator-config", default=str(SELECTION_PATH),
                    help="frozen estimator from scripts/nfl_tune.py (default: params/estimator.json)")
    ap.add_argument("--variance-model", default=None, choices=VARIANCE_MODELS,
                    help="override the tuned variance model")
    ap.add_argument("--gate-lookback-games", type=int, default=272)
    ap.add_argument("--tolerance", type=float, default=1e-3)
    args = ap.parse_args(argv)
    if (args.season is None) != (args.week is None):
        ap.error("--season and --week go together")

    pull = pull_games(args.raw_root) if args.pull else latest_pull(args.raw_root)
    if pull is None:
        print(f"No cached nflverse pull under {args.raw_root}; rerun with --pull.", file=sys.stderr)
        return 2
    games = load_games(pull)
    selection = load_selection(args.estimator_config)
    if selection is None:
        print(f"WARNING: {args.estimator_config} not found -- using untuned EstimatorConfig defaults "
              "(run scripts/nfl_tune.py).", file=sys.stderr)
        estimator = EstimatorConfig()
    else:
        estimator = selection[0]
    if args.variance_model:
        estimator = dataclasses.replace(estimator, variance_model=args.variance_model)
    result = refresh(
        games, args.params_dir,
        data_vintage={"pull_date": pull.pull_date, "sha256": pull.sha256,
                      "source_url": pull.manifest.get("source_url")},
        season=args.season, week=args.week,
        config=estimator,
        gate_lookback_games=args.gate_lookback_games, tolerance=args.tolerance,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status in (PROMOTED, "offseason_frozen") else 1


if __name__ == "__main__":
    raise SystemExit(main())

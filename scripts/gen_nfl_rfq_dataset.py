#!/usr/bin/env python3
"""Generate a simulated NFL RFQ replay dataset from cached nflverse history (#15).

Steps 2, 3 and 5 need NFL RFQ flow to replay and none exists (see #11), so this
manufactures it from historical games in the wire format the pipeline already
replays. Output is deterministic for a given seed and recorded in a manifest.

    # test period (scored once), and the train period used to tune knobs
    python scripts/gen_nfl_rfq_dataset.py --seasons 2022-2025 --seed 7 \
        --rfqs-per-game 40 --out data/rfq_sim/test_2022_2025
    python scripts/gen_nfl_rfq_dataset.py --seasons 2006-2021 --seed 7 \
        --out data/rfq_sim/train_2006_2021

Weekly covariance params are estimated walk-forward with the frozen estimator
(``params/estimator.json``) and cached under ``--params-history``, so the
dataset can never carry a parameter fitted on its own outcomes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.nfl.ingest import latest_pull, load_games, pull_games  # noqa: E402
from combo_mm.nfl.markets import PUSH_RULES, TIE_RULES  # noqa: E402
from combo_mm.nfl.rfq_sim import (  # noqa: E402
    SimConfig,
    WalkForwardParams,
    build_session,
    write_dataset,
)
from combo_mm.nfl.tuning import SELECTION_PATH, load_selection  # noqa: E402


def _parse_seasons(text: str) -> range:
    if "-" in text:
        first, last = text.split("-", 1)
        return range(int(first), int(last) + 1)
    return range(int(text), int(text) + 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seasons", required=True,
                    help="season or inclusive range, e.g. 2024 or 2022-2025")
    ap.add_argument("--out", required=True, help="dataset directory to write")
    ap.add_argument("--seed", type=int, default=SimConfig.seed)
    ap.add_argument("--rfqs-per-game", type=float, default=SimConfig.rfqs_per_game)
    ap.add_argument("--raw-root", default="data/raw")
    ap.add_argument("--pull", action="store_true",
                    help="download today's nflverse data first")
    ap.add_argument("--params-history", default="params/history",
                    help="where walk-forward weekly params are cached")
    ap.add_argument("--estimator-config", default=str(SELECTION_PATH),
                    help="frozen estimator from scripts/nfl_tune.py")
    ap.add_argument("--no-alt-lines", action="store_true",
                    help="list only main moneyline, spread and total markets")
    ap.add_argument("--allow-integer-lines", action="store_true",
                    help="do not snap listed lines to half points (pushes occur)")
    ap.add_argument("--push-rule", default=SimConfig.push_rule, choices=PUSH_RULES)
    ap.add_argument("--tie-rule", default=SimConfig.tie_rule, choices=TIE_RULES)
    ap.add_argument("--max-games", type=int, default=None,
                    help="cap the game count (smoke runs)")
    args = ap.parse_args(argv)

    pull = pull_games(args.raw_root) if args.pull else latest_pull(args.raw_root)
    if pull is None:
        print(f"No cached nflverse pull under {args.raw_root}; rerun with --pull.",
              file=sys.stderr)
        return 2
    games = load_games(pull)

    seasons = _parse_seasons(args.seasons)
    targets = [g for g in games if g.season in seasons and g.has_lines]
    if args.max_games is not None:
        targets = sorted(targets, key=lambda g: (g.season, g.week, g.game_id))[:args.max_games]
    if not targets:
        print(f"No games with closing lines in seasons {args.seasons}.", file=sys.stderr)
        return 2

    selection = load_selection(args.estimator_config)
    if selection is None:
        print(f"No frozen estimator at {args.estimator_config}; "
              "run scripts/nfl_tune.py first.", file=sys.stderr)
        return 2
    estimator, _ = selection

    config = SimConfig(
        seed=args.seed,
        rfqs_per_game=args.rfqs_per_game,
        alt_lines=not args.no_alt_lines,
        force_half_point_lines=not args.allow_integer_lines,
        push_rule=args.push_rule,
        tie_rule=args.tie_rule,
    )
    # Every game's params come from the full history, not just the target
    # seasons, so early target weeks still have their prior seasons.
    provider = WalkForwardParams(
        games, args.params_history, estimator,
        data_vintage={"pull_date": pull.pull_date, "sha256": pull.sha256})
    session = build_session(targets, config, params_provider=provider)
    manifest_path = write_dataset(
        session, args.out, config,
        source={"games_csv_sha256": pull.sha256, "pull_date": pull.pull_date,
                "seasons": [min(seasons), max(seasons)],
                "estimator_config": str(args.estimator_config),
                "estimator": estimator.to_dict(),
                "params_history": str(args.params_history)},
    )
    counts = {k: v for k, v in session.counts.items() if k != "params_weeks"}
    print(json.dumps(counts, indent=2, sort_keys=True))
    print(f"wrote {manifest_path.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

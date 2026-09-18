#!/usr/bin/env python3
"""One-click NFL week-1 backtest for the dashboard (offline research).

Replays every same-game combo (2-3 legs of moneyline / spread / total) from
the backtest week's games as RFQs through the real pipeline: the NFL joint
model prices each RFQ, a naive independent-leg maker competes, the better price
trades, and combos settle on the final scores. Writes the full quote lifecycle
to ``<data-dir>/week1_backtest.db``, which the dashboard can then show in its
live tabs via the data-source switcher.

Needs the research stack (``pip install -r requirements-nfl.txt``) and a
cached nflverse pull under ``data/raw`` (``python scripts/refresh_params.py
--pull``). Never touches the live capture database.

    python scripts/run_week_backtest.py --data-dir data/live
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.nfl import week_backtest  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data/live",
                    help="directory receiving week1_backtest.db")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parents[1]
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "week1_backtest.db"
    if out_path.exists():
        out_path.unlink()  # fresh replay every run

    out = week_backtest.run_from_pull(
        raw_root=repo / "data" / "raw",
        estimator_path=repo / "params" / "estimator.json",
        db_path=str(out_path),
        season=week_backtest.BACKTEST_SEASON,
        week=week_backtest.BACKTEST_WEEK,
    )
    meta = out.meta
    print(f"wrote {out.db_path}: {meta['n_rfqs']} RFQs across "
          f"{meta['n_games']} games "
          f"(pull {meta.get('data_vintage', {}).get('pull_date', '?')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

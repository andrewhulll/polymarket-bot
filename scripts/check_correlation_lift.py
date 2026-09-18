"""Dead-man's checker for the NFL correlation model actually affecting live prices.

Reads recent ``priced_quotes`` rows from ``data/live/rfq_capture.db`` and exits
non-zero when the joint model's correlation adjustment (``fair - naive``) is
degenerate -- near zero on almost every recently auto-quoted RFQ. That is a
silent failure distinct from the process being down: RFQs still price and
quote fine, they just carry no dependence beyond the naive product, which
defeats the reason this bot exists (see docs/correlation-model.md). The usual
cause is a params file whose variance model gives every game the same
sigma_home/sigma_away (``league_constant`` with ``var_slope == 0``), which
zeroes ``Cov(margin, total)`` identically for every same-game combo.

Wire it in alongside ``scripts/check_heartbeat.py``; the dashboard Engine
status tab shows the same distribution with a visible banner.

Exit codes: 0 = correlation model is affecting recent quotes, 2 = degenerate
or no data yet, 1 = usage error.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.live_view_models import connect_readonly, correlation_lift  # noqa: E402

EXIT_DEGENERATE = 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data/live",
                        help="capture output directory (default: data/live)")
    parser.add_argument("--window", type=int, default=1000,
                        help="most recent auto-quoted RFQs to sample (default: 1000)")
    parser.add_argument("--degenerate-bps", type=float, default=1.0,
                        help="a quote below this |corr adjustment| counts as degenerate (default: 1.0)")
    parser.add_argument("--degenerate-frac", type=float, default=0.95,
                        help="flag when at least this fraction of sampled quotes are degenerate "
                             "(default: 0.95)")
    parser.add_argument("--quiet", action="store_true",
                        help="only set the exit code, print nothing")
    args = parser.parse_args(argv)

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    db_path = Path(args.data_dir) / "rfq_capture.db"
    if not db_path.exists():
        say(f"NO DATA: {db_path} does not exist yet")
        return EXIT_DEGENERATE

    try:
        with closing(connect_readonly(db_path)) as conn:
            stats = correlation_lift(conn, window=args.window, degenerate_bps=args.degenerate_bps,
                                     degenerate_frac=args.degenerate_frac)
    except sqlite3.Error as exc:
        say(f"NO DATA: {db_path} unreadable ({type(exc).__name__}: {exc})")
        return EXIT_DEGENERATE

    if stats["n"] == 0:
        say(f"NO DATA: no QUOTED rows with a correlation adjustment yet in {db_path}")
        return EXIT_DEGENERATE
    if stats["degenerate"]:
        say(f"DEGENERATE: {stats['frac_degenerate']:.0%} of the last {stats['n']} auto-quotes show "
            f"< {args.degenerate_bps:g} bps of correlation adjustment (mean {stats['mean_abs_bps']:.3f}, "
            f"p95 {stats['p95_abs_bps']:.3f}, max {stats['max_abs_bps']:.3f} bps) in {db_path}")
        return EXIT_DEGENERATE
    say(f"OK: last {stats['n']} auto-quotes show mean |corr adj| {stats['mean_abs_bps']:.2f} bps "
        f"(p95 {stats['p95_abs_bps']:.2f}, max {stats['max_abs_bps']:.2f}) in {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

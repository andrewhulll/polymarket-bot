"""Dead-man's checker for the headless RFQ capture process.

Reads ``live_engine_health.heartbeat_at`` from ``data/live/rfq_capture.db``
and exits non-zero when the heartbeat is older than ``--max-age-minutes``
(default 10) or when no heartbeat has ever been recorded. Wire it into
launchd (``StartInterval``, see
``deploy/com.polymarket-bot.heartbeat-check.plist``) or a systemd timer
(``deploy/polymarket-bot-heartbeat-check.timer``); the dashboard
Engine-status tab shows the same staleness with a visible banner.

Exit codes: 0 = heartbeat fresh, 2 = stale or missing, 1 = usage error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.capture_process import (DEFAULT_MAX_HEARTBEAT_AGE_S,
                                      read_heartbeat_age_s)

EXIT_STALE = 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data/live",
                        help="capture output directory (default: data/live)")
    parser.add_argument("--max-age-minutes", type=float,
                        default=DEFAULT_MAX_HEARTBEAT_AGE_S / 60,
                        help="stale threshold in minutes (default: 10)")
    parser.add_argument("--quiet", action="store_true",
                        help="only set the exit code, print nothing")
    args = parser.parse_args(argv)

    db_path = Path(args.data_dir) / "rfq_capture.db"
    age_s = read_heartbeat_age_s(db_path)
    max_age_s = args.max_age_minutes * 60

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    if age_s is None:
        say(f"STALE: no heartbeat recorded yet in {db_path}")
        return EXIT_STALE
    if age_s > max_age_s:
        say(f"STALE: last heartbeat {age_s / 60:.1f} min ago "
            f"(threshold {args.max_age_minutes:g} min) in {db_path}")
        return EXIT_STALE
    say(f"OK: last heartbeat {age_s:.0f}s ago in {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

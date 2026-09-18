"""Manually latch or reset the paper risk kill switch."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from combo_mm.store import EventStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite event store path")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--trip", action="store_true")
    action.add_argument("--reset", action="store_true")
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    store = EventStore(args.db)
    try:
        store.set_kill_switch(args.trip,
                              ts=datetime.now(timezone.utc).isoformat(),
                              reason=args.reason)
        print("tripped" if args.trip else "reset")
    finally:
        store.close()


if __name__ == "__main__":
    main()

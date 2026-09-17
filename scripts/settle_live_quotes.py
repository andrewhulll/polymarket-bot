#!/usr/bin/env python3
"""Score live quotes against final scores (plan: ``docs/settlement-tracking.md``).

The live quoter records what the NFL correlation model would have quoted; this
job says whether it was right. It reads ``priced_quotes``, settles each quoted
RFQ's legs against the cached nflverse pull, and writes ``quote_settlements``.

    python scripts/settle_live_quotes.py                  # settle what is due
    python scripts/settle_live_quotes.py --dry-run        # show, write nothing
    python scripts/settle_live_quotes.py --since 2026-09-01T00:00:00Z

Offline by design: scores come from the newest cached pull under ``data/raw``
and the leg catalog from its on-disk cache, so the job never touches the
network -- refresh scores with ``python scripts/refresh_params.py --pull``.
Idempotent: SETTLED, VOID and UNSETTLEABLE are terminal and skipped, while
PENDING and UNRESOLVED rows are retried every run -- which is how a finished
game, or a catalog that has caught up, flips them.

Nothing here was traded. The headline number is the model's Brier against the
naive independent-leg maker's, not P&L.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.combo_markets import ComboMarketCatalog  # noqa: E402
from combo_mm.nfl.ingest import latest_pull, load_games  # noqa: E402
from combo_mm.nfl.markets import PUSH_RULES, PUSH_VOID  # noqa: E402
from combo_mm.nfl.settle_live import (  # noqa: E402
    PENDING,
    SETTLED,
    UNRESOLVED,
    UNSETTLEABLE,
    VOID,
    GameIndex,
    settle_quote,
)
from combo_mm.quote_selections import QuoteSelectionStore  # noqa: E402

DEFAULT_DB = "data/live/quote_selections.db"
DEFAULT_CATALOG = "data/live/combo_markets.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"quote store (default: {DEFAULT_DB})")
    ap.add_argument("--catalog", default=DEFAULT_CATALOG,
                    help=f"combo catalog cache (default: {DEFAULT_CATALOG})")
    ap.add_argument("--raw-root", default="data/raw", help="cached nflverse pulls")
    ap.add_argument("--since", default=None, help="only quotes priced at or after this ISO time")
    ap.add_argument("--limit", type=int, default=5000, help="most quotes to settle in one run")
    ap.add_argument("--push-rule", default=PUSH_VOID, choices=PUSH_RULES,
                    help="how a pushed leg settles (default: void, which voids the combo)")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args(argv)

    if not Path(args.db).is_file():
        print(f"No quote store at {args.db}; run the dashboard's live monitor first.",
              file=sys.stderr)
        return 2
    pull = latest_pull(args.raw_root)
    if pull is None:
        print(f"No cached nflverse pull under {args.raw_root}; "
              "run `python scripts/refresh_params.py --pull`.", file=sys.stderr)
        return 2

    index = GameIndex(load_games(pull))
    catalog = ComboMarketCatalog(args.catalog)
    n_cached = catalog.load_cache()
    if n_cached == 0:
        print(f"WARNING: catalog cache {args.catalog} is empty or unreadable "
              f"({catalog.last_error or 'not found'}); legs will be UNRESOLVED.",
              file=sys.stderr)

    store = QuoteSelectionStore(args.db)
    try:
        due = store.quotes_needing_settlement(limit=args.limit, since=args.since)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        counts = {SETTLED: 0, VOID: 0, PENDING: 0, UNRESOLVED: 0, UNSETTLEABLE: 0}
        for quote in due:
            fill = store.accepted_quote(str(quote.get("rfq_id")))
            outcome = settle_quote(quote, catalog.lookup, index, settled_at=now,
                                   push_rule=args.push_rule, scores_vintage=pull.pull_date,
                                   fill=fill)
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            if not args.dry_run:
                store.record_settlement(outcome.to_dict())
        metrics = store.settlement_metrics()
    finally:
        store.close()

    summary = {"considered": len(due), "written": 0 if args.dry_run else len(due),
               "dry_run": args.dry_run, "scores_vintage": pull.pull_date,
               "catalog_positions": n_cached, **counts,
               "model_brier": metrics["brier"], "naive_brier": metrics["naive_brier"],
               "edge_vs_naive": metrics["edge_vs_naive"], "n_scored": metrics["n"]}
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"{len(due):,} quote(s) due{' (dry run)' if args.dry_run else ''}: "
              f"{counts[SETTLED]:,} settled, {counts[VOID]:,} void, "
              f"{counts[PENDING]:,} pending, {counts[UNRESOLVED]:,} unresolved, "
              f"{counts[UNSETTLEABLE]:,} unsettleable "
              f"[scores {pull.pull_date}]")
        if counts[UNSETTLEABLE]:
            print(f"  {counts[UNSETTLEABLE]:,} combo(s) carry a leg no final score settles "
                  "(prop, period or non-NFL); terminal, not retried.")
        if metrics["brier"] is not None:
            better = "better" if (metrics["edge_vs_naive"] or 0) > 0 else "worse"
            print(f"Scored {int(metrics['n']):,}: model Brier {metrics['brier']:.4f} vs "
                  f"naive {metrics['naive_brier']:.4f} "
                  f"({abs(metrics['edge_vs_naive'] or 0):.4f} {better}).")
        else:
            print("Nothing scored yet -- no settled quote carries a fair value.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

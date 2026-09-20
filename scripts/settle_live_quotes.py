#!/usr/bin/env python3
"""Settle priced live NFL same-game quotes against cached final scores.

Reads priced quotes from the durable quote-selections DB, resolves each leg
through the combo catalog cache, joins to the latest cached nflverse pull
and writes one idempotent ``quote_settlements`` row per (rfq_id, trigger).

Never touches the network: score freshness comes from
``python scripts/refresh_params.py --pull`` (the dashboard's Check
settlement button runs that first), and the catalog is read from its local
cache only. Only QUOTED quotes are scored -- declines carry no fair price.

    python scripts/settle_live_quotes.py \
        [--db data/live/rfq_capture.db] [--raw-root data/raw] \
        [--catalog data/live/combo_markets.json.gz] \
        [--since 2026-09-01T00:00:00Z] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from combo_mm.combo_markets import ComboMarketCatalog  # noqa: E402
from combo_mm.nfl.ingest import latest_pull, load_games  # noqa: E402
from combo_mm.nfl.settle_live import settle_quote  # noqa: E402
from combo_mm.quote_selections import QuoteSelectionStore  # noqa: E402

TERMINAL = ("SETTLED", "VOID")


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "data" / "live" / "rfq_capture.db"),
                    help="durable live capture and quote ledger DB")
    ap.add_argument("--raw-root", default=str(ROOT / "data" / "raw"),
                    help="cached nflverse pulls")
    ap.add_argument("--catalog", default=str(ROOT / "data" / "live" / "combo_markets.json.gz"),
                    help="combo catalog cache (read from cache only, never crawled here)")
    ap.add_argument("--since", default=None,
                    help="only quotes priced at or after this ISO timestamp")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute settlements but do not write them")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    store = QuoteSelectionStore(args.db)

    pull = latest_pull(args.raw_root)
    if pull is None:
        print(f"settle_live_quotes: no cached nflverse pull under {args.raw_root} "
              f"(run `python scripts/refresh_params.py --pull` first)", file=sys.stderr)
        return 2
    games = load_games(pull)

    catalog = ComboMarketCatalog(cache_path=args.catalog)
    n_cached = catalog.load_cache()  # read-only: no crawl thread, no network
    if n_cached == 0:
        print(f"settle_live_quotes: combo catalog cache unreadable or empty "
              f"({args.catalog}); legs will resolve UNRESOLVED", file=sys.stderr)

    quotes = store.quotes_needing_settlement(since=args.since)
    # Resolve every leg position once up front; the catalog is not consulted again.
    pids: list = []
    for q in quotes:
        for leg in (q.get("detail") or {}).get("legs") or []:
            pid = str((leg or {}).get("position_id") or "")
            if pid and pid not in pids:
                pids.append(pid)
    index = {}
    for pid, market in zip(pids, catalog.resolve(pids)):
        if market is not None:
            index[pid] = (market.slug, market.outcome_index)

    counts = {"SETTLED": 0, "VOID": 0, "PENDING": 0, "UNRESOLVED": 0, "ERROR": 0}
    for q in quotes:
        detail = q.get("detail") or {}
        try:
            row = settle_quote(
                rfq_id=q["rfq_id"], trigger=q["trigger"],
                legs=detail.get("legs") or [],
                fair=q.get("fair"), naive=q.get("naive"),
                bid=q.get("bid"), ask=q.get("ask"),
                model_version=q.get("model_version"),
                params_version=q.get("params_version"),
                catalog_index=index, games=games,
                scores_vintage=pull.pull_date)
        except Exception as exc:  # never let one bad quote kill the batch
            counts["ERROR"] += 1
            print(f"settle_live_quotes: ERROR {q.get('rfq_id')}|{q.get('trigger')}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        counts[row["status"]] += 1
        if not args.dry_run:
            store.record_quote_settlement(row)

    print("settle_live_quotes: processed={n} settled={s} void={v} pending={p} "
          "unresolved={u} errors={e} dry_run={d} db={db} scores_vintage={vint}".format(
              n=len(quotes), s=counts["SETTLED"], v=counts["VOID"], p=counts["PENDING"],
              u=counts["UNRESOLVED"], e=counts["ERROR"], d=args.dry_run,
              db=args.db, vint=pull.pull_date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

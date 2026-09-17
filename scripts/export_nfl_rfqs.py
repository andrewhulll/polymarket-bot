"""Export the NFL slice of a live RFQ capture (scripts/capture_live_rfqs.py)
to a flat CSV/JSON file. Read-only; does not touch the dashboard or pricing.

An RFQ counts as NFL if any of its legs resolved to an NFL market in the
combo catalog (``rfq_screen.n_nfl_legs > 0``) -- broader than the
``QUOTABLE`` screen used for live quoting, since this is for "give me the
data", not "what would we quote".

Usage::

    python3 scripts/export_nfl_rfqs.py --data-dir data/live --out data/live/nfl_rfqs
    # -> data/live/nfl_rfqs.csv and data/live/nfl_rfqs.json

    python3 scripts/export_nfl_rfqs.py --since 2026-09-10 --until 2026-09-16
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.combo_markets import ComboMarketCatalog
from combo_mm.store import EventStore


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_trade_extras(raw_path: Path) -> Dict[str, Dict[str, Any]]:
    """rfq_id -> {price, size, executed_at, ...} from the raw JSONL capture.

    Trade extras (accepted price/size) never make it into rfq_capture.db --
    normalize() drops gateway-native fields it does not recognize (see
    combo_mm/intl_gateway.py). The raw log is the only place they survive.
    Last trade frame per RFQ wins.
    """
    extras: Dict[str, Dict[str, Any]] = {}
    if not raw_path.is_file():
        return extras
    with raw_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw = row.get("raw") or {}
            rfq_id = raw.get("rfq_id")
            if not rfq_id or "price" not in raw:
                continue
            extras[str(rfq_id)] = {
                "price": raw.get("price"),
                "size": raw.get("size"),
                "executed_at": raw.get("executed_at"),
                "direction": raw.get("direction"),
                "side": raw.get("side"),
            }
    return extras


def nfl_rfq_ids(db_path: Path) -> List[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT rfq_id FROM rfq_screen WHERE n_nfl_legs > 0").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def describe_leg(catalog: ComboMarketCatalog, symbol: str) -> str:
    market = catalog.lookup(symbol)
    if market is None:
        return symbol
    return f"{market.title} [{market.outcome}]"


def build_rows(data_dir: Path, *, since: Optional[datetime] = None,
               until: Optional[datetime] = None) -> List[Dict[str, Any]]:
    db_path = data_dir / "rfq_capture.db"
    if not db_path.is_file():
        raise FileNotFoundError(
            f"{db_path} not found -- run scripts/capture_live_rfqs.py first")
    catalog = ComboMarketCatalog(cache_path=data_dir / "combo_markets.json")
    catalog.load_cache()
    trade_extras = load_trade_extras(data_dir / "rfq_raw.jsonl")
    store = EventStore(str(db_path))
    try:
        rows: List[Dict[str, Any]] = []
        for rfq_id in nfl_rfq_ids(db_path):
            entity = store.get_rfq(rfq_id)
            screen = store.get_rfq_screen(rfq_id)
            if entity is None or screen is None:
                continue
            created = _parse_ts(entity["created_time"])
            if since is not None and (created is None or created < since):
                continue
            if until is not None and (created is None or created > until):
                continue
            legs = [describe_leg(catalog, leg["symbol"]) for leg in entity["legs"]]
            trade = trade_extras.get(rfq_id, {})
            rows.append({
                "rfq_id": rfq_id,
                "status": entity["status"],
                "screen": screen["screen"],
                "n_legs": screen["n_legs"],
                "n_nfl_legs": screen["n_nfl_legs"],
                "direction": screen["direction"],
                "combo_side": screen["side"],
                "qty_decimal": entity["qty_decimal"],
                "cash_order_qty": entity["cash_order_qty"],
                "created_time": entity["created_time"],
                "updated_time": entity["updated_time"],
                "submission_deadline": screen["submission_deadline"],
                "legs": legs,
                "traded_price": trade.get("price"),
                "traded_size": trade.get("size"),
                "executed_at": trade.get("executed_at"),
            })
        rows.sort(key=lambda r: r["created_time"] or "")
        return rows
    finally:
        store.close()


def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "rfq_id", "status", "screen", "n_legs", "n_nfl_legs", "direction",
        "combo_side", "qty_decimal", "cash_order_qty", "created_time",
        "updated_time", "submission_deadline", "legs", "traded_price",
        "traded_size", "executed_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            flat = dict(row)
            flat["legs"] = "; ".join(row["legs"])
            writer.writerow(flat)


def write_json(rows: List[Dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data/live")
    parser.add_argument("--out", default="data/live/nfl_rfqs",
                        help="output path without extension")
    parser.add_argument("--since", default=None, help="ISO date/time, inclusive")
    parser.add_argument("--until", default=None, help="ISO date/time, inclusive")
    args = parser.parse_args(argv)

    since = _parse_ts(args.since) if args.since else None
    until = _parse_ts(args.until) if args.until else None
    rows = build_rows(Path(args.data_dir), since=since, until=until)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, out.with_suffix(".csv"))
    write_json(rows, out.with_suffix(".json"))
    print(f"{len(rows)} NFL RFQs -> {out.with_suffix('.csv')}, {out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

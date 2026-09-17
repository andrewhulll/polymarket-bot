"""scripts/capture_live_rfqs.py and scripts/export_nfl_rfqs.py, no network."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from capture_live_rfqs import RfqCapture  # noqa: E402
from export_nfl_rfqs import build_rows, load_trade_extras  # noqa: E402
from combo_mm.quote_selections import QuoteSelectionStore

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


def market(mid, slug, title, tags, ids, outcomes=("Yes", "No")):
    return {"id": mid, "condition_id": f"0x{mid}", "position_ids": list(ids), "slug": slug,
            "title": title, "outcomes": list(outcomes),
            "outcome_prices": ["0.6", "0.4"], "tags": list(tags), "volume": 1.0}


NFL_GAME = market("1", "nfl-sea-ari-2026-09-13", "Seahawks vs. Cardinals",
                  ["sports", "nfl", "games"], ["100", "101"],
                  outcomes=("Seahawks", "Cardinals"))
SOCCER_GAME = market("2", "lal-bet-get-2026-09-17-bet", "Real Betis vs. Getafe",
                     ["sports", "soccer", "games"], ["200", "201"])


def _rfq_request(rfq_id, leg_ids, condition_id="0xnfl"):
    return {
        "event_type": "rfq_created",
        "rfq_id": rfq_id,
        "exchange_ts": NOW.isoformat(),
        "symbol": condition_id,
        "id": rfq_id,
        "createdTime": NOW.isoformat(),
        "updatedTime": NOW.isoformat(),
        "status": "RFQ_STATUS_OPEN",
        "rfqCreatorUserId": "req_1",
        "comboLegs": [{"symbol": lid, "side": "YES", "settlementPrice": None}
                      for lid in leg_ids],
        "direction": "BUY",
        "condition_id": condition_id,
        "cashOrderQty": "1500",
    }


def _rfq_trade(rfq_id, price="0.55", size="1500", condition_id="0xnfl"):
    return {
        "event_type": "rfq_closed",
        "rfq_id": rfq_id,
        "exchange_ts": NOW.isoformat(),
        "symbol": condition_id,
        "id": rfq_id,
        "updatedTime": NOW.isoformat(),
        "direction": "BUY",
        "condition_id": condition_id,
        "side": "YES",
        "requester_id": "req_1",
        "price": price,
        "size": size,
        "executed_at": NOW.isoformat(),
    }


def _build_capture(tmp_path):
    from combo_mm.combo_markets import parse_catalog_page

    capture = RfqCapture(tmp_path)
    capture.catalog.merge(parse_catalog_page({"markets": [NFL_GAME, SOCCER_GAME]}))
    capture.catalog.save_cache()  # export reads the catalog from disk, not this process
    return capture


def test_handle_writes_raw_jsonl_and_screens_nfl(tmp_path):
    capture = _build_capture(tmp_path)
    capture.handle({"kind": "event", "raw": _rfq_request("rfq_nfl", ["100", "101"])}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_request("rfq_soccer", ["200", "201"], "0xsoccer")}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_trade("rfq_nfl")}, NOW)

    assert capture.rfqs_seen == 2
    assert capture.nfl_rfqs_seen == 1
    assert capture.trades_seen == 1

    raw_lines = capture.raw_path.read_text().splitlines()
    assert len(raw_lines) == 3
    first = json.loads(raw_lines[0])
    assert first["raw"]["rfq_id"] == "rfq_nfl"

    nfl_screen = capture.store.get_rfq_screen("rfq_nfl")
    soccer_screen = capture.store.get_rfq_screen("rfq_soccer")
    assert nfl_screen["n_nfl_legs"] == 2
    assert soccer_screen["n_nfl_legs"] == 0
    capture.stop()


def test_handle_ignores_book_items(tmp_path):
    capture = _build_capture(tmp_path)
    capture.handle({"kind": "book", "symbol": "100", "bid": 0.5, "ask": 0.51}, NOW)
    assert capture.rfqs_seen == 0
    assert capture.raw_path.read_text() == ""
    capture.stop()


def test_rescreen_records_decline_when_pricing_unavailable(tmp_path):
    from combo_mm.combo_markets import parse_catalog_page

    capture = RfqCapture(tmp_path)
    capture.selections = QuoteSelectionStore(tmp_path / "rfq_capture.db")
    capture.handle({"kind": "event", "raw": _rfq_request("late", ["100", "101"])}, NOW)
    assert capture.store.get_rfq_screen("late")["screen"] == "UNRESOLVED"
    capture.catalog.merge(parse_catalog_page({"markets": [NFL_GAME]}))
    capture.rescreen_unresolved()
    assert capture.store.get_rfq_screen("late")["screen"] == "QUOTABLE"
    quote = capture.selections.list_priced_quotes(rfq_id="late")[0]
    assert quote["reason_code"] == "PRICING_UNAVAILABLE"
    capture.stop()


def test_load_trade_extras_reads_last_trade_per_rfq(tmp_path):
    raw_path = tmp_path / "rfq_raw.jsonl"
    lines = [
        {"received_at": NOW.isoformat(), "raw": _rfq_request("rfq_nfl", ["100", "101"])},
        {"received_at": NOW.isoformat(), "raw": _rfq_trade("rfq_nfl", price="0.55")},
        {"received_at": NOW.isoformat(), "raw": _rfq_trade("rfq_nfl", price="0.58")},
    ]
    raw_path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    extras = load_trade_extras(raw_path)
    assert extras["rfq_nfl"]["price"] == "0.58"


def test_build_rows_filters_to_nfl_and_joins_trade(tmp_path):
    capture = _build_capture(tmp_path)
    capture.handle({"kind": "event", "raw": _rfq_request("rfq_nfl", ["100", "101"])}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_request("rfq_soccer", ["200", "201"], "0xsoccer")}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_trade("rfq_nfl", price="0.55", size="1500")}, NOW)
    capture.stop()

    rows = build_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["rfq_id"] == "rfq_nfl"
    assert row["traded_price"] == "0.55"
    assert row["traded_size"] == "1500"
    assert "Seahawks vs. Cardinals" in row["legs"][0]


def test_build_rows_date_filter_excludes_out_of_range(tmp_path):
    capture = _build_capture(tmp_path)
    capture.handle({"kind": "event", "raw": _rfq_request("rfq_nfl", ["100", "101"])}, NOW)
    capture.stop()

    later = datetime(2026, 9, 20, tzinfo=timezone.utc)
    rows = build_rows(tmp_path, since=later)
    assert rows == []

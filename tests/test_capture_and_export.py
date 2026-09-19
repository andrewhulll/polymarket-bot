"""scripts/capture_live_rfqs.py and scripts/export_nfl_rfqs.py, no network."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from capture_live_rfqs import RfqCapture  # noqa: E402
from export_nfl_rfqs import build_rows, load_trade_extras  # noqa: E402
from combo_mm.nfl.live_pricer import LiveQuote

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


def _attach_quoter(capture, *, status="QUOTED"):
    class Quoter:
        def submit(self, rfq):
            quote = LiveQuote(rfq_id=rfq.rfq_id, priced_at=NOW.isoformat(),
                              status=status, reason_code="QUOTED_OK" if status == "QUOTED"
                              else "UNSUPPORTED_LEG", fair=0.5, bid=0.48, ask=0.52,
                              bid_qty="10", ask_qty="10")
            capture._record_decision(rfq, quote, NOW, NOW)
            return True

        def stop(self):
            pass

    capture.quoter = Quoter()


def test_handle_stores_only_quoted_rfqs(tmp_path, monkeypatch):
    from combo_mm.inventory import InventoryProvider

    def fail_inventory_rebuild(*args, **kwargs):
        raise AssertionError("live capture must not rebuild inventory on RFQ close")

    monkeypatch.setattr(InventoryProvider, "record", fail_inventory_rebuild)
    capture = _build_capture(tmp_path)
    _attach_quoter(capture)
    capture.handle({"kind": "event", "raw": _rfq_request("rfq_nfl", ["100", "101"])}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_request("rfq_soccer", ["200", "201"], "0xsoccer")}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_trade("rfq_nfl")}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_trade("rfq_soccer")}, NOW)

    assert capture.rfqs_seen == 2
    assert capture.nfl_rfqs_seen == 1
    assert capture.trades_seen == 2

    # Only the quoted RFQ's request and trade are kept.
    raw_lines = capture.raw_path.read_text().splitlines()
    assert len(raw_lines) == 2
    frames = [json.loads(line) for line in raw_lines]
    assert frames[0]["raw"]["rfq_id"] == "rfq_nfl"
    assert frames[0]["raw"]["event_type"] == "rfq_created"
    assert frames[1]["raw"]["rfq_id"] == "rfq_nfl"
    assert frames[1]["raw"]["event_type"] == "rfq_closed"

    assert capture.store.get_rfq("rfq_soccer") is None
    assert capture.store.get_rfq("rfq_nfl") is not None
    nfl_screen = capture.store.get_rfq_screen("rfq_nfl")
    assert nfl_screen["n_nfl_legs"] == 2
    assert capture.store.get_rfq_screen("rfq_soccer") is None
    assert capture.store.count_raw_events() == 2
    session = capture.transient_rfqs.page(limit=10)
    assert [row["rfq_id"] for row in session["rows"]] == ["rfq_soccer"]
    assert session["rows"][0]["screen"] == "OTHER_SAME_GAME"
    capture.stop()


def test_handle_ignores_book_items(tmp_path):
    capture = _build_capture(tmp_path)
    capture.handle({"kind": "book", "symbol": "100", "bid": 0.5, "ask": 0.51}, NOW)
    assert capture.rfqs_seen == 0
    assert capture.raw_path.read_text() == ""
    capture.stop()


def test_declined_and_unresolved_rfqs_are_not_stored(tmp_path):
    capture = _build_capture(tmp_path)
    _attach_quoter(capture, status="DECLINED")
    capture.handle({"kind": "event", "raw": _rfq_request("declined", ["100", "101"])}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_request("unknown", ["999", "998"])}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_trade("declined")}, NOW)
    assert capture.store.get_rfq("declined") is None
    assert capture.store.get_rfq("unknown") is None
    assert capture.store.count_raw_events() == 0
    assert capture.raw_path.read_text() == ""
    rows = capture.transient_rfqs.page(limit=10)["rows"]
    assert {row["rfq_id"] for row in rows} == {"declined", "unknown"}
    assert capture.transient_rfqs.detail("declined")["pricing"]["reason_code"] == "UNSUPPORTED_LEG"
    assert capture.transient_rfqs.detail("unknown")["screen"]["screen"] == "UNRESOLVED"
    capture.stop()


def test_risk_rejection_stays_in_session_memory(tmp_path):
    capture = _build_capture(tmp_path)
    _attach_quoter(capture)
    # Screening accepts one unrelated leg, but live inventory requires one game.
    capture.handle({"kind": "event", "raw": _rfq_request(
        "risk_rejected", ["100", "101", "200"])}, NOW)
    assert capture.store.get_rfq("risk_rejected") is None
    assert capture.store.count_raw_events() == 0
    assert capture.raw_path.read_text() == ""
    with capture.store._lock:
        assert capture.store._conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] == 0
    detail = capture.transient_rfqs.detail("risk_rejected")
    assert detail["pricing"]["reason_code"] == "RISK_GAME_UNRESOLVED"
    capture.stop()


def test_trade_arriving_while_pricing_is_saved_only_after_quote(tmp_path):
    capture = _build_capture(tmp_path)

    class DeferredQuoter:
        def submit(self, rfq):
            self.rfq = rfq
            return True

        def stop(self):
            pass

    capture.quoter = DeferredQuoter()
    capture.handle({"kind": "event", "raw": _rfq_request("early", ["100", "101"])}, NOW)
    capture.handle({"kind": "event", "raw": _rfq_trade("early")}, NOW)
    assert capture.store.get_rfq("early") is None
    quote = LiveQuote(rfq_id="early", priced_at=NOW.isoformat(), status="QUOTED",
                      reason_code="QUOTED_OK", fair=0.5, bid=0.48, ask=0.52,
                      bid_qty="10", ask_qty="10")
    capture._record_decision(capture.quoter.rfq, quote, NOW, NOW)
    assert capture.store.get_rfq("early")["status"] == "CLOSED"
    assert len(capture.raw_path.read_text().splitlines()) == 2
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
    _attach_quoter(capture)
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
    _attach_quoter(capture)
    capture.handle({"kind": "event", "raw": _rfq_request("rfq_nfl", ["100", "101"])}, NOW)
    capture.stop()

    later = datetime(2026, 9, 20, tzinfo=timezone.utc)
    rows = build_rows(tmp_path, since=later)
    assert rows == []

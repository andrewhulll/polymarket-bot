"""Regression tests for defects found in the PR #7 review.

Each test pins one concrete failure: before the fix it failed (or silently
produced wrong state); after the fix it passes. All offline.
"""
from datetime import datetime, timezone

import pytest

from combo_mm import (
    EventStore,
    LegMarkInput,
    PollingConsumer,
    RetailPollingSource,
    normalize,
    price_combo,
)
from combo_mm.consumer import _dispatch_source_item
from combo_mm.dropcopy import SimulatedDropCopyTransport, drain_drop_copy
from combo_mm.normalize import NormalizeError

from tests.test_retail import FakeClient, _rfq as _retail_rfq

NOW = datetime(2026, 9, 16, 14, 0, 0, tzinfo=timezone.utc)


def _rfq_created(rid="R1", t="2026-01-01T00:00:00Z"):
    return {"event_type": "rfq_created", "rfq_id": rid, "exchange_ts": t,
            "payload": {"id": rid, "symbol": "S", "qtyDecimal": "10",
                        "status": "OPEN", "updatedTime": t,
                        "comboLegs": [{"symbol": "A", "side": "YES"}]}}


def _quote(etype, t, **payload):
    return normalize({"event_type": etype, "rfq_id": "R1", "quote_id": "Q1",
                      "exchange_ts": t,
                      "payload": {"creatorRfqUserId": "maker", **payload}})


def _store_with_active_quote():
    store = EventStore()
    store.apply(normalize(_rfq_created()))
    store.apply(_quote("quote_created", "2026-01-01T00:00:01Z",
                       buyPrice=0.5, sellPrice=0.4))
    return store


# -- store projections -------------------------------------------------------
def test_quote_deleted_moves_quote_to_deleted():
    store = _store_with_active_quote()
    store.apply(_quote("quote_deleted", "2026-01-01T00:00:02Z"))
    assert store.get_quote("Q1")["status"] == "DELETED"
    # Quote-level terminal only: the RFQ itself is not closed by it.
    assert store.get_rfq("R1")["status"] == "QUOTED"


def test_partial_lifecycle_payload_keeps_existing_quote_fields():
    store = _store_with_active_quote()
    store.apply(_quote("quote_accepted", "2026-01-01T00:00:02Z",
                       acceptedSide="BUY",
                       confirmationDeadline="2026-01-01T00:00:05Z"))
    store.apply(_quote("quote_confirmed", "2026-01-01T00:00:03Z",
                       executionDeadline="2026-01-01T00:00:09Z"))
    q = store.get_quote("Q1")
    assert q["status"] == "CONFIRMED"
    assert q["accepted_side"] == "BUY"
    assert q["confirmation_deadline"] == "2026-01-01T00:00:05Z"
    assert q["buy_price"] == 0.5


def test_sweeper_compares_parsed_deadlines_not_strings():
    store = _store_with_active_quote()
    store.apply(_quote("quote_accepted", "2026-01-01T00:00:02Z",
                       confirmationDeadline="2026-01-01T00:00:05Z"))
    # 500ms past the deadline; as strings "...05Z" > "...05.500000Z".
    now = datetime(2026, 1, 1, 0, 0, 5, 500000, tzinfo=timezone.utc)
    expired = store.sweep_confirmation_deadlines(now, "maker")
    assert expired == [{"rfq_id": "R1", "quote_id": "Q1"}]
    assert store.get_quote("Q1")["status"] == "EXPIRED"


def test_rfq_stats_are_plain_per_status_counts():
    store = EventStore()
    store.apply(normalize(_rfq_created("R1")))
    store.apply(normalize(_rfq_created("R2")))
    store.apply(normalize({"event_type": "rfq_cancelled", "rfq_id": "R2",
                           "exchange_ts": "2026-01-01T00:00:01Z"}))
    assert store.get_rfq_stats() == {"OPEN": 1, "CANCELLED": 1}


def test_state_digest_reflects_row_contents_not_just_counts():
    s1, s2 = _store_with_active_quote(), EventStore()
    s2.apply(normalize(_rfq_created()))
    s2.apply(_quote("quote_created", "2026-01-01T00:00:01Z",
                    buyPrice=0.9, sellPrice=0.1))  # same statuses, other prices
    assert s1.get_quote_stats() == s2.get_quote_stats()
    assert s1.state_digest() != s2.state_digest()


# -- normalize / dedup -------------------------------------------------------
def test_quote_event_without_rfq_id_is_rejected_not_misattributed():
    with pytest.raises(NormalizeError):
        normalize({"event_type": "quote_created",
                   "exchange_ts": "2026-01-01T00:00:01Z",
                   "payload": {"id": "QUOTE-9", "creatorRfqUserId": "m"}})


def test_new_event_after_restart_is_not_dropped_as_duplicate(tmp_path):
    db = str(tmp_path / "pipeline.db")
    s1 = EventStore(db)
    _dispatch_source_item(s1, {"kind": "event", "raw": _rfq_created("A1")},
                          on_item=None, now=NOW, source="stream")
    s1.close()
    s2 = EventStore(db)  # process restart: counters start over
    status = _dispatch_source_item(
        s2, {"kind": "event", "raw": _rfq_created("B2", "2026-01-02T00:00:00Z")},
        on_item=None, now=NOW, source="stream")
    assert status == "applied"
    assert s2.get_rfq("B2") is not None


def test_redelivered_event_without_event_id_is_deduplicated():
    store = EventStore()
    item = {"kind": "event", "raw": _rfq_created()}
    assert _dispatch_source_item(store, item, on_item=None, now=NOW,
                                 source="stream") == "applied"
    assert _dispatch_source_item(store, item, on_item=None, now=NOW,
                                 source="stream") == "duplicate"


def test_drop_copy_records_without_tokens_do_not_collapse():
    store = EventStore()
    records = [
        {"rfqId": "R1", "quoteId": "Q1", "symbol": "S", "side": "BUY",
         "price": 0.5, "qty": "5", "fillId": f"F{i}",
         "executedTime": "2026-01-01T00:00:10Z"}
        for i in (1, 2)
    ]
    drain_drop_copy(SimulatedDropCopyTransport(records), store, now=NOW)
    assert store.get_fill_stats()["fills"] == 2
    assert store.count_raw_events() == 2


# -- retail ------------------------------------------------------------------
def test_retail_disappearance_actually_closes_rfq_in_store():
    store = EventStore()
    src = RetailPollingSource(client=FakeClient([_retail_rfq("F")]))
    consumer = PollingConsumer(src, store)
    consumer.poll_once(NOW)
    assert store.get_rfq("F")["status"] == "OPEN"
    src._rfqs._listings = []
    consumer.poll_once(NOW)
    assert store.get_rfq("F")["status"] == "CLOSED"


def test_retail_status_change_without_timestamp_bump_reaches_store():
    store = EventStore()
    src = RetailPollingSource(client=FakeClient([_retail_rfq("G")]))
    consumer = PollingConsumer(src, store)
    consumer.poll_once(NOW)
    src._rfqs._listings = [_retail_rfq("G", status="CANCELLED")]  # same updatedTime
    consumer.poll_once(NOW.replace(second=30))
    assert store.get_rfq("G")["status"] == "CLOSED"


# -- pricing -----------------------------------------------------------------
def test_offer_is_never_clamped_below_fair():
    d = price_combo([LegMarkInput("A", "YES", resolved_price=1.0)],
                    rfq_id="x", qty_decimal="10")
    # fair == 1.0: an offer at price_max (0.999) would sell below value.
    assert d.buy_price == 0.0 or d.buy_price >= d.fair


def test_zero_bid_leg_does_not_explode_spread():
    d = price_combo([LegMarkInput("A", "YES", bid=0.0, ask=0.02,
                                  bid_size=5, ask_size=5)],
                    rfq_id="x", qty_decimal="10")
    assert d.reason_code != "QUOTED_OK" or d.buy_price < 0.999
    if d.components.get("model_uncertainty_bps") is not None:
        assert d.components["model_uncertainty_bps"] < 1e6

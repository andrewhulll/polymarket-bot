"""Fault injection: missing legs, stale books, out-of-order/duplicates,
unknown writes, malformed input."""
from datetime import datetime, timezone

import pytest

from combo_mm import EventStore, LegBookCache, fixtures, normalize
from combo_mm.normalize import NormalizeError
from combo_mm.pricing import (
    CROSSED_BOOK,
    MISSING_LEG,
    STALE_LEG,
    LegMarkInput,
    price_combo,
)
from combo_mm.replay import replay_session
from combo_mm.config import PipelineConfig

NOW = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)


def _raw(event_id, event_type, rfq_id, t, **kw):
    raw = {
        "event_id": event_id,
        "event_type": event_type,
        "rfq_id": rfq_id,
        "exchange_ts": fixtures._ts(t),
    }
    raw.update(kw)
    return raw


def test_missing_legs_allowed_and_pricer_declines():
    store = EventStore(":memory:")
    raw = _raw("m1", "rfq_created", "RM", 100, symbol="POTUS-2028",
               payload={"qtyDecimal": "5", "comboLegs": []})
    assert store.apply(normalize(raw, now=NOW)) is True
    assert store.get_rfq("RM")["legs"] == []

    decision = price_combo([], rfq_id="RM", qty_decimal="5", decided_at="x")
    assert decision.reason_code == MISSING_LEG
    assert not decision.quoted


def test_stale_book_decline_and_crossed_book_decline():
    stale_leg = LegMarkInput(symbol="L", side="YES", bid=0.5, ask=0.52,
                             bid_size=10.0, ask_size=10.0, stale=True)
    d = price_combo([stale_leg], rfq_id="R", qty_decimal="5")
    assert d.reason_code == STALE_LEG

    crossed = LegMarkInput(symbol="L", side="YES", bid=0.53, ask=0.52,
                           bid_size=10.0, ask_size=10.0)
    d = price_combo([crossed], rfq_id="R", qty_decimal="5")
    assert d.reason_code == CROSSED_BOOK

    missing = LegMarkInput(symbol="L", side="YES", bid=None, ask=0.52)
    d = price_combo([missing], rfq_id="R", qty_decimal="5")
    assert d.reason_code == MISSING_LEG


def test_out_of_order_quote_accept_without_quote_row_ignored():
    store = EventStore(":memory:")
    created = _raw("o1", "rfq_created", "RO", 100, symbol="POTUS-2028",
                   payload={"qtyDecimal": "5",
                            "comboLegs": [{"symbol": "L", "side": "YES"}]})
    store.apply(normalize(created, now=NOW))
    # accept arrives with no quote row: quote projection rejected...
    accept = _raw("o2", "quote_accepted", "RO", 150,
                  payload={"creatorRfqUserId": "m1"})
    store.apply(normalize(accept, now=NOW))
    assert store.get_quote("m1:RO") is None
    # ...but the RFQ row may still advance monotonically (no regression).
    assert store.get_rfq("RO")["status"] == "ACCEPTED"


def test_unknown_event_type_and_missing_rfq_id():
    with pytest.raises(NormalizeError):
        normalize({"event_type": "bogus", "exchange_ts": fixtures._ts(1)})
    with pytest.raises(NormalizeError):
        normalize({"event_type": "rfq_created",
                   "exchange_ts": fixtures._ts(1),
                   "payload": {"qtyDecimal": "1"}})


def test_fill_for_unknown_rfq_still_recorded():
    """Drop-copy fills are canonical: recorded even if we never saw the RFQ."""
    from combo_mm.fills import FillsLedger

    store = EventStore(":memory:")
    ledger = FillsLedger(store)
    raw = _raw("f1", "drop_copy_fill", "RGHOST", 100, symbol="POTUS-2028",
               payload={"side": "SELL", "price": 0.4, "qty": "7",
                        "drop_copy_seq": "dc-x"})
    assert store.apply(normalize(raw, now=NOW)) is True
    pos = ledger.get_position("POTUS-2028")
    assert float(pos["net_qty"]) == -7
    # Duplicate redelivery does not double-count.
    assert store.apply(normalize(raw, now=NOW)) is False
    assert float(ledger.get_position("POTUS-2028")["net_qty"]) == -7


def test_stale_books_flagged_not_blocking_pipeline():
    books = LegBookCache(staleness_ms=2000)
    books.update("L", 0.5, 0.52, updated_at="2026-09-16T13:00:00Z", seq=1)
    snap = books.get(["L"], now_ms=1_789_567_200_000)["L"]
    assert snap.stale  # flagged...
    # ...but the pipeline keeps flowing around it (no exception anywhere).
    store = EventStore(":memory:")
    session, combos = fixtures.build_session()
    out = replay_session(session, combos, store, PipelineConfig(),
                         enable_shadow=False)
    assert out["events"] > 0

"""PollingConsumer + SimulatedEventSource: dispatch parity with the stream path."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from combo_mm import EventStore, PollingConsumer, SimulatedEventSource
from combo_mm.fixtures import BASE_TS
from combo_mm.stream import StreamDisconnected

T0 = "2026-09-16T14:00:00Z"


def _book(t, symbol, bid=0.50, ask=0.52):
    return {"t": t, "kind": "book", "symbol": symbol, "bid": bid, "ask": ask,
            "bid_size": 100.0, "ask_size": 100.0, "seq": 0, "ts": T0}


def _created(t, rid):
    return {"t": t, "kind": "event", "raw": {
        "event_type": "rfq_created", "rfq_id": rid, "symbol": "SYM-1",
        "exchange_ts": T0,
        "payload": {
            "id": rid, "symbol": "SYM-1", "status": "OPEN",
            "qtyDecimal": "10",
            "comboLegs": [{"symbol": "LEG-A", "side": "YES"}],
        }}}


def _closed(t, rid):
    return {"t": t, "kind": "event", "raw": {
        "event_type": "rfq_closed", "rfq_id": rid, "symbol": "SYM-1",
        "exchange_ts": "2026-09-16T14:00:02Z",
        "payload": {"id": rid, "status": "CANCELLED",
                    "updatedTime": "2026-09-16T14:00:02Z"}}}


def _at(ms):
    return BASE_TS + timedelta(milliseconds=ms)


def test_simulated_source_releases_only_due_items_in_order():
    src = SimulatedEventSource([_book(2000, "LEG-A"), _book(0, "LEG-B"),
                                _created(1000, "R-1")])
    assert [i["kind"] for i in src.poll(_at(0))] == ["book"]
    assert [i["t"] for i in src.poll(_at(1500))] == [1000]
    assert [i["t"] for i in src.poll(_at(5000))] == [2000]
    assert src.exhausted
    assert src.poll(_at(6000)) == []


def test_simulated_source_skips_invisible_items():
    hidden = dict(_book(0, "LEG-A"), stream=False)
    src = SimulatedEventSource([hidden, _book(0, "LEG-B")])
    items = src.poll(_at(0))
    assert [i["symbol"] for i in items] == ["LEG-B"]


def test_simulated_source_disconnect_raises():
    src = SimulatedEventSource([_book(0, "LEG-A"),
                                {"t": 500, "kind": "disconnect"},
                                _book(1000, "LEG-B")])
    assert len(src.poll(_at(0))) == 1
    with pytest.raises(StreamDisconnected):
        src.poll(_at(1000))


def test_polling_consumer_applies_events_and_books():
    store = EventStore(":memory:")
    src = SimulatedEventSource([_book(0, "LEG-A"), _created(1000, "R-1"),
                                _closed(2000, "R-1")])
    consumer = PollingConsumer(src, store, source_label="sim-test")
    assert consumer.poll_once(_at(0)) == 1
    assert consumer.poll_once(_at(1500)) == 1
    assert consumer.poll_once(_at(5000)) == 1
    assert consumer.items_seen == 3
    assert consumer.items_applied == 2  # book snapshots don't count
    assert consumer.polls == 3
    rfqs = {r["rfq_id"]: r for r in store.list_rfqs()}
    # rfq_closed is the only public close event: it normalizes to CLOSED
    # (there is no cancel event on the real stream).
    assert rfqs["R-1"]["status"] == "CLOSED"
    sources = {r["source"] for r in
               store._conn.execute("SELECT DISTINCT source FROM raw_events")}
    assert sources == {"sim-test"}


def test_polling_consumer_propagates_disconnect():
    store = EventStore(":memory:")
    src = SimulatedEventSource([{"t": 0, "kind": "disconnect"}])
    consumer = PollingConsumer(src, store)
    with pytest.raises(StreamDisconnected):
        consumer.poll_once(_at(0))
    assert consumer.polls == 0  # failed poll doesn't count


def test_polling_consumer_thread_runs_and_stops_cleanly():
    store = EventStore(":memory:")
    src = SimulatedEventSource([_book(0, "LEG-A")])
    consumer = PollingConsumer(src, store, poll_interval_s=0.05)
    consumer.start()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        while consumer.polls < 2 and datetime.now(timezone.utc) < deadline:
            time.sleep(0.01)
        assert consumer.polls >= 2
    finally:
        consumer.stop()
    assert consumer.items_applied == 0  # book snapshots aren't events


def test_polling_consumer_thread_backs_off_on_source_errors():
    class Flaky:
        def __init__(self):
            self.calls = 0

        def poll(self, now):
            self.calls += 1
            raise StreamDisconnected("boom")

    consumer = PollingConsumer(Flaky(), EventStore(":memory:"),
                               poll_interval_s=0.01,
                               backoff_initial_s=0.01, backoff_max_s=0.02)
    consumer.start()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        while consumer._thread.is_alive() and \
                consumer._source.calls < 3 and \
                datetime.now(timezone.utc) < deadline:
            time.sleep(0.01)
        assert consumer._source.calls >= 3, "expected retries with backoff"
    finally:
        consumer.stop()


def test_polling_consumer_rejects_bad_interval():
    with pytest.raises(ValueError):
        PollingConsumer(SimulatedEventSource([]), EventStore(":memory:"),
                        poll_interval_s=0)

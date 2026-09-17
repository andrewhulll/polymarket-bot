"""LiveMonitor: poll -> store -> shadow engine, failures recorded not raised."""
from datetime import datetime, timezone

from combo_mm.live_monitor import LiveMonitor
from combo_mm.sources import EventSource
from combo_mm.store import EventStore

NOW = datetime(2026, 9, 20, 16, 0, 0, tzinfo=timezone.utc)
TS = "2026-09-20T16:00:00Z"


class FakeSource(EventSource):
    def __init__(self, batches, beta=True):
        self.batches = list(batches)
        self.rfq_beta_enabled = beta
        self.last_error = None

    def poll(self, now):
        batch = self.batches.pop(0) if self.batches else []
        if isinstance(batch, Exception):
            raise batch
        return batch


def _book(symbol, bid, ask):
    return {"kind": "book", "symbol": symbol, "bid": bid, "ask": ask,
            "bid_size": 500.0, "ask_size": 500.0, "seq": 1, "ts": TS}


def _rfq(rfq_id):
    return {"kind": "event", "raw": {
        "event_type": "rfq_created", "rfq_id": rfq_id, "exchange_ts": TS,
        "payload": {"id": rfq_id, "symbol": "KC-COMBO", "rfqCreatorUserId": "u1",
                    "createdTime": TS, "updatedTime": TS, "status": "OPEN",
                    "qtyDecimal": "20",
                    "comboLegs": [{"symbol": "KC-ML", "side": "YES"},
                                  {"symbol": "KC-OVER", "side": "YES"}]}}}


def test_poll_applies_books_and_rfqs_and_quotes():
    store = EventStore()
    monitor = LiveMonitor(FakeSource([[_book("KC-ML", 0.60, 0.62), _book("KC-OVER", 0.49, 0.51),
                                       _rfq("R1")]]), store)
    assert monitor.poll_once(NOW) == 3
    assert (monitor.polls, monitor.books_seen, monitor.events_applied) == (1, 2, 1)
    assert store.get_rfq("R1")["status"] == "OPEN"
    decisions = store.get_shadow_decisions()
    assert [d["decision"] for d in decisions] == ["QUOTED_OK"]
    assert len(store.get_shadow_quotes()) == 1


def test_books_later_in_the_same_poll_are_applied_before_pricing():
    """Retail emits a new RFQ's leg books after its events in the same poll."""
    store = EventStore()
    monitor = LiveMonitor(FakeSource([[_rfq("R1"), _book("KC-ML", 0.60, 0.62),
                                       _book("KC-OVER", 0.49, 0.51)]]), store)
    monitor.poll_once(NOW)
    assert [d["decision"] for d in store.get_shadow_decisions()] == ["QUOTED_OK"]


def test_duplicate_rfq_is_not_requoted():
    store = EventStore()
    rfq = _rfq("R1")
    rfq["raw"]["event_id"] = "e1"
    books = [_book("KC-ML", 0.60, 0.62), _book("KC-OVER", 0.49, 0.51)]
    monitor = LiveMonitor(FakeSource([books + [rfq], [rfq]]), store)
    monitor.poll_once(NOW)
    monitor.poll_once(NOW)
    assert monitor.events_applied == 1
    assert len(store.get_shadow_decisions()) == 1


def test_source_failure_is_recorded_not_raised():
    class Boom(RuntimeError):
        pass

    monitor = LiveMonitor(FakeSource([Boom("secret-ish detail"), []], beta=False), EventStore())
    assert monitor.poll_once(NOW) == 0
    assert monitor.poll_errors == 1 and monitor.last_error == "Boom"
    assert monitor.rfq_beta_enabled is False
    monitor.poll_once(NOW)
    assert monitor.polls == 1 and monitor.last_error is None

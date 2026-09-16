"""Consumer lifecycle tests against the deterministic fixture stream.

All tests are offline: they use the simulated fixture transport and either
the synchronous StreamConsumer.run() drain or the threaded start()/stop()
pair. Nothing here touches the network.
"""
from __future__ import annotations

import random
import time

from combo_mm import (
    ConsumerConfig,
    EventStore,
    PipelineConfig,
    SimulatedTransport,
    fixtures,
    recovery_sync,
)
from combo_mm.consumer import StreamConsumer
from combo_mm.dropcopy import SimulatedDropCopyTransport, drain_drop_copy
from combo_mm.stream import StreamDisconnected


def _cfg(**kw) -> ConsumerConfig:
    """Fast, deterministic consumer config for tests."""
    return ConsumerConfig(
        max_reconnects=kw.get("max_reconnects", 3),
        backoff_initial_ms=1,
        backoff_max_ms=5,
        min_reconnect_interval_s=0.0,  # no pacing in tests
        watchdog_silence_s=kw.get("watchdog_silence_s", 2.0),
    )


def _consumer(transport, store, enable_shadow=False, **kw) -> StreamConsumer:
    return StreamConsumer(transport, store, _cfg(**kw),
                          rng=random.Random(7), enable_shadow=enable_shadow)


def _clean_consumer(**kw):
    session, combos = fixtures.build_session()
    transport = SimulatedTransport(session, fixtures.SELF_USER_ID, combos)
    store = EventStore(":memory:")
    consumer = _consumer(transport, store, **kw)
    return consumer, transport, store, session, combos


def _visible_events(session):
    return [it for it in session
            if it.get("kind") == "event" and it.get("stream", True)]


def test_clean_run_needs_no_reconnect_and_recovers_durable_state():
    consumer, transport, store, session, combos = _clean_consumer()
    stats = consumer.run()
    # The fixture scripts one mid-stream disconnect at t=2200: exactly one
    # reconnect, and recovery runs on EVERY (re)connect.
    assert stats["reconnects"] == 1
    assert stats["recoveries"] == 2
    assert len(consumer.recovery_log) == 2
    # Initial recovery: one report, inserting the durable snapshot:
    # 7 non-terminal RFQs from GetRFQs(open) (RFQ-002/003/004/007 are
    # terminal in the durable fold) + 5 own quotes from GetQuotes(self).
    report = consumer.recovery_log[0]
    assert report.added == 12
    assert report.ignored == 0
    assert report.refreshed == 0
    assert report.marked_closed == 0  # nothing missed while we were away
    # Every stream-visible event was applied exactly once; deterministic.
    # (The fixture deliberately redelivers evt-019/evt-020 to model
    # at-least-once delivery: the store dedupes them by event id.)
    n_visible = len(_visible_events(session))
    assert stats["seen"] == n_visible
    assert stats["applied"] == n_visible - 2
    assert stats["duplicates"] == 2
    # The durable record covers all fixture RFQs...
    assert sum(store.get_rfq_stats().values()) == 11
    # ...but RFQ-007's close is stream-invisible: created at t=2300 (after
    # the scripted disconnect), its t=2400 close never arrives on the
    # stream, so the run leaves it non-terminal...
    assert store.get_rfq("RFQ-007")["status"] == "QUOTED"
    # ...until the next recovery converges it via absence inference.
    report3 = recovery_sync(transport, store,
                            self_user_id=fixtures.SELF_USER_ID)
    assert report3.marked_closed == 1
    assert store.get_rfq("RFQ-007")["status"] == "CLOSED"
    # A further recovery is a no-op: already converged.
    report4 = recovery_sync(transport, store,
                            self_user_id=fixtures.SELF_USER_ID)
    assert report4.added == 0
    assert report4.marked_closed == 0


def test_run_is_deterministic_across_runs():
    consumer1, _, store1, _, _ = _clean_consumer()
    consumer2, _, store2, _, _ = _clean_consumer()
    stats1 = consumer1.run()
    stats2 = consumer2.run()
    assert stats1 == stats2
    assert store1.state_digest() == store2.state_digest()
    assert len(store1.state_digest()) == 64


def test_pipeline_config_is_coerced_to_consumer_config():
    """PipelineConfig (the design-doc config surface) is accepted too."""
    session, combos = fixtures.build_session()
    transport = SimulatedTransport(session, fixtures.SELF_USER_ID, combos)
    store = EventStore(":memory:")
    consumer = StreamConsumer(transport, store, PipelineConfig(),
                              rng=random.Random(7))
    stats = consumer.run()
    # The fixture's scripted disconnect fires here too: 1 reconnect.
    assert stats["reconnects"] == 1
    assert stats["recoveries"] == 2


def test_shadow_quotes_paper_only_no_live_submission():
    consumer, transport, store, session, combos = _clean_consumer(
        enable_shadow=True)
    stats = consumer.run()
    assert stats["shadow_decisions"] > 0
    assert sum(store.get_shadow_stats().values()) > 0
    # Paper only: CreateQuote was never invoked on the transport.
    assert transport.create_quote_calls == []


def test_drop_copy_drain_after_run_records_fill_exactly_once():
    consumer, transport, store, session, combos = _clean_consumer()
    stats = consumer.run()
    assert stats["seen"] > 0
    # Fills reconcile exclusively through Drop Copy (as in run_pipeline).
    # drain_drop_copy returns the resume token; the fill lands in the ledger.
    token = drain_drop_copy(
        SimulatedDropCopyTransport(fixtures.build_drop_copy_feed()),
        store, now=fixtures.BASE_TS)
    assert token == "dc-0002"
    rows = list(store._conn.execute("SELECT fill_id FROM fills"))
    assert [r["fill_id"] for r in rows] == ["fill-0001"]
    # A second drain of the same feed is a no-op (token resume).
    token2 = drain_drop_copy(
        SimulatedDropCopyTransport(fixtures.build_drop_copy_feed()),
        store, now=fixtures.BASE_TS)
    assert token2 == token
    rows2 = list(store._conn.execute("SELECT fill_id FROM fills"))
    assert [r["fill_id"] for r in rows2] == ["fill-0001"]


class _ResumingFlakyTransport:
    """Yields the stream once, disconnecting twice mid-way, then drains.

    Resumes after the last consumed position, like the real exchange:
    a reconnect delivers only new events.
    """

    def __init__(self, items, drops=2):
        self._items = items
        self._pos = 0
        n = len(items)
        # Drop mid-stream this many times, at spread-out positions.
        self._drop_at = {n // 3, 2 * n // 3} if drops >= 2 else {n // 2}
        self.opens = 0

    def stream_rfq_events(self):
        self.opens += 1
        while self._pos < len(self._items):
            item = self._items[self._pos]
            self._pos += 1
            yield item
            if self._pos in self._drop_at:
                self._drop_at.discard(self._pos)
                raise StreamDisconnected("simulated drop")

    def get_rfq_user_id(self):
        return "self-test"

    def get_rfqs(self, status=None):
        return []

    def get_quotes(self, user_filter=None):
        return []

    def get_drop_copy(self, since_token=None):
        return {"records": [], "next_token": 0}


def test_reconnect_resumes_after_drops_without_duplicates():
    session, _ = fixtures.build_session()
    visible = _visible_events(session)
    transport = _ResumingFlakyTransport(visible, drops=2)
    store = EventStore(":memory:")
    consumer = StreamConsumer(transport, store, _cfg(max_reconnects=5),
                              rng=random.Random(7))
    stats = consumer.run()
    assert stats["reconnects"] == 2  # two drops, then the clean drain
    assert stats["recoveries"] == 3  # recovery ran on EVERY (re)connect
    assert len(consumer.recovery_log) == 3
    # Resumed delivery: every event seen exactly once; the fixture's two
    # deliberate redeliveries (evt-019/evt-020) dedupe by event id.
    assert stats["seen"] == len(visible)
    assert stats["applied"] == len(visible) - 2
    assert stats["duplicates"] == 2


def test_max_reconnects_bounds_retries():
    """A stream that always drops: the bound stops the retry loop."""

    class _AlwaysDrops(_HangingTransport):
        def stream_rfq_events(self):
            raise StreamDisconnected("always down")
            yield  # pragma: no cover - makes this a generator

    store = EventStore(":memory:")
    consumer = StreamConsumer(_AlwaysDrops(), store, _cfg(max_reconnects=1),
                              rng=random.Random(7))
    stats = consumer.run()
    # Bounded: the always-dropping stream does not retry forever.
    assert stats["reconnects"] == 1
    assert stats["recoveries"] == 1


class _HangingTransport:
    """stream_rfq_events() yields nothing: the watchdog must fire."""

    def stream_rfq_events(self):
        yield from ()  # an exhausted generator: silence, not items

    def get_rfq_user_id(self):
        return "self-test"

    def get_rfqs(self, status=None):
        return []

    def get_quotes(self, user_filter=None):
        return []

    def get_drop_copy(self, since_token=None):
        return {"records": [], "next_token": 0}


def test_watchdog_reconnects_on_silent_stream():
    consumer = StreamConsumer(_HangingTransport(), EventStore(":memory:"),
                              _cfg(max_reconnects=50, watchdog_silence_s=1.0),
                              rng=random.Random(7))
    consumer.start()
    try:
        deadline = time.time() + 15.0
        while consumer.reconnects < 2 and time.time() < deadline:
            time.sleep(0.2)
        assert consumer.reconnects >= 2  # watchdog kept cycling the stream
    finally:
        consumer.stop()
    assert consumer.state == "STOPPED"

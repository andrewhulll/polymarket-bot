"""recovery_sync(): missed rfq_closed, unseen RFQs, quote refresh -- idempotent."""
from datetime import datetime, timezone

from combo_mm import (
    EventStore,
    PipelineConfig,
    SimulatedTransport,
    fixtures,
    normalize,
    recovery_sync,
)
from combo_mm.replay import replay_session


def _harness():
    session, combos = fixtures.build_session()
    transport = SimulatedTransport(session, fixtures.SELF_USER_ID, combos)
    return session, combos, transport


def test_missed_close_marked_closed():
    session, combos, transport = _harness()
    store = EventStore(":memory:")
    cfg = PipelineConfig()
    # Stream-only replay: RFQ-007's close (stream=False) is missed.
    replay_session(session, combos, store, cfg, enable_shadow=False,
                   include_stream_invisible=False)
    rfq7 = store.get_rfq("RFQ-007")
    assert rfq7 is not None and rfq7["status"] == "QUOTED"

    summary = recovery_sync(transport, store, self_user_id=fixtures.SELF_USER_ID)
    assert summary.marked_closed >= 1
    assert store.get_rfq("RFQ-007")["status"] == "CLOSED"


def test_unseen_rfq_inserted():
    session, combos, transport = _harness()
    store = EventStore(":memory:")
    cfg = PipelineConfig()
    replay_session(session, combos, store, cfg, enable_shadow=False,
                   include_stream_invisible=False)
    # RFQ-008's create is stream-invisible, but its stream-visible update
    # bootstraps a partial row (no qty yet -- the update carries none).
    rfq8_partial = store.get_rfq("RFQ-008")
    assert rfq8_partial is not None
    assert rfq8_partial["qty_decimal"] is None

    # The durable read fills in RFQ-008's full create data (the stream had
    # only bootstrapped a partial row): counted as a refresh, not an insert.
    summary = recovery_sync(transport, store, self_user_id=fixtures.SELF_USER_ID)
    assert summary.refreshed >= 1
    rfq8 = store.get_rfq("RFQ-008")
    assert rfq8 is not None and rfq8["status"] == "OPEN"
    assert rfq8["qty_decimal"] == 30.0


def test_recovery_is_idempotent():
    session, combos, transport = _harness()
    store = EventStore(":memory:")
    cfg = PipelineConfig()
    replay_session(session, combos, store, cfg, enable_shadow=False,
                   include_stream_invisible=False)
    kw = dict(self_user_id=fixtures.SELF_USER_ID)
    first = recovery_sync(transport, store, **kw)
    digest = store.state_digest()
    second = recovery_sync(transport, store, **kw)
    assert store.state_digest() == digest
    assert (second.added, second.marked_closed, second.refreshed) == (0, 0, 0)
    assert (first.added, first.marked_closed) != (0, 0)


def test_recovery_never_regresses_executed():
    session, combos, transport = _harness()
    store = EventStore(":memory:")
    cfg = PipelineConfig()
    replay_session(session, combos, store, cfg, enable_shadow=False,
                   include_stream_invisible=False)
    assert store.get_rfq("RFQ-001")["status"] == "EXECUTED"
    recovery_sync(transport, store, self_user_id=fixtures.SELF_USER_ID)
    # EXECUTED must not be "reconciled" down to CLOSED.
    assert store.get_rfq("RFQ-001")["status"] == "EXECUTED"

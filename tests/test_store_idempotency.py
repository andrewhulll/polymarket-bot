"""Exactly-once write path: raw-first ordering, duplicate suppression."""
from datetime import datetime, timezone

from combo_mm import EventStore, fixtures, normalize
from combo_mm.replay import replay_session
from combo_mm.config import PipelineConfig


def _store():
    return EventStore(":memory:")


def _raw(event_id, event_type, rfq_id, t=100, **kw):
    from combo_mm.fixtures import _ts  # test-only use of fixture helper
    raw = {
        "event_id": event_id,
        "event_type": event_type,
        "rfq_id": rfq_id,
        "exchange_ts": _ts(t),
    }
    raw.update(kw)
    return raw


def _created(event_id="e1", rfq_id="R1", t=100):
    return _raw(
        event_id, "rfq_created", rfq_id, t,
        symbol="POTUS-2028",
        payload={"qtyDecimal": "10", "creatorUserId": "u1",
                 "comboLegs": [{"symbol": "L1", "side": "YES"}]},
    )


def test_same_event_twice_single_row_no_state_change():
    store = _store()
    now = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)
    ev = normalize(_created(), now=now)
    assert store.apply(ev) is True
    digest1 = store.state_digest()
    assert store.apply(ev) is False  # duplicate -> idempotent no-op
    assert store.count_raw_events() == 1
    assert store.state_digest() == digest1
    assert len(store.list_rfqs()) == 1


def test_duplicate_event_id_different_payload_does_not_overwrite():
    """Exactly-once is keyed on event_id: a redelivery wins, a conflicting
    reuse of the same id is ignored (first write wins)."""
    store = _store()
    now = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)
    store.apply(normalize(_created("e1", "R1"), now=now))
    # Same event_id, different rfq_id: must NOT create a second RFQ.
    store.apply(normalize(_created("e1", "R2"), now=now))
    assert len(store.list_rfqs()) == 1
    assert store.get_rfq("R1") is not None
    assert store.get_rfq("R2") is None


def test_raw_inserted_before_projection():
    """A projection that no-ops (late event) still leaves the raw row behind,
    so the no-op is auditable."""
    store = _store()
    now = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)
    store.apply(normalize(_created("e1", "R1"), now=now))
    # Terminal first...
    store.apply(normalize(_raw("e2", "rfq_cancelled", "R1", t=200), now=now))
    # ...then a late quote_created that must not regress the RFQ.
    late = _raw(
        "e3", "quote_created", "R1", t=300,
        payload={"creatorRfqUserId": "m1", "buyPrice": 0.6, "sellPrice": 0.5,
                 "buyQtyDecimal": "10", "sellQtyDecimal": "10"},
    )
    assert store.apply(normalize(late, now=now)) is True  # raw row recorded
    rfq = store.get_rfq("R1")
    assert rfq["status"] == "CANCELLED"  # unchanged
    assert store.get_quote("m1:R1") is None  # race rule: no new quote created
    assert store.count_raw_events() == 3


def test_terminal_event_for_unknown_rfq_records_row():
    store = _store()
    now = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)
    store.apply(normalize(_raw("e9", "rfq_closed", "RX", t=100), now=now))
    rfq = store.get_rfq("RX")
    assert rfq is not None and rfq["status"] == "CLOSED"


def test_fill_idempotent_on_fill_id():
    from combo_mm.fills import FillsLedger

    store = _store()
    session, combos = fixtures.build_session()
    cfg = PipelineConfig()
    replay_session(session, combos, store, cfg, enable_shadow=False,
                   drain_drop_copy_records=fixtures.build_drop_copy_feed())
    fills = list(store._conn.execute("SELECT * FROM fills"))
    assert len(fills) == 1  # one fill row, despite the drop-copy redelivery
    pos = FillsLedger(store).get_position("POTUS-2028")
    assert pos is not None
    # Our fill is SELL 100 @ 0.57 (taker bought against our ask).
    assert float(pos["net_qty"]) == -100  # not double-counted
    assert pos["avg_price"] == 0.57


def _rfq_raw(rfq_id="R-1", event_id="e1"):
    return {
        "event_id": event_id,
        "event_type": "rfq_created",
        "rfq_id": rfq_id,
        "symbol": "POTUS-2028",
        "exchange_ts": "2026-09-16T14:00:00Z",
        "payload": {
            "qtyDecimal": "100",
            "creatorUserId": "u-taker",
            "comboLegs": [
                {"tokenId": "t1", "symbol": "POTUS-2028", "side": "YES", "ratio": "1"},
            ],
        },
    }


def _quote_raw(rfq_id, quote_id, etype, event_id,
               t="2026-09-16T14:01:00Z"):
    return {
        "event_id": event_id,
        "event_type": etype,
        "rfq_id": rfq_id,
        "quote_id": quote_id,
        "exchange_ts": t,
        "payload": {"creatorRfqUserId": "maker-001", "buyPrice": "0.31",
                    "sellPrice": "0.27"},
    }


def test_replaced_quote_draft_revised_returns_to_active():
    """The replacement exception: a revised draft on a REPLACED quote
    reactivates it (the maker is still in the game)."""
    store = EventStore(":memory:")
    assert store.apply(normalize(_rfq_raw()))
    assert store.apply(normalize(_quote_raw("R-1", "q-1", "quote_created",
                                            "e2")))
    assert store.get_quote("q-1")["status"] == "ACTIVE"
    # First revision supersedes the live quote...
    # (strictly newer exchange_ts: equal timestamps are idempotent no-ops)
    assert store.apply(normalize(_quote_raw("R-1", "q-1", "quote_draft_revised",
                                            "e3", t="2026-09-16T14:02:00Z")))
    assert store.get_quote("q-1")["status"] == "REPLACED"
    # ...and the replacement exception: a further revision on the REPLACED
    # quote moves it back to ACTIVE -- the maker is still in the game.
    assert store.apply(normalize(_quote_raw("R-1", "q-1", "quote_draft_revised",
                                            "e4", t="2026-09-16T14:03:00Z")))
    assert store.get_quote("q-1")["status"] == "ACTIVE"
    assert store.get_rfq("R-1")["status"] == "QUOTED"


def test_fill_idempotent_on_drop_copy_seq():
    """The same drop-copy sequence delivered under two different event ids
    records exactly one fill."""
    store = EventStore(":memory:")
    assert store.apply(normalize(_rfq_raw()))
    assert store.apply(normalize(_quote_raw("R-1", "q-1", "quote_created",
                                            "e2")))
    assert store.apply(normalize(_quote_raw("R-1", "q-1", "quote_confirmed",
                                            "e3", t="2026-09-16T14:02:00Z")))
    for eid in ("fill-a", "fill-b"):
        raw = {
            "event_id": eid,
            "event_type": "drop_copy_fill",
            "rfq_id": "R-1",
            "quote_id": "q-1",
            "symbol": "POTUS-2028",
            "exchange_ts": "2026-09-16T14:02:00Z",
            "payload": {
                "side": "BUY",
                "price": "0.30",
                "quantity": "100",
                "fillId": None,
                "dropCopySeq": "seq-99",
            },
        }
        assert store.apply(normalize(raw))
    fills = store._conn.execute("SELECT fill_id FROM fills").fetchall()
    assert len(fills) == 1
    assert fills[0]["fill_id"] == "seq-99"
    from combo_mm.fills import FillsLedger
    assert float(FillsLedger(store).get_position("POTUS-2028")["net_qty"]) == 100


def test_grpc_transport_is_unimplemented_stub():
    import pytest

    from combo_mm.stream import GrpcTransport

    t = GrpcTransport()
    with pytest.raises(NotImplementedError):
        t.get_rfq_user_id()
    with pytest.raises(NotImplementedError):
        t.get_rfqs()
    with pytest.raises(NotImplementedError):
        t.get_quotes()
    with pytest.raises(NotImplementedError):
        next(t.stream_rfq_events())


def test_unknown_config_fields_are_fatal():
    import pytest

    from combo_mm.config import PipelineConfig

    with pytest.raises(TypeError, match="unknown config fields"):
        PipelineConfig.from_dict({"paper_mode": True, "bogus_knob": 1})


def test_synchronous_mode_is_configurable_and_validated(tmp_path):
    import pytest

    def mode(store):
        return store._conn.execute("PRAGMA synchronous").fetchone()[0]

    assert mode(EventStore(str(tmp_path / "full.db"))) == 2          # FULL (default)
    assert mode(EventStore(str(tmp_path / "normal.db"), synchronous="NORMAL")) == 1
    with pytest.raises(ValueError):
        EventStore(str(tmp_path / "bad.db"), synchronous="OFF")

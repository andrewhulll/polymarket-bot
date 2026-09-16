"""Deterministic replay: same fixture -> fresh DBs -> byte-identical digests."""
from combo_mm import EventStore, PipelineConfig, fixtures
from combo_mm.replay import replay_session


def test_replay_determinism_byte_for_byte():
    session, combos = fixtures.build_session()
    cfg = PipelineConfig()
    digests = []
    for _ in range(2):
        store = EventStore(":memory:")
        out = replay_session(session, combos, store, cfg, enable_shadow=True)
        digests.append(out["digest"])
        assert out["events"] > 0
        assert out["decisions"] > 0
    assert digests[0] == digests[1]
    assert len(digests[0]) == 64


def test_replay_without_shadow_is_deterministic_too():
    session, combos = fixtures.build_session()
    cfg = PipelineConfig()
    digests = set()
    for _ in range(2):
        store = EventStore(":memory:")
        out = replay_session(session, combos, store, cfg, enable_shadow=False)
        digests.add(out["digest"])
    assert len(digests) == 1


def test_stream_only_vs_full_replay_differ_only_by_missed_events():
    session, combos = fixtures.build_session()
    cfg = PipelineConfig()
    s1, s2 = EventStore(":memory:"), EventStore(":memory:")
    replay_session(session, combos, s1, cfg, enable_shadow=False,
                   include_stream_invisible=False)
    replay_session(session, combos, s2, cfg, enable_shadow=False,
                   include_stream_invisible=True)
    # Full replay sees RFQ-007's close and RFQ-008's creation.
    assert s1.get_rfq("RFQ-007")["status"] == "QUOTED"
    assert s2.get_rfq("RFQ-007")["status"] == "CLOSED"
    # RFQ-008's stream-invisible create is dropped in s1, but its
    # stream-visible update still bootstraps a partial row there.
    assert s1.get_rfq("RFQ-008")["qty_decimal"] is None
    assert s2.get_rfq("RFQ-008")["qty_decimal"] == 30.0

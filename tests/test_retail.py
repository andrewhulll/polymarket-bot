"""RetailPollingSource: diffing, budgets, credential hygiene. All mocked.

No network, no real credentials. The fake client duck-types the
``polymarket-us`` SDK surface the source actually touches
(``.rfqs.list/.retrieve`` and ``.markets.bbo``); a separate fake SDK module
covers the env-var -> SDK-constructor path for the leakage test.
"""
import logging
import sys
import types
from datetime import datetime, timezone

import pytest

from combo_mm import EventStore, PipelineConfig, PollingConsumer, SimulatedEventSource
from combo_mm.auth import CredentialsNotConfigured
from combo_mm.retail import (
    KEY_ID_ENV,
    SECRET_ENV,
    RFQ_BETA_MESSAGE,
    RetailPollingSource,
)

NOW = datetime(2026, 9, 16, 14, 0, 0, tzinfo=timezone.utc)
T0 = "2026-09-16T14:00:00Z"
T1 = "2026-09-16T14:00:05Z"
T2 = "2026-09-16T14:00:10Z"

FAKE_KEY = "PM_TEST_KEY_ID_abc123"
FAKE_SECRET = "PM_TEST_SECRET_xyz789"  # fake; never leaves this process


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #
def _rfq(rid, status="OPEN", updated=T0, legs=True, qty="100"):
    entry = {"id": rid, "symbol": f"SYM-{rid}", "status": status,
             "updatedTime": updated, "qtyDecimal": qty}
    if legs:
        entry["legs"] = [{"symbol": f"LEG-{rid}-A", "side": "YES"},
                         {"symbol": f"LEG-{rid}-B", "side": "NO"}]
    return entry


def _bbo(bid=0.50, ask=0.52):
    return {"marketData": {
        "bestBid": {"value": str(bid), "currency": "USD"},
        "bestAsk": {"value": str(ask), "currency": "USD"},
        "bidDepth": "120", "askDepth": "130"}}


class FakeRFQs:
    def __init__(self, listings, details=None):
        self._listings = listings
        self._details = details or {}
        self.calls = []

    def list(self):
        self.calls.append("list")
        return {"rfqs": self._listings}

    def retrieve(self, rfq_id):
        self.calls.append(("retrieve", rfq_id))
        return self._details.get(rfq_id, {"id": rfq_id})


class FakeMarkets:
    def __init__(self, bbos=None):
        self._bbos = bbos or {}
        self.calls = []

    def bbo(self, slug):
        self.calls.append(slug)
        return self._bbos.get(slug, _bbo())


class FakeClient:
    """Duck-types the SDK surface RetailPollingSource touches."""

    def __init__(self, listings, details=None, bbos=None):
        self.rfqs = FakeRFQs(listings, details)
        self.markets = FakeMarkets(bbos)

    def get(self, path, *, query=None, authenticated=False):
        # The hand-modeled REST shape, for the env-var (real SDK) path.
        if path == "/v1/rfqs":
            return {"rfqs": self.rfqs._listings}
        assert path.startswith("/v1/rfqs/")
        rid = path.rsplit("/", 1)[-1]
        return self.rfqs._details.get(rid, {"id": rid})


def _event_types(items):
    return [i["raw"]["event_type"] for i in items if i["kind"] == "event"]


def _books(items):
    return [i for i in items if i["kind"] == "book"]


# --------------------------------------------------------------------------- #
# diffing                                                                      #
# --------------------------------------------------------------------------- #
def test_created_updated_closed_diffing():
    listings = [_rfq("A"), _rfq("B"), _rfq("C")]
    src = RetailPollingSource(client=FakeClient(listings))

    items = src.poll(NOW)
    assert sorted(_event_types(items)) == ["rfq_created"] * 3
    assert len(_books(items)) == 6  # 2 legs x 3 RFQs
    assert src.last_request_count == 1 + 6

    # Identical re-poll: no events (books still refresh within budget).
    items = src.poll(NOW)
    assert _event_types(items) == []
    assert len(_books(items)) == 6

    # Newer updatedTime -> rfq_updated.
    listings[0] = _rfq("A", updated=T1)
    items = src.poll(NOW)
    assert _event_types(items) == ["rfq_updated"]
    assert items[0]["raw"]["rfq_id"] == "A"

    # Terminal status -> rfq_closed (an exchange fact, not client-derived).
    listings[0] = _rfq("A", status="CANCELLED", updated=T2)
    items = src.poll(NOW)
    types = _event_types(items)
    assert types == ["rfq_closed"]
    assert items[0].get("client_derived") is False

    # Disappearance of an OPEN RFQ -> client-derived rfq_closed.
    # (Rebind the fake's listing: rebinding the local `listings` alone
    # would not affect what FakeRFQs.list() serves.)
    src._rfqs._listings = [_rfq("A", status="CANCELLED", updated=T2),
                           _rfq("B")]
    items = src.poll(NOW)
    types = _event_types(items)
    assert types == ["rfq_closed"]
    assert items[0]["raw"]["rfq_id"] == "C"
    assert items[0]["client_derived"] is True

    # Terminal RFQs never re-emit.
    items = src.poll(NOW)
    assert _event_types(items) == []


def test_new_rfq_without_legs_triggers_targeted_detail_fetch():
    sparse = _rfq("D", legs=False)
    detail = _rfq("D")  # full shape incl. legs
    client = FakeClient([sparse], details={"D": detail})
    src = RetailPollingSource(client=client)

    items = src.poll(NOW)
    assert ("retrieve", "D") in client.rfqs.calls
    assert _event_types(items) == ["rfq_created"]
    assert sorted(i["symbol"] for i in _books(items)) == \
        ["LEG-D-A", "LEG-D-B"]


def test_expired_status_emits_client_derived_expiry():
    src = RetailPollingSource(client=FakeClient([_rfq("E")]))
    assert _event_types(src.poll(NOW)) == ["rfq_created"]
    src._rfqs._listings = [_rfq("E", status="EXPIRED", updated=T1)]
    items = src.poll(NOW)
    assert _event_types(items) == ["rfq_expired"]
    assert items[0]["client_derived"] is True


def test_client_derived_flag_survives_into_audit_log():
    store = EventStore(":memory:")
    src = RetailPollingSource(client=FakeClient([_rfq("F")]))
    consumer = PollingConsumer(src, store, source_label="retail-poll")
    consumer.poll_once(NOW)
    src._rfqs._listings = []  # F disappears while OPEN
    consumer.poll_once(NOW)
    rows = store._conn.execute(
        "SELECT event_type, client_derived FROM raw_events "
        "WHERE rfq_id = 'F' ORDER BY rowid").fetchall()
    kinds = [(r[0], r[1]) for r in rows]
    assert ("rfq_created", 0) in kinds
    assert ("rfq_closed", 1) in kinds


def test_unmappable_and_size_missing_rfqs_are_skipped_not_fatal():
    listings = [
        {"symbol": "NO-ID", "status": "OPEN"},          # no id
        _rfq("G", qty=None) | {"qtyDecimal": None},    # no size anywhere
        _rfq("H"),
    ]
    # strip the size keys entirely for G
    del listings[1]["qtyDecimal"]
    src = RetailPollingSource(client=FakeClient(listings))
    items = src.poll(NOW)
    assert _event_types(items) == ["rfq_created"]
    assert items[0]["raw"]["rfq_id"] == "H"


# --------------------------------------------------------------------------- #
# request budget                                                               #
# --------------------------------------------------------------------------- #
def test_max_requests_per_poll_is_honored_and_drains():
    listings = [_rfq(f"R{i}") for i in range(3)]  # 6 legs total
    src = RetailPollingSource(client=FakeClient(listings),
                              max_requests_per_poll=3)
    all_books = []
    for _ in range(3):
        items = src.poll(NOW)
        assert src.last_request_count <= 3
        all_books.extend(_books(items))
    assert sorted(b["symbol"] for b in all_books) == sorted(
        f"LEG-R{i}-{s}" for i in range(3) for s in ("A", "B"))
    # Steady state: one list + two BBOs per poll stays within budget.
    items = src.poll(NOW)
    assert src.last_request_count == 3


def test_budget_zero_rejected():
    with pytest.raises(ValueError):
        RetailPollingSource(client=FakeClient([]), max_requests_per_poll=0)


def test_config_poll_knobs_validation():
    assert PipelineConfig().poll_interval_s == 5.0
    assert PipelineConfig().max_requests_per_poll == 10
    with pytest.raises(ValueError):
        PipelineConfig(poll_interval_s=0)
    with pytest.raises(ValueError):
        PipelineConfig(max_requests_per_poll=0)


# --------------------------------------------------------------------------- #
# credentials                                                                  #
# --------------------------------------------------------------------------- #
def test_missing_env_vars_refuse_startup(monkeypatch):
    monkeypatch.delenv(KEY_ID_ENV, raising=False)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    with pytest.raises(CredentialsNotConfigured):
        RetailPollingSource()
    monkeypatch.setenv(KEY_ID_ENV, "only-one")
    with pytest.raises(CredentialsNotConfigured):
        RetailPollingSource()


def test_sdk_not_installed_raises_helpful_error(monkeypatch):
    monkeypatch.setenv(KEY_ID_ENV, FAKE_KEY)
    monkeypatch.setenv(SECRET_ENV, FAKE_SECRET)
    monkeypatch.setitem(sys.modules, "polymarket_us", None)
    with pytest.raises(RuntimeError, match="pip install polymarket-us"):
        RetailPollingSource()


def test_no_credential_leakage_in_logs_repr_or_db(monkeypatch, caplog):
    """Fake credential material must not appear in logs, repr, or the DB."""
    monkeypatch.setenv(KEY_ID_ENV, FAKE_KEY)
    monkeypatch.setenv(SECRET_ENV, FAKE_SECRET)

    listings = [_rfq("L1"), _rfq("L2")]
    captured = {}

    def _factory(*, key_id, secret_key, timeout=30.0):
        captured["key_id"] = key_id
        captured["secret_key"] = secret_key
        return FakeClient(listings)

    fake_module = types.SimpleNamespace(PolymarketUS=_factory)
    monkeypatch.setitem(sys.modules, "polymarket_us", fake_module)

    source = RetailPollingSource()  # env path, fake SDK: no network
    assert captured["key_id"] == FAKE_KEY
    assert captured["secret_key"] == FAKE_SECRET
    assert FAKE_KEY not in repr(source)
    assert FAKE_SECRET not in repr(source)

    store = EventStore(":memory:")
    consumer = PollingConsumer(source, store, source_label="retail-poll")
    with caplog.at_level(logging.DEBUG, logger="combo_mm"):
        consumer.poll_once(NOW)
    assert consumer.items_applied == 2

    assert FAKE_KEY not in caplog.text
    assert FAKE_SECRET not in caplog.text

    tables = [r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert tables, "expected pipeline tables to exist"
    for table in tables:
        for row in store._conn.execute(f"SELECT * FROM {table}"):
            blob = " ".join(str(v) for v in row if v is not None)
            assert FAKE_KEY not in blob, f"key leaked into {table}"
            assert FAKE_SECRET not in blob, f"secret leaked into {table}"


def test_poll_failure_logs_class_only_no_credential_echo(monkeypatch, caplog):
    monkeypatch.setenv(KEY_ID_ENV, FAKE_KEY)
    monkeypatch.setenv(SECRET_ENV, FAKE_SECRET)

    class BoomRFQs(FakeRFQs):
        def list(self):
            raise RuntimeError("boom-429-from-server")

    client = FakeClient([])
    client.rfqs = BoomRFQs([])
    source = RetailPollingSource(client=client)
    with caplog.at_level(logging.DEBUG, logger="combo_mm"):
        with pytest.raises(RuntimeError):
            source.poll(NOW)
    assert "boom-429-from-server" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert FAKE_KEY not in caplog.text and FAKE_SECRET not in caplog.text


# --------------------------------------------------------------------------- #
# RFQ beta gate: 403 -> simulated RFQ events + retail books, no retry loop    #
# --------------------------------------------------------------------------- #
class FakePermissionDeniedError(Exception):
    """Mimics polymarket_us.errors.PermissionDeniedError (HTTP 403)."""

    status_code = 403


class BetaDeniedRFQs(FakeRFQs):
    """RFQ endpoints 403 while beta is not enabled for the key."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.denied = True

    def list(self):
        if self.denied:
            raise FakePermissionDeniedError("permission denied")
        return super().list()

    def retrieve(self, rfq_id):
        if self.denied:
            raise FakePermissionDeniedError("permission denied")
        return super().retrieve(rfq_id)


def _sim_fallback_session():
    """Tiny scripted feed: one book (must be skipped) + one RFQ event."""
    return [
        {"t": 0, "kind": "book", "symbol": "SIM-LEG", "bid": 0.50,
         "ask": 0.52, "bid_size": 1.0, "ask_size": 1.0, "seq": 0, "ts": T0},
        {"t": 0, "kind": "event", "raw": {
            "event_type": "rfq_created", "rfq_id": "SIM-1", "symbol": "SIM-SYM",
            "exchange_ts": T0,
            "payload": {
                "id": "SIM-1", "symbol": "SIM-SYM", "status": "OPEN",
                "qtyDecimal": "10",
                "comboLegs": [{"symbol": "SIM-LEG", "side": "YES"}],
            }}},
    ]


def _beta_denied_client(listings):
    client = FakeClient([])
    client.rfqs = BetaDeniedRFQs(listings)
    return client


def test_beta_403_falls_back_to_simulated_rfq_events(caplog):
    client = _beta_denied_client([])
    src = RetailPollingSource(
        client=client,
        fallback_rfq_source=SimulatedEventSource(_sim_fallback_session()))
    assert src.rfq_beta_enabled is True  # optimistic default

    with caplog.at_level(logging.DEBUG, logger="combo_mm"):
        items = src.poll(NOW)  # must not raise

    # (a) the actionable message is logged
    assert RFQ_BETA_MESSAGE in caplog.text
    assert "support@polymarket.us" in caplog.text

    # (b) simulated RFQ events flow through; the sim feed's book snapshots
    # must not clobber (retail) books
    events = [i for i in items if i["kind"] == "event"]
    assert [i["raw"]["rfq_id"] for i in events] == ["SIM-1"]
    assert not [i for i in items
                if i["kind"] == "book" and i["symbol"] == "SIM-LEG"]

    # capability flag + bookkeeping reflect the degraded state
    assert src.rfq_beta_enabled is False
    assert src.last_error == "rfq_beta_not_enabled"
    assert src.polls == 1


def test_beta_message_logged_once_per_outage(caplog):
    client = _beta_denied_client([])
    src = RetailPollingSource(
        client=client,
        fallback_rfq_source=SimulatedEventSource(_sim_fallback_session()))
    with caplog.at_level(logging.DEBUG, logger="combo_mm"):
        src.poll(NOW)   # transition: True -> False, logs RFQ_BETA_MESSAGE
        src.poll(NOW)   # still degraded: debug note only, no repeat
    assert caplog.text.count(RFQ_BETA_MESSAGE) == 1


def test_beta_flag_flips_back_on_when_read_succeeds(caplog):
    client = _beta_denied_client([_rfq("W")])
    src = RetailPollingSource(
        client=client,
        fallback_rfq_source=SimulatedEventSource(_sim_fallback_session()))

    src.poll(NOW)
    assert src.rfq_beta_enabled is False

    client.rfqs.denied = False  # enablement granted: no restart needed
    with caplog.at_level(logging.DEBUG, logger="combo_mm"):
        items = src.poll(NOW)
    assert src.rfq_beta_enabled is True
    assert src.last_error is None
    assert _event_types(items) == ["rfq_created"]
    assert items[0]["raw"]["rfq_id"] == "W"


def test_retail_books_continue_during_beta_outage():
    """Mixed mode: retail BBO keeps flowing while RFQ reads 403."""
    client = _beta_denied_client([_rfq("V")])
    client.rfqs.denied = False
    src = RetailPollingSource(
        client=client,
        fallback_rfq_source=SimulatedEventSource(_sim_fallback_session()))

    items = src.poll(NOW)  # healthy: V tracked, books flow via retail
    assert src.rfq_beta_enabled is True
    assert set(client.markets.calls) == {"LEG-V-A", "LEG-V-B"}

    client.markets.calls.clear()
    client.rfqs.denied = True  # beta revoked (or never enabled)
    items = src.poll(NOW)  # must not raise

    # (d) leg market data is NOT beta-gated: retail BBO refresh continues
    books = _books(items)
    assert books, "expected retail book refresh during the 403 window"
    assert set(client.markets.calls) == {"LEG-V-A", "LEG-V-B"}
    assert all(b["symbol"].startswith("LEG-V-") for b in books)
    # ...while RFQ events come from the simulated fallback
    assert [i["raw"]["rfq_id"] for i in items
            if i["kind"] == "event"] == ["SIM-1"]
    assert src.last_request_count <= src.max_requests_per_poll


def test_beta_403_does_not_raise_or_backoff_at_consumer():
    client = _beta_denied_client([])
    src = RetailPollingSource(
        client=client,
        fallback_rfq_source=SimulatedEventSource(_sim_fallback_session()))
    store = EventStore(":memory:")
    consumer = PollingConsumer(src, store, source_label="retail-poll")

    n = consumer.poll_once(NOW)  # must not raise: no retry/backoff triggered
    assert n == 1
    assert consumer.polls == 1
    assert consumer.last_error is None
    rows = store._conn.execute("SELECT rfq_id FROM rfq").fetchall()
    assert [r[0] for r in rows] == ["SIM-1"]


def test_beta_403_without_fallback_stays_graceful(caplog):
    client = _beta_denied_client([])
    src = RetailPollingSource(client=client)  # no fallback configured
    with caplog.at_level(logging.DEBUG, logger="combo_mm"):
        items = src.poll(NOW)  # must not raise
    assert items == []
    assert src.rfq_beta_enabled is False
    assert RFQ_BETA_MESSAGE in caplog.text


def test_beta_denied_on_detail_endpoint_flips_flag(caplog):
    class DenyRetrieve(FakeRFQs):
        def retrieve(self, rfq_id):
            raise FakePermissionDeniedError("permission denied")

    client = FakeClient([_rfq("D", legs=False)])
    client.rfqs = DenyRetrieve([_rfq("D", legs=False)])
    src = RetailPollingSource(client=client)
    with caplog.at_level(logging.DEBUG, logger="combo_mm"):
        items = src.poll(NOW)  # must not raise
    assert src.rfq_beta_enabled is False
    assert RFQ_BETA_MESSAGE in caplog.text
    # The poll still completes with what the listing provided.
    assert _event_types(items) == ["rfq_created"]


# --------------------------------------------------------------------------- #
# BBO parsing                                                                  #
# --------------------------------------------------------------------------- #
def test_bbo_parsing_accepts_nested_and_flat_shapes():
    parse = RetailPollingSource._parse_bbo
    nested = parse("S", _bbo(0.5, 0.52), T0)
    assert (nested["bid"], nested["ask"]) == (0.5, 0.52)
    assert (nested["bid_size"], nested["ask_size"]) == (120.0, 130.0)
    assert nested["ts"] == T0  # fetch time
    flat = parse("S", {"bestBid": "0.41", "bestAsk": 0.43,
                       "bidDepth": "7", "askDepth": "9"}, T0)
    assert (flat["bid"], flat["ask"]) == (0.41, 0.43)
    assert flat["bid_size"] == 7.0
    assert parse("S", {"marketData": {"bestBid": None}}, T0) is None
    assert parse("S", {}, T0) is None

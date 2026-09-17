"""Tests for combo_mm.intl_gateway (issue #11).

The websocket tests use a fake quoter-gateway server; the whole module is
skipped when the optional ``websockets`` package is missing
(``pip install websockets``).
"""
from __future__ import annotations

import ast
import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

websockets = pytest.importorskip("websockets")

import combo_mm.intl_gateway as ig
from combo_mm.intl_gateway import (
    GatewayCredentials,
    InternationalQuoterGatewayAdapter,
    MappingError,
    MissingCredentialsError,
    map_rfq_request,
    map_rfq_trade,
)
from combo_mm.normalize import normalize

CREDS = GatewayCredentials(
    api_key="key",
    api_secret="secret",
    api_passphrase="pass",
    wallet_address="0xabc",
)


def _rfq_frame(**overrides):
    frame = {
        "type": "RFQ_REQUEST",
        "rfq_id": "rfq_1",
        "requestor_public_id": "req_9",
        "leg_position_ids": ["111", "222"],
        "condition_id": "0xcond",
        "yes_position_id": "0xyes",
        "no_position_id": "0xno",
        "direction": "BUY",
        "side": "YES",
        "requested_size": {"unit": "notional", "value_e6": "1500000"},
        "submission_deadline": 9_999_999_999_999,
    }
    frame.update(overrides)
    return frame


# ---------------------------------------------------------------------------
# Pure mapping tests (no network)
# ---------------------------------------------------------------------------

def test_map_rfq_request_notional():
    raw = map_rfq_request(_rfq_frame())
    assert raw["event_type"] == "rfq_created"
    assert raw["rfq_id"] == "rfq_1"
    assert raw["cashOrderQty"] == "1.5"
    assert "qtyDecimal" not in raw
    assert raw["symbol"] == "0xcond"
    assert raw["rfqCreatorUserId"] == "req_9"
    assert raw["status"] == "RFQ_STATUS_OPEN"
    legs = raw["comboLegs"]
    assert [leg["symbol"] for leg in legs] == ["111", "222"]
    assert all(leg["side"] == "YES" for leg in legs)
    assert all(leg["settlementPrice"] is None for leg in legs)
    # Gateway-native extras ride along on the raw dict.
    assert raw["direction"] == "BUY"
    assert raw["condition_id"] == "0xcond"
    # ... and the mapped dict survives the real normalize().
    event = normalize(raw)
    assert event.event_type == "rfq_created"
    assert event.rfq_id == "rfq_1"
    assert event.payload["cashOrderQty"] == "1.5"


def test_map_rfq_request_shares():
    raw = map_rfq_request(
        _rfq_frame(requested_size={"unit": "shares", "value_e6": "2500000"})
    )
    assert raw["qtyDecimal"] == "2.5"
    assert "cashOrderQty" not in raw
    assert normalize(raw).payload["qtyDecimal"] == "2.5"


def test_map_rfq_request_rejects_bad_unit():
    with pytest.raises(MappingError):
        map_rfq_request(_rfq_frame(requested_size={"unit": "bushels", "value_e6": "1"}))


def test_map_rfq_request_rejects_bad_side():
    with pytest.raises(MappingError):
        map_rfq_request(_rfq_frame(side="MAYBE"))


def test_map_rfq_request_rejects_missing_fields():
    frame = _rfq_frame()
    del frame["rfq_id"]
    with pytest.raises(MappingError):
        map_rfq_request(frame)


def test_map_rfq_trade():
    raw = map_rfq_trade(
        {
            "type": "RFQ_TRADE",
            "rfq_id": "rfq_1",
            "requester_id": "req_9",
            "condition_id": "0xcond",
            "leg_position_ids": ["111"],
            "direction": "BUY",
            "side": "YES",
            "price_e6": "125000",
            "size_e6": "800000",
            "executed_at": 1780854786039,
        }
    )
    assert raw["event_type"] == "rfq_closed"
    assert raw["rfq_id"] == "rfq_1"
    # The accepted quote rides along as raw extras.
    assert raw["price"] == "0.125" and raw["size"] == "0.8"
    assert raw["executed_at"] == "2026-06-07T17:53:06.039000Z"
    assert raw["side"] == "YES" and raw["requester_id"] == "req_9"
    event = normalize(raw)
    assert event.event_type == "rfq_closed"


def test_map_rfq_trade_without_price_fields():
    raw = map_rfq_trade({"type": "RFQ_TRADE", "rfq_id": "rfq_1"})
    assert "price" not in raw and "size" not in raw and "executed_at" not in raw


def test_map_rfq_trade_rejects_missing_id():
    with pytest.raises(MappingError):
        map_rfq_trade({"type": "RFQ_TRADE"})


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def test_credentials_from_env_ok():
    env = {
        "POLYMARKET_API_KEY": "key-abc-123",
        "POLYMARKET_SECRET": "secret-def-456",
        "POLYMARKET_PASSPHRASE": "phrase-ghi-789",
        "POLYMARKET_ADDRESS": "0xabc",
    }
    creds = GatewayCredentials.from_env(env=env)
    assert creds.api_key == "key-abc-123"
    redacted = repr(creds)
    assert "<redacted>" in redacted
    for secret in ("key-abc-123", "secret-def-456", "phrase-ghi-789"):
        assert secret not in redacted


def test_credentials_missing_names_the_var():
    with pytest.raises(MissingCredentialsError) as excinfo:
        GatewayCredentials.from_env(env={"POLYMARKET_API_KEY": "k"})
    msg = str(excinfo.value)
    assert "POLYMARKET_SECRET" in msg and "POLYMARKET_ADDRESS" in msg


def test_credentials_dotenv_fills_gaps(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\nPOLYMARKET_API_KEY=k\nPOLYMARKET_SECRET='s'\n"
        'POLYMARKET_PASSPHRASE="p"\nPOLYMARKET_ADDRESS=0xabc\n'
    )
    creds = GatewayCredentials.from_env(env={}, dotenv_path=dotenv)
    assert (creds.api_key, creds.api_secret, creds.api_passphrase) == ("k", "s", "p")


def test_credentials_env_wins_over_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("POLYMARKET_API_KEY=fromfile\nPOLYMARKET_SECRET=s\n")
    # File alone cannot satisfy the four required vars...
    with pytest.raises(MissingCredentialsError):
        GatewayCredentials.from_env(env={}, dotenv_path=dotenv)
    # ...but the real environment fills the gap and wins over the file.
    creds = GatewayCredentials.from_env(
        env={
            "POLYMARKET_API_KEY": "fromenv",
            "POLYMARKET_SECRET": "s",
            "POLYMARKET_PASSPHRASE": "p",
            "POLYMARKET_ADDRESS": "0xabc",
        },
        dotenv_path=dotenv,
    )
    assert creds.api_key == "fromenv"


# ---------------------------------------------------------------------------
# Fake gateway server
# ---------------------------------------------------------------------------

class _FakeGateway:
    """Scripted quoter-gateway: auth handshake, then per-connection frames."""

    def __init__(self, on_auth=None):
        self.auth_frames = []
        self.connections = 0
        self.on_connect = None  # async hook(ws, connection_index)
        self._on_auth = on_auth or self._accept_auth

    @staticmethod
    async def _accept_auth(ws, msg):
        await ws.send(json.dumps({"type": "auth", "success": True}))

    async def handler(self, ws):
        raw = await ws.recv()
        msg = json.loads(raw)
        self.auth_frames.append(msg)
        self.connections += 1
        await self._on_auth(ws, msg)
        if self.on_connect is not None:
            await self.on_connect(ws, self.connections)
        try:
            async for _ in ws:  # hold open; client only sends pings
                pass
        except Exception:
            pass


def _serve_in_thread(gw):
    """Run the fake gateway on an ephemeral port; return (thread, url)."""
    holder = {}
    ready = threading.Event()

    async def _main():
        server = await websockets.serve(gw.handler, "127.0.0.1", 0)
        try:
            sockets = getattr(server, "sockets", None) or []
            holder["port"] = sockets[0].getsockname()[1]
        except Exception:
            holder["port"] = 18765
        ready.set()
        await asyncio.Future()  # serve forever

    thread = threading.Thread(
        target=lambda: asyncio.run(_main()), daemon=True
    )
    thread.start()
    assert ready.wait(timeout=10), "fake gateway did not start"
    return thread, f"ws://127.0.0.1:{holder['port']}/ws/rfq"


def _wait_for(predicate, timeout=10.0, tick=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(tick)
    return False


def _make_adapter(url):
    return InternationalQuoterGatewayAdapter(
        CREDS,
        url=url,
        backoff_initial_s=0.05,
        backoff_max_s=0.5,
        ping_interval_s=5.0,
        silence_watchdog_s=30.0,
    )


def test_auth_handshake_shape():
    gw = _FakeGateway()
    _, url = _serve_in_thread(gw)
    adapter = _make_adapter(url)
    adapter.start()
    try:
        assert _wait_for(lambda: len(gw.auth_frames) >= 1), "no auth frame seen"
        auth = gw.auth_frames[0]
        assert auth["type"] == "auth"
        assert auth["auth"]["apiKey"] == "key"
        assert auth["auth"]["passphrase"] == "pass"
        assert auth["auth"]["secret"] == "secret"
        assert auth["identity"]["signer_address"] == "0xabc"
        assert auth["identity"]["maker_address"] == "0xabc"
        assert auth["identity"]["signature_type"] == 0
        assert _wait_for(lambda: adapter.connected), "adapter never connected"
        assert _wait_for(lambda: adapter.stats()["auth"] == "accepted"), "positive ack not recorded"
        assert adapter.stats()["auth_error"] is None
    finally:
        adapter.stop()


def test_rfq_request_end_to_end_mapping():
    gw = _FakeGateway()

    async def _script(ws, _n):
        await ws.send(json.dumps(_rfq_frame()))
        await ws.send(
            json.dumps(
                {
                    "type": "RFQ_TRADE",
                    "rfq_id": "rfq_1",
                    "requester_id": "req_9",
                    "condition_id": "0xcond",
                    "leg_position_ids": ["111", "222"],
                    "direction": "BUY",
                    "side": "YES",
                }
            )
        )

    gw.on_connect = _script
    _, url = _serve_in_thread(gw)
    adapter = _make_adapter(url)
    adapter.start()
    try:
        assert _wait_for(lambda: adapter.stats()["rfqs_seen"] >= 1)
        assert _wait_for(lambda: adapter.stats()["trades_seen"] >= 1)
        items = []
        assert _wait_for(
            lambda: items.extend(adapter.poll(datetime.now(timezone.utc)))
            or len(items) >= 2
        )
        by_type = {it["raw"]["event_type"] for it in items}
        assert by_type == {"rfq_created", "rfq_closed"}
        for it in items:
            assert it["kind"] == "event"
            normalize(it["raw"])  # must survive the real pipeline path
        # Buffer drained by poll().
        assert adapter.poll(datetime.now(timezone.utc)) == []
    finally:
        adapter.stop()


def test_reconnect_after_drop():
    gw = _FakeGateway()

    async def _script(ws, n):
        if n == 1:
            await ws.send(json.dumps(_rfq_frame(rfq_id="rfq_first")))
            await ws.close()  # drop; server keeps listening for the redial
        else:
            await ws.send(json.dumps(_rfq_frame(rfq_id="rfq_second")))

    gw.on_connect = _script
    _, url = _serve_in_thread(gw)
    adapter = _make_adapter(url)
    adapter.start()
    try:
        assert _wait_for(lambda: adapter.stats()["rfqs_seen"] >= 2, timeout=15)
        stats = adapter.stats()
        assert stats["connects"] >= 2
        assert stats["reconnects"] >= 1
        raws = [it["raw"] for it in adapter.poll(datetime.now(timezone.utc))]
        assert {r["rfq_id"] for r in raws} == {"rfq_first", "rfq_second"}
    finally:
        adapter.stop()


def test_gateway_without_auth_ack_streams_rfqs():
    """The live gateway sends no auth ack: RFQ frames follow the auth frame directly."""
    async def _no_ack(ws, _msg):
        await ws.send(json.dumps(_rfq_frame(rfq_id="rfq_noack")))

    gw = _FakeGateway(on_auth=_no_ack)
    _, url = _serve_in_thread(gw)
    adapter = _make_adapter(url)
    adapter.start()
    try:
        assert _wait_for(lambda: adapter.stats()["rfqs_seen"] >= 1), "RFQ after auth was dropped"
        assert adapter.connected
        stats = adapter.stats()
        assert stats["last_error"] is None and stats["connects"] == 1 and stats["reconnects"] == 0
        assert stats["auth"] == "pending"
        raws = [it["raw"] for it in adapter.poll(datetime.now(timezone.utc))]
        assert [r["rfq_id"] for r in raws] == ["rfq_noack"]
    finally:
        adapter.stop()


def test_auth_rejection_is_reported_and_the_public_feed_keeps_streaming():
    """Live gateway: RFQs arrive before the auth reply and keep coming after a rejection."""
    async def _reject_mid_stream(ws, _msg):
        await ws.send(json.dumps(_rfq_frame(rfq_id="rfq_before")))
        await ws.send(json.dumps({
            "type": "auth", "success": False,
            "error": "rpc error: code = PermissionDenied desc = could not validate web socket request"}))
        await ws.send(json.dumps(_rfq_frame(rfq_id="rfq_after")))

    gw = _FakeGateway(on_auth=_reject_mid_stream)
    _, url = _serve_in_thread(gw)
    adapter = _make_adapter(url)
    adapter.start()
    try:
        assert _wait_for(lambda: adapter.stats()["rfqs_seen"] >= 2), "feed stopped after auth rejection"
        stats = adapter.stats()
        assert stats["auth"] == "rejected" and "PermissionDenied" in stats["auth_error"]
        assert stats["connects"] == 1 and stats["reconnects"] == 0 and stats["last_error"] is None
        assert adapter.connected and len(gw.auth_frames) == 1
        raws = [it["raw"] for it in adapter.poll(datetime.now(timezone.utc))]
        assert [r["rfq_id"] for r in raws] == ["rfq_before", "rfq_after"]
    finally:
        adapter.stop()


# ---------------------------------------------------------------------------
# Receive-only structural assertion
# ---------------------------------------------------------------------------

def test_no_trading_code_paths():
    """Tripwire: the adapter must never grow quote/order/trading code."""
    src = Path(ig.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = set()
    strings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
    banned_names = {
        "quote",
        "submit_quote",
        "place_order",
        "create_order",
        "cancel_quote",
        "send_order",
        "sign_order",
    }
    assert not (banned_names & names), f"trading identifiers found: {banned_names & names}"
    banned_substrings = ("signed_order", "maker/quotes", "RFQ_QUOTE", "v1/maker")
    hits = [s for s in strings for b in banned_substrings if b in s]
    assert not hits, f"trading wire tokens found in string literals: {hits[:3]}"

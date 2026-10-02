"""Inventory loading is pruned and chunked, never different.

``read_inventory_rows`` skips RFQs the inventory can never read. The first
test pins that the resulting snapshot equals the one built from the full,
unpruned tables (the loader this replaced), across randomized data.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

import combo_mm.store as store_module
from combo_mm.inventory import InventoryProvider
from combo_mm.store import EventStore, read_inventory_rows

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()
NOW_MS = int(NOW.timestamp() * 1000)


class FullLoadStore:
    """The previous loader: every row, one legs query per RFQ."""

    def __init__(self, conn):
        self._conn = conn

    def inventory_rows(self):
        c = self._conn
        rfqs = [dict(r) for r in c.execute(
            "SELECT r.rfq_id, r.symbol, r.status, r.updated_time, "
            "s.submission_deadline FROM rfq r LEFT JOIN rfq_screen s "
            "ON s.rfq_id=r.rfq_id ORDER BY r.rfq_id")]
        for rfq in rfqs:
            rfq["legs"] = [dict(r) for r in c.execute(
                "SELECT symbol, side, settlement_price FROM rfq_legs "
                "WHERE rfq_id=? ORDER BY rowid", (rfq["rfq_id"],))]
        quotes = [dict(r) for r in c.execute(
            "SELECT quote_id, rfq_id, symbol, status, origin, buy_price, "
            "sell_price, buy_qty_decimal, sell_qty_decimal, created_time "
            "FROM quotes ORDER BY rowid")]
        fills = [dict(r) for r in c.execute(
            "SELECT fill_id, rfq_id, quote_id, symbol, side, price, qty, "
            "executed_time FROM fills ORDER BY fill_id")]
        last = c.execute(
            "SELECT state FROM kill_switch_events ORDER BY id DESC LIMIT 1").fetchone()
        return rfqs, quotes, fills, bool(last and last["state"] == "tripped")


def _add_rfq(store, rfq_id, *, status="RFQ_STATUS_OPEN", deadline=None, screen=True,
             legs=("LEG-A", "LEG-B")):
    with store._conn:
        store._conn.execute(
            "INSERT INTO rfq(rfq_id,symbol,status) VALUES (?,?,?)",
            (rfq_id, f"COMBO-{rfq_id}", status))
        for leg in legs:
            store._conn.execute(
                "INSERT INTO rfq_legs(rfq_id,symbol,side) VALUES (?,?,'YES')",
                (rfq_id, f"{leg}-{rfq_id}"))
    if screen:
        store.upsert_rfq_screen(rfq_id, n_legs=len(legs), n_resolved=len(legs),
                                n_nfl_legs=len(legs), screen="QUOTABLE", rank=0,
                                catalog_version=1, submission_deadline=deadline)


def _quote(store, rfq_id, n=1, *, buy=.52, sell=.48, qty="100"):
    for i in range(n):
        store.record_shadow_draft(
            quote_id=f"Q-{rfq_id}-{i}", rfq_id=rfq_id, symbol=f"COMBO-{rfq_id}",
            buy_price=buy, sell_price=sell, buy_qty=qty, sell_qty=qty,
            decided_at=f"2026-09-18T11:59:{i:02d}Z")


@pytest.mark.parametrize("seed", range(5))
def test_pruned_snapshot_equals_full_load_snapshot(seed, monkeypatch):
    monkeypatch.setattr(store_module, "_ID_CHUNK", 3)   # force several chunks
    rng = random.Random(seed)
    store = EventStore()
    past, future = NOW_MS - 60_000, NOW_MS + 60_000
    deadlines = [None, "", str(past), str(future), str(NOW_MS), "0",
                 "2026-09-18T11:00:00Z", "2026-09-18T13:00:00Z", "garbage"]
    for i in range(40):
        rfq_id = f"R{i:02d}"
        _add_rfq(store, rfq_id,
                 status=rng.choice(["RFQ_STATUS_OPEN", "OPEN", "QUOTED", "CLOSED",
                                    "RFQ_STATUS_EXPIRED"]),
                 deadline=rng.choice(deadlines), screen=rng.random() < 0.8,
                 legs=rng.choice([("LEG-A",), ("LEG-A", "LEG-B")]))
        roll = rng.random()
        if roll < 0.75:
            _quote(store, rfq_id, n=rng.randint(1, 3),
                   buy=round(rng.uniform(.2, .8), 2), sell=round(rng.uniform(.1, .7), 2),
                   qty=str(rng.choice([10, 50, 100])))
        if rng.random() < 0.25:    # filled RFQs keep their position forever
            assert store.record_fill(
                fill_id=f"F{i}", rfq_id=rfq_id, quote_id=f"Q-{rfq_id}-0",
                symbol=f"COMBO-{rfq_id}", side=rng.choice(["BUY", "SELL"]),
                price=round(rng.uniform(.2, .8), 2), qty=rng.randint(1, 20),
                executed_time="2026-09-18T11:59:30Z")
    # Non-shadow rows and a tripped kill switch must also come through intact.
    with store._conn:
        store._conn.execute(
            "INSERT INTO quotes(quote_id,rfq_id,symbol,status,origin,buy_price,sell_price,"
            "buy_qty_decimal,sell_qty_decimal,created_time) VALUES "
            "('LIVEQ','R00','COMBO-R00','ACTIVE','live',.5,.5,10,10,'2026-09-18T11:00:00Z')")
        store._conn.execute(
            "INSERT INTO kill_switch_events(ts,state,trigger,detail_json) "
            "VALUES ('2026-09-18T11:00:00Z','tripped','t','{}')")

    full = InventoryProvider(FullLoadStore(store._conn))
    pruned = InventoryProvider(store)
    assert pruned._prunes_expired and not full._prunes_expired
    for as_of in ("", NOW_ISO, "2026-09-18T11:59:45Z", "2026-09-18T12:30:00Z"):
        a, b = pruned(as_of), full(as_of)
        if not as_of:       # "now" differs between calls; compare against a fixed instant
            a, b = pruned(NOW_ISO), full(NOW_ISO)
        assert a == b, as_of


def test_expired_rfqs_are_not_loaded_but_filled_ones_are():
    store = EventStore()
    past, future = str(NOW_MS - 1), str(NOW_MS + 60_000)
    _add_rfq(store, "OLD", deadline=past)
    _add_rfq(store, "LIVE", deadline=future)
    _add_rfq(store, "NOSCREEN", screen=False)
    _add_rfq(store, "FILLED_OLD", deadline=past)
    _add_rfq(store, "NOQUOTE", deadline=future)      # never quoted: never needed
    for rfq_id in ("OLD", "LIVE", "NOSCREEN", "FILLED_OLD"):
        _quote(store, rfq_id)
    assert store.record_fill(fill_id="F", rfq_id="FILLED_OLD", quote_id="Q-FILLED_OLD-0",
                             symbol="COMBO-FILLED_OLD", side="BUY", price=.5, qty=5,
                             executed_time="2026-09-18T11:59:30Z")

    rfqs, quotes, fills, halted = read_inventory_rows(store._conn, deadline_after_ms=NOW_MS)
    assert [r["rfq_id"] for r in rfqs] == ["FILLED_OLD", "LIVE", "NOSCREEN"]
    assert {q["rfq_id"] for q in quotes} == {"FILLED_OLD", "LIVE", "NOSCREEN"}
    assert [f["fill_id"] for f in fills] == ["F"] and halted is False
    assert [leg["symbol"] for leg in rfqs[1]["legs"]] == ["LEG-A-LIVE", "LEG-B-LIVE"]

    everything = read_inventory_rows(store._conn)       # no pruning requested
    assert [r["rfq_id"] for r in everything[0]] == ["FILLED_OLD", "LIVE", "NOSCREEN", "OLD"]


def test_loading_issues_a_constant_number_of_queries():
    store = EventStore()
    future = str(NOW_MS + 60_000)
    for i in range(60):
        _add_rfq(store, f"R{i:02d}", deadline=future)
        _quote(store, f"R{i:02d}")
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        rfqs, quotes, _, _ = store.inventory_rows(deadline_after_ms=NOW_MS)
    finally:
        store._conn.set_trace_callback(None)
    assert len(rfqs) == len(quotes) == 60
    # rfq + (legs, quotes) per 500-id chunk + fills + kill switch -- not one per RFQ.
    assert len(statements) <= 6, statements


def test_provider_falls_back_for_stores_without_pruning():
    class Plain:
        def inventory_rows(self):
            return [], [], [], False

    assert InventoryProvider(Plain())(NOW_ISO).exposures == {}

"""Read-only live tab projections against the actual capture schema."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from combo_mm.normalize import normalize
from combo_mm.intl_gateway import GatewayCredentials, InternationalQuoterGatewayAdapter
from combo_mm.quote_selections import QuoteSelectionStore
from combo_mm.store import EventStore
from dashboard.live_view_models import connect_readonly, engine_status, fills, performance, pricing, rfqs


def test_live_tabs_reconcile_to_one_database(tmp_path):
    path = tmp_path / "capture.db"
    store = EventStore(str(path))
    quotes = QuoteSelectionStore(path)
    posted = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    raw = {
        "event_type": "rfq_created", "rfq_id": "rfq-1", "event_id": "event-1",
        "symbol": "combo", "createdTime": posted.isoformat(),
        "updatedTime": posted.isoformat(), "status": "RFQ_STATUS_OPEN",
        "qtyDecimal": "10", "comboLegs": [
            {"symbol": "leg-1", "side": "YES"},
            {"symbol": "leg-2", "side": "YES"}],
    }
    assert store.apply(normalize(raw, now=posted))
    store.upsert_rfq_screen("rfq-1", n_legs=2, n_resolved=2, n_nfl_legs=2,
                            screen="QUOTABLE", rank=0, catalog_version=1,
                            checks_json=json.dumps({"known legs": True,
                                                    "NFL same game": True,
                                                    "no unsupported same game": True,
                                                    "at least 1 shares": True}))
    quotes.record_priced_quote({
        "rfq_id": "rfq-1", "priced_at": posted.isoformat(), "status": "QUOTED",
        "reason_code": "QUOTED_OK", "response_action": "SELL",
        "response_price": .55, "size": 10, "size_unit": "shares",
        "fair": .50, "naive": .45, "side": "YES", "games": [{"game": "SEA-ARI"}],
        "components": {"base": 5}}, "auto")
    store.record_shadow_draft(quote_id="paper:rfq-1:auto", rfq_id="rfq-1",
                              buy_price=.55, sell_price=.45,
                              buy_qty="10", sell_qty="10")
    store.record_live_trade("rfq-1", .56, 10, posted.isoformat())
    store.record_live_latency(
        rfq_id="rfq-1", posted_at=posted.isoformat(),
        started_at=(posted + timedelta(milliseconds=25)).isoformat(),
        decided_at=(posted + timedelta(milliseconds=65)).isoformat(), quoted=True)
    store.update_live_health(started_at=posted.isoformat(), messages_processed=2,
                             errors=0, gateway_connected=True, buffer_drops=0)
    with connect_readonly(path) as conn:
        feed = rfqs(conn, only_quotable=True)
        assert [row["rfq_id"] for row in feed] == ["rfq-1"]
        assert all(feed[0]["filters"].values())
        decision = pricing(conn)[0]
        assert decision["status"] == "QUOTED"
        assert decision["market_price"] == .56
        assert decision["edge_vs_market"] < 0
        assert decision["wait_ms"] == 25
        assert decision["compute_ms"] == 40
        pnl = performance(conn)
        assert pnl["quoted"] == pnl["shadow_fills"] == 1
        assert round(pnl["expected_pnl"], 2) == .50
        assert pnl["realized_pnl"] == 0
        assert pnl["by_game"][0]["game"] == "SEA-ARI"
        assert pnl["by_market_source"][0]["market_source"] == "accepted trade"
        health = engine_status(conn)
        assert health["health"]["messages_processed"] == 2
        assert health["wait"]["p50"] == 25
        assert health["compute"]["p50"] == 40
        try:
            conn.execute("DELETE FROM rfq")
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("live dashboard connection must be read-only")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE priced_quotes SET after_deadline = 1 WHERE rfq_id = 'rfq-1'")
    with connect_readonly(path) as conn:
        assert pricing(conn)[0]["after_deadline"] == 1
        # Late quotes stay in the ledger, flagged on the fill.
        assert performance(conn)["shadow_fills"] == 1
        assert fills(conn)[0]["after_deadline"] is True
    quotes.close()
    store.close()


def test_gateway_wakes_headless_consumer_on_frame():
    adapter = InternationalQuoterGatewayAdapter(GatewayCredentials(
        api_key="k", api_secret="s", api_passphrase="p", wallet_address="0xabc"))
    assert not adapter.wait_for_items(0)
    adapter._emit({"rfq_id": "r", "exchange_ts": "2026-09-17T12:00:00Z"}, "rfq")
    assert adapter.wait_for_items(0)
    assert len(adapter.poll(datetime.now(timezone.utc))) == 1
    assert not adapter.wait_for_items(0)


def test_no_observed_trade_falls_back_to_leg_implied_naive(tmp_path):
    """Without an observed trade, the naive price is the market reference."""
    path = tmp_path / "capture.db"
    store = EventStore(str(path))
    quotes = QuoteSelectionStore(path)
    posted = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)

    def add_rfq(rfq_id, response_price, naive, after_deadline=False):
        raw = {
            "event_type": "rfq_created", "rfq_id": rfq_id, "event_id": f"event-{rfq_id}",
            "symbol": "combo", "createdTime": posted.isoformat(),
            "updatedTime": posted.isoformat(), "status": "RFQ_STATUS_OPEN",
            "qtyDecimal": "10", "comboLegs": [
                {"symbol": "leg-1", "side": "YES"},
                {"symbol": "leg-2", "side": "YES"}],
        }
        assert store.apply(normalize(raw, now=posted))
        store.upsert_rfq_screen(rfq_id, n_legs=2, n_resolved=2, n_nfl_legs=2,
                                screen="QUOTABLE", rank=0, catalog_version=1,
                                checks_json=json.dumps({"known legs": True}))
        quotes.record_priced_quote({
            "rfq_id": rfq_id, "priced_at": posted.isoformat(), "status": "QUOTED",
            "reason_code": "QUOTED_OK", "response_action": "SELL",
            "response_price": response_price, "size": 10, "size_unit": "shares",
            "fair": .42, "naive": naive, "side": "YES", "games": [{"game": "SEA-ARI"}],
            "components": {"base": 5},
            "after_deadline": after_deadline}, "auto")
        store.record_shadow_draft(quote_id=f"paper:{rfq_id}:auto", rfq_id=rfq_id,
                                  buy_price=response_price, sell_price=.45,
                                  buy_qty="10", sell_qty="10")

    # Neither RFQ has an observed Combo trade. rfq-2 beats the naive price
    # (SELL @ .40 vs .45); rfq-3 is worse than naive (SELL @ .50 vs .45).
    # rfq-4 is quoted after the deadline but must still be logged, flagged.
    add_rfq("rfq-2", .40, .45)
    add_rfq("rfq-3", .50, .45)
    add_rfq("rfq-4", .40, .45, after_deadline=True)

    with connect_readonly(path) as conn:
        detail = {row["rfq_id"]: row for row in pricing(conn)}
        for row in detail.values():
            assert row["naive"] == .45
            assert row["market_price"] == .45
            assert row["market_source"] == "leg-implied naive"
        assert detail["rfq-2"]["edge_vs_market"] == pytest.approx(-0.05)
        assert detail["rfq-4"]["after_deadline"] == 1

        pnl = performance(conn)
        assert pnl["quoted"] == 3
        assert pnl["shadow_fills"] == 2  # rfq-3 was worse than the market ref
        assert pnl["by_market_source"] == [
            {"market_source": "leg-implied naive", "shadow_fills": 2,
             "expected_pnl": pnl["expected_pnl"],
             "realized_pnl": pnl["realized_pnl"]}]
        by_id = {f["rfq_id"]: f for f in fills(conn)}
        assert by_id["rfq-2"]["market_source"] == "leg-implied naive"
        assert by_id["rfq-2"]["after_deadline"] is False
        assert by_id["rfq-4"]["after_deadline"] is True
        assert "rfq-3" not in by_id


def test_observed_trade_still_takes_precedence_over_naive(tmp_path):
    """An accepted trade remains the market reference when one is observed."""
    path = tmp_path / "capture.db"
    store = EventStore(str(path))
    quotes = QuoteSelectionStore(path)
    posted = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    raw = {
        "event_type": "rfq_created", "rfq_id": "rfq-9", "event_id": "event-rfq-9",
        "symbol": "combo", "createdTime": posted.isoformat(),
        "updatedTime": posted.isoformat(), "status": "RFQ_STATUS_OPEN",
        "qtyDecimal": "10", "comboLegs": [
            {"symbol": "leg-1", "side": "YES"},
            {"symbol": "leg-2", "side": "YES"}],
    }
    assert store.apply(normalize(raw, now=posted))
    store.upsert_rfq_screen("rfq-9", n_legs=2, n_resolved=2, n_nfl_legs=2,
                            screen="QUOTABLE", rank=0, catalog_version=1,
                            checks_json=json.dumps({"known legs": True}))
    quotes.record_priced_quote({
        "rfq_id": "rfq-9", "priced_at": posted.isoformat(), "status": "QUOTED",
        "reason_code": "QUOTED_OK", "response_action": "SELL",
        "response_price": .40, "size": 10, "size_unit": "shares",
        "fair": .42, "naive": .45, "side": "YES", "games": [{"game": "SEA-ARI"}],
        "components": {"base": 5}}, "auto")
    store.record_shadow_draft(quote_id="paper:rfq-9:auto", rfq_id="rfq-9",
                              buy_price=.40, sell_price=.45,
                              buy_qty="10", sell_qty="10")
    store.record_live_trade("rfq-9", .43, 10, posted.isoformat())

    with connect_readonly(path) as conn:
        row = pricing(conn, rfq_id="rfq-9")[0]
        assert row["market_price"] == .43
        assert row["market_source"] == "accepted trade"
        assert row["edge_vs_market"] == pytest.approx(-0.03)
        by_id = {f["rfq_id"]: f for f in fills(conn)}
        assert by_id["rfq-9"]["market_source"] == "accepted trade"
        assert by_id["rfq-9"]["market_price"] == .43
    quotes.close()
    store.close()

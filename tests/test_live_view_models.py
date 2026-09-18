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
from dashboard.live_view_models import (
    connect_readonly, correlation_lift, engine_status, fills, inventory_state,
    performance, pricing, rfqs,
)


def test_paper_notional_stays_within_inventory_equity_and_reduces_buying_power(tmp_path):
    path = tmp_path / "capture.db"
    store = EventStore(str(path))
    quotes = QuoteSelectionStore(path)
    for index, action in enumerate(("BUY", "BUY", "SELL"), 1):
        rfq_id = f"R{index}"
        with store._conn:
            store._conn.execute(
                "INSERT INTO rfq(rfq_id,symbol,status) VALUES (?,?,?)",
                (rfq_id, f"COMBO{index}", "RFQ_STATUS_OPEN"))
        store.upsert_rfq_screen(rfq_id, n_legs=2, n_resolved=2,
                                n_nfl_legs=2, screen="QUOTABLE", rank=0,
                                catalog_version=1)
        quotes.record_priced_quote({
            "rfq_id": rfq_id, "priced_at": f"2026-09-18T00:00:0{index}Z",
            "status": "QUOTED", "reason_code": "QUOTED_OK",
            "response_action": action, "response_price": .5,
            "size": 60000 if index < 3 else 20000, "size_unit": "shares",
            "fair": .5, "naive": .5, "side": "YES",
            "games": [{"game": "KC@BUF", "away": "KC", "home": "BUF"}],
            "legs": [{"slug": "kc-buf-moneyline"}, {"slug": "kc-buf-total"}]}, "auto")
        store.record_shadow_draft(quote_id=f"Q{index}", rfq_id=rfq_id,
                                  buy_price=.5, sell_price=.5,
                                  buy_qty="60000", sell_qty="60000")
    with connect_readonly(path) as conn:
        perf = performance(conn)
        inventory = inventory_state(conn)
        ledger = fills(conn)
    assert max(abs(point["net_notional"]) for point in perf["curve"]) <= inventory["equity"]
    assert perf["net_notional"] == inventory["paper_net_notional"] == 40000.0
    assert inventory["buying_power"] == 10000.0
    assert inventory["pending"] == {}
    assert inventory["executed"]["KC@BUF"] == inventory["paper_wcl"] == 60000.0
    assert inventory["exposures"]["KC@BUF"] == 60000.0
    assert inventory["markets"]["kc-buf-moneyline"] == 60000.0
    assert inventory["teams"]["KC"] == inventory["teams"]["BUF"] == 60000.0
    assert any(event["action"] == "paper quote" for event in inventory["paper_events"])
    assert any(event["action"] == "capital cap" for event in inventory["paper_events"])
    assert any(row.get("capacity_limited") for row in ledger)
    quotes.close()
    store.close()


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


def test_no_observed_trade_is_an_assumed_win(tmp_path):
    """Without an observed accepted trade the quote stands as an assumed win.

    The leg-implied naive combo price is our own number, not the market's, so
    it never stands in as the reference: the fill carries no market price and
    no market edge. Only a trade that beats our quote removes the fill.
    """
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

    # None of these RFQs traded on the feed, so none has a market reference.
    add_rfq("rfq-2", .40, .45)
    add_rfq("rfq-3", .50, .45)
    add_rfq("rfq-4", .40, .45, after_deadline=True)

    with connect_readonly(path) as conn:
        detail = {row["rfq_id"]: row for row in pricing(conn)}
        for row in detail.values():
            # naive stays available as our own number, but it is not the market.
            assert row["naive"] == .45
            assert row["market_price"] is None
            assert row["market_source"] is None
            assert row["edge_vs_market"] is None
        assert detail["rfq-4"]["after_deadline"] == 1

        pnl = performance(conn)
        assert pnl["quoted"] == 3
        # No accepted trades -> every quote stands as an assumed win.
        assert pnl["shadow_fills"] == 3
        # SELL: expected = -(fair - price) * 10 -> -.2, -.2, +.8
        assert pnl["by_market_source"] == [
            {"market_source": "no observed trade", "shadow_fills": 3,
             "expected_pnl": pytest.approx(0.4), "realized_pnl": 0}]
        by_id = {f["rfq_id"]: f for f in fills(conn)}
        assert set(by_id) == {"rfq-2", "rfq-3", "rfq-4"}
        for f in by_id.values():
            assert f["market_price"] is None
            assert f["market_source"] == "no observed trade"
            assert f["quote_edge"] is None
            assert f["model_edge"] is None
        assert by_id["rfq-4"]["after_deadline"] is True
        assert by_id["rfq-2"]["after_deadline"] is False


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


def test_beating_trade_removes_fill(tmp_path):
    """An accepted trade better than our quote means we lost the auction."""
    path = tmp_path / "capture.db"
    store = EventStore(str(path))
    quotes = QuoteSelectionStore(path)
    posted = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    raw = {
        "event_type": "rfq_created", "rfq_id": "rfq-b", "event_id": "event-rfq-b",
        "symbol": "combo", "createdTime": posted.isoformat(),
        "updatedTime": posted.isoformat(), "status": "RFQ_STATUS_OPEN",
        "qtyDecimal": "10", "comboLegs": [
            {"symbol": "leg-1", "side": "YES"},
            {"symbol": "leg-2", "side": "YES"}],
    }
    assert store.apply(normalize(raw, now=posted))
    store.upsert_rfq_screen("rfq-b", n_legs=2, n_resolved=2, n_nfl_legs=2,
                            screen="QUOTABLE", rank=0, catalog_version=1,
                            checks_json=json.dumps({"known legs": True}))
    quotes.record_priced_quote({
        "rfq_id": "rfq-b", "priced_at": posted.isoformat(), "status": "QUOTED",
        "reason_code": "QUOTED_OK", "response_action": "SELL",
        "response_price": .40, "size": 10, "size_unit": "shares",
        "fair": .42, "naive": .45, "side": "YES", "games": [{"game": "SEA-ARI"}],
        "components": {"base": 5}}, "auto")
    store.record_shadow_draft(quote_id="paper:rfq-b:auto", rfq_id="rfq-b",
                              buy_price=.40, sell_price=.45,
                              buy_qty="10", sell_qty="10")
    # Someone else sold at .38, undercutting our .40 ask: we lost.
    store.record_live_trade("rfq-b", .38, 10, posted.isoformat())

    with connect_readonly(path) as conn:
        assert performance(conn)["shadow_fills"] == 0
        assert fills(conn) == []
        row = pricing(conn, rfq_id="rfq-b")[0]
        assert row["market_price"] == .38
        assert row["edge_vs_market"] == pytest.approx(0.02)  # SELL: -1*(.38-.40)
    quotes.close()
    store.close()


def _quoted_rfq(quotes: QuoteSelectionStore, rfq_id: str, corr_bps: float | None) -> None:
    quotes.record_priced_quote({
        "rfq_id": rfq_id, "priced_at": "2026-09-17T12:00:00Z", "status": "QUOTED",
        "reason_code": "QUOTED_OK", "response_action": "SELL", "response_price": .40,
        "size": 10, "size_unit": "shares", "fair": .40, "naive": .40,
        "corr_adjustment_bps": corr_bps, "side": "YES"}, "auto")


def test_correlation_lift_flags_degenerate_model(tmp_path):
    """A model whose adjustment is ~0 bps on nearly every quote is flagged."""
    path = tmp_path / "capture.db"
    quotes = QuoteSelectionStore(path)
    for i in range(20):
        _quoted_rfq(quotes, f"rfq-{i}", corr_bps=0.05)  # noise-level, like league_constant
    with connect_readonly(path) as conn:
        stats = correlation_lift(conn)
        assert stats["n"] == 20
        assert stats["mean_abs_bps"] == pytest.approx(0.05)
        assert stats["max_abs_bps"] == pytest.approx(0.05)
        assert stats["frac_degenerate"] == 1.0
        assert stats["degenerate"] is True
    quotes.close()


def test_correlation_lift_ignores_declines_and_null_adjustment(tmp_path):
    path = tmp_path / "capture.db"
    quotes = QuoteSelectionStore(path)
    quotes.record_priced_quote({
        "rfq_id": "rfq-declined", "priced_at": "2026-09-17T12:00:00Z", "status": "DECLINED",
        "reason_code": "NO_NFL_SAME_GAME"}, "auto")
    _quoted_rfq(quotes, "rfq-no-corr", corr_bps=None)
    with connect_readonly(path) as conn:
        stats = correlation_lift(conn)
        assert stats["n"] == 0
        assert stats["degenerate"] is None
    quotes.close()


def test_correlation_lift_not_flagged_when_model_moves_prices(tmp_path):
    """A model producing real, varied adjustments across most quotes is not flagged."""
    path = tmp_path / "capture.db"
    quotes = QuoteSelectionStore(path)
    for i in range(20):
        bps = 40.0 + i if i % 20 else 0.1  # one degenerate row, the rest well above threshold
        _quoted_rfq(quotes, f"rfq-{i}", corr_bps=bps)
    with connect_readonly(path) as conn:
        stats = correlation_lift(conn, degenerate_bps=1.0, degenerate_frac=0.95)
        assert stats["n"] == 20
        assert stats["frac_degenerate"] == pytest.approx(0.05)
        assert stats["degenerate"] is False
        assert stats["mean_abs_bps"] > 40
    quotes.close()

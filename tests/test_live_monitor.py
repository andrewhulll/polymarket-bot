"""LiveMonitor: poll -> store -> shadow engine, failures recorded not raised."""
from datetime import datetime, timedelta, timezone

from combo_mm.combo_markets import ComboMarketCatalog, parse_catalog_page
from combo_mm.config import PipelineConfig
from combo_mm.intl_gateway import map_rfq_request, map_rfq_trade
from combo_mm.live_monitor import LiveMonitor
from combo_mm.quote_selections import QuoteSelectionStore
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


def test_quote_latency_over_budget_is_flagged():
    """An RFQ decided 250ms after it posted breaches a 100ms budget."""
    store = EventStore()
    config = PipelineConfig(quote_latency_budget_ms=100)
    monitor = LiveMonitor(FakeSource([[_book("KC-ML", 0.60, 0.62), _book("KC-OVER", 0.49, 0.51),
                                       _rfq("R1")]]), store, config)
    monitor.poll_once(NOW + timedelta(milliseconds=250))
    lat = store.get_latency_stats()
    assert lat["count"] == 1
    assert lat["breaches"] == 1
    assert 240 <= lat["last_ms"] <= 260


def test_quote_latency_within_budget_is_not_flagged():
    store = EventStore()
    config = PipelineConfig(quote_latency_budget_ms=1000)
    monitor = LiveMonitor(FakeSource([[_book("KC-ML", 0.60, 0.62), _book("KC-OVER", 0.49, 0.51),
                                       _rfq("R1")]]), store, config)
    monitor.poll_once(NOW + timedelta(milliseconds=50))
    lat = store.get_latency_stats()
    assert lat["count"] == 1
    assert lat["breaches"] == 0


def test_source_failure_is_recorded_not_raised():
    class Boom(RuntimeError):
        pass

    monitor = LiveMonitor(FakeSource([Boom("secret-ish detail"), []], beta=False), EventStore())
    assert monitor.poll_once(NOW) == 0
    assert monitor.poll_errors == 1 and monitor.last_error == "Boom"
    assert monitor.rfq_beta_enabled is False
    monitor.poll_once(NOW)
    assert monitor.polls == 1 and monitor.last_error is None


# ---------------------------------------------------------------------------
# Gateway feed: leg screening, re-screening, and accepted quotes for picks
# ---------------------------------------------------------------------------

CATALOG_PAGE = {"markets": [
    {"id": "1", "condition_id": "0x1", "position_ids": ["100", "101"], "slug": "nfl-sea-ari-2026-09-20",
     "title": "Seahawks vs. Cardinals", "outcomes": ["Seahawks", "Cardinals"],
     "outcome_prices": ["0.655", "0.345"], "tags": ["sports", "nfl", "games"]},
    {"id": "2", "condition_id": "0x2", "position_ids": ["110", "111"],
     "slug": "nfl-sea-ari-2026-09-20-total-44pt5", "title": "Seahawks vs. Cardinals: O/U 44.5",
     "outcomes": ["Over", "Under"], "outcome_prices": ["0.5", "0.5"], "tags": ["sports", "nfl", "games"]},
    {"id": "3", "condition_id": "0x3", "position_ids": ["200", "201"], "slug": "lal-bet-get-2026-09-17-bet",
     "title": "Will Real Betis win on 2026-09-17?", "outcomes": ["Yes", "No"],
     "outcome_prices": ["0.585", "0.415"], "tags": ["sports", "soccer", "games"]},
]}


def _gateway_rfq(rfq_id, legs):
    return {"kind": "event", "raw": map_rfq_request({
        "type": "RFQ_REQUEST", "rfq_id": rfq_id, "requestor_public_id": "req_1",
        "leg_position_ids": legs, "condition_id": f"0x{rfq_id}", "yes_position_id": "y",
        "no_position_id": "n", "direction": "BUY", "side": "YES",
        "requested_size": {"unit": "notional", "value_e6": "5000000"},
        "submission_deadline": 1789700000000}, received_at=TS)}


def _gateway_trade(rfq_id, price_e6="412000"):
    return {"kind": "event", "raw": map_rfq_trade({
        "type": "RFQ_TRADE", "rfq_id": rfq_id, "requester_id": "req_1", "condition_id": f"0x{rfq_id}",
        "leg_position_ids": [], "direction": "BUY", "side": "YES", "price_e6": price_e6,
        "size_e6": "12000000", "executed_at": 1789700001000}, received_at=TS)}


def _catalog(with_page=True):
    catalog = ComboMarketCatalog(fetch_json=lambda url: {})
    if with_page:
        catalog.merge(parse_catalog_page(CATALOG_PAGE))
    return catalog


def test_gateway_rfqs_are_screened_with_their_extras():
    store = EventStore()
    source = FakeSource([[_gateway_rfq("Q1", ["101", "110", "200"]), _gateway_rfq("Q2", ["101", "200"])]])
    LiveMonitor(source, store, source_label="quoter gateway", catalog=_catalog()).poll_once(NOW)
    q1, q2 = store.get_rfq_screen("Q1"), store.get_rfq_screen("Q2")
    assert (q1["screen"], q1["rank"], q1["n_nfl_legs"]) == ("QUOTABLE", 0, 2)
    assert (q1["direction"], q1["side"], q1["submission_deadline"]) == ("BUY", "YES", "1789700000000")
    assert (q2["screen"], q2["rank"]) == ("NO_NFL_SAME_GAME", 1)
    assert q1["seq"] < q2["seq"]


def test_unresolved_rfqs_are_rescreened_when_the_catalog_grows():
    store = EventStore()
    catalog = _catalog(with_page=False)
    monitor = LiveMonitor(FakeSource([[_gateway_rfq("Q1", ["100", "111"])], []]), store,
                          catalog=catalog)
    monitor.RESCREEN_MIN_INTERVAL_S = 0.0
    monitor.poll_once(NOW)
    assert store.get_rfq_screen("Q1")["screen"] == "UNRESOLVED"
    catalog.merge(parse_catalog_page(CATALOG_PAGE))
    monitor.poll_once(NOW)
    row = store.get_rfq_screen("Q1")
    assert (row["screen"], row["n_resolved"], row["direction"]) == ("QUOTABLE", 2, "BUY")


def test_accepted_quote_stored_only_for_selected_rfqs():
    store, selections = EventStore(), QuoteSelectionStore()
    selections.select("Q1")
    source = FakeSource([[_gateway_rfq("Q1", ["100", "110"]), _gateway_rfq("Q2", ["100", "110"])],
                         [_gateway_trade("Q1"), _gateway_trade("Q2")]])
    monitor = LiveMonitor(source, store, catalog=_catalog(), selections=selections)
    monitor.poll_once(NOW)
    monitor.poll_once(NOW)
    rows = {r["rfq_id"]: r for r in selections.list_selected()}
    assert set(rows) == {"Q1"} and monitor.accepted_recorded == 1
    assert rows["Q1"]["accepted_price"] == 0.412 and rows["Q1"]["accepted_size"] == 12.0


def test_selecting_after_the_trade_still_records_the_accepted_quote():
    store, selections = EventStore(), QuoteSelectionStore()
    source = FakeSource([[_gateway_rfq("Q1", ["100", "110"]), _gateway_trade("Q1")]])
    monitor = LiveMonitor(source, store, catalog=_catalog(), selections=selections)
    monitor.poll_once(NOW)
    assert selections.list_selected() == []
    monitor.select("Q1", {"direction": "BUY", "screen": "QUOTABLE"})
    [row] = selections.list_selected()
    assert row["accepted_price"] == 0.412 and row["screen"] == "QUOTABLE"

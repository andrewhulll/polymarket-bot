"""Quote selections: accepted quotes are stored only for RFQs we picked."""
from combo_mm.quote_selections import QuoteSelectionStore

TRADE = {"rfq_id": "rfq-1", "price": "0.125", "size": "0.8", "direction": "BUY", "side": "YES",
         "requester_id": "req_9", "condition_id": "0xc", "executed_at": "2026-09-17T01:00:00Z"}


def test_trade_for_unselected_rfq_is_not_stored():
    store = QuoteSelectionStore()
    assert store.record_accepted(TRADE) is False
    assert store.list_selected() == []


def test_selected_rfq_gets_its_accepted_quote_once():
    store = QuoteSelectionStore()
    assert store.select("rfq-1", {"direction": "BUY", "size": 5.0, "size_unit": "notional",
                                  "legs": [{"label": "Seahawks vs. Cardinals → Cardinals"}]})
    assert store.select("rfq-1") is False
    assert store.record_accepted(TRADE) is True
    assert store.record_accepted(TRADE) is False
    [row] = store.list_selected()
    assert row["accepted_price"] == 0.125 and row["accepted_size"] == 0.8
    assert row["accepted_executed_at"] == "2026-09-17T01:00:00Z"
    assert row["legs"][0]["label"].startswith("Seahawks")


def test_unselect_drops_selection_and_accepted_quote():
    store = QuoteSelectionStore()
    store.select("rfq-1")
    store.record_accepted(TRADE)
    store.unselect("rfq-1")
    assert not store.is_selected("rfq-1") and store.list_selected() == []
    store.select("rfq-1")
    assert store.list_selected()[0]["accepted_price"] is None


def test_selections_survive_reopen(tmp_path):
    path = tmp_path / "live" / "sel.db"
    store = QuoteSelectionStore(path)
    store.select("rfq-1")
    store.record_accepted(TRADE)
    store.close()
    reopened = QuoteSelectionStore(path)
    assert reopened.is_selected("rfq-1")
    assert reopened.list_selected()[0]["accepted_price"] == 0.125

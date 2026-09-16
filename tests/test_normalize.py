"""normalize(): validation, coercion, event-key stability, state machines."""
import pytest

from combo_mm.events import (
    NormalizedEvent,
    deterministic_quote_id,
    quote_allows,
    quote_target_status,
    rfq_allows,
)
from combo_mm.normalize import NormalizeError, normalize

BASE = {
    "event_id": "evt-1",
    "event_type": "rfq_created",
    "rfq_id": "RFQ-1",
    "symbol": "POTUS-2028",
    "exchange_ts": "2026-09-16T14:00:00Z",
    "payload": {
        "qtyDecimal": "100",
        "creatorUserId": "user-1",
        "restRemainder": False,
        "comboLegs": [
            {"symbol": "LEG-A", "side": "YES"},
            {"symbol": "LEG-B", "side": "NO", "settlementPrice": "1.0"},
        ],
    },
}


def test_event_key_uses_raw_event_id():
    ev = normalize(dict(BASE))
    assert ev.event_key == "evt-1"


def test_event_key_stable_without_event_id():
    raw = dict(BASE)
    raw.pop("event_id")
    a = normalize(raw)
    b = normalize(dict(raw))
    assert a.event_key == b.event_key
    assert len(a.event_key) == 64  # sha256 hex
    # ... and differs when the content differs
    raw2 = dict(raw, rfq_id="RFQ-2")
    assert normalize(raw2).event_key != a.event_key


def test_qty_xor_cash_validation():
    both = dict(BASE, payload={**BASE["payload"], "cashOrderQty": "50"})
    with pytest.raises(NormalizeError):
        normalize(both)
    neither = dict(BASE, payload={"creatorUserId": "user-1"})
    with pytest.raises(NormalizeError):
        normalize(neither)
    cash_only = dict(BASE, payload={"cashOrderQty": "50"})
    ev = normalize(cash_only)
    assert ev.payload["cashOrderQty"] == "50"
    assert "qtyDecimal" not in ev.payload


def test_type_coercion():
    ev = normalize(dict(BASE))
    assert isinstance(ev, NormalizedEvent)
    assert ev.payload["qtyDecimal"] == "100"
    assert ev.payload["restRemainder"] == 0
    assert ev.payload["comboLegs"][0] == {
        "symbol": "LEG-A", "side": "YES", "settlementPrice": None,
    }
    assert ev.payload["comboLegs"][1]["settlementPrice"] == "1.0"
    assert ev.event_at == "2026-09-16T14:00:00Z"
    assert ev.received_at  # ingest time attached


def test_epoch_ms_exchange_ts():
    raw = dict(BASE, exchange_ts=1_789_567_200_000)
    ev = normalize(raw)
    assert ev.event_at.startswith("2026-09-16")


def test_unknown_event_type_rejected():
    with pytest.raises(NormalizeError):
        normalize(dict(BASE, event_type="nope"))


def test_bad_leg_side_rejected():
    raw = dict(BASE, payload={**BASE["payload"],
                              "comboLegs": [{"symbol": "X", "side": "MAYBE"}]})
    with pytest.raises(NormalizeError):
        normalize(raw)


def test_deterministic_quote_id():
    raw = {
        "event_id": "q1",
        "event_type": "quote_created",
        "rfq_id": "RFQ-9",
        "exchange_ts": "2026-09-16T14:00:00Z",
        "payload": {"creatorRfqUserId": "maker-001", "buyPrice": 0.6,
                    "sellPrice": 0.5, "buyQtyDecimal": "10",
                    "sellQtyDecimal": "10"},
    }
    ev = normalize(raw)
    assert ev.quote_id == deterministic_quote_id("maker-001", "RFQ-9")
    assert ev.quote_id == "maker-001:RFQ-9"


def test_fill_validation():
    raw = {
        "event_id": "f1",
        "event_type": "drop_copy_fill",
        "rfq_id": "RFQ-1",
        "symbol": "POTUS-2028",
        "exchange_ts": "2026-09-16T14:00:00Z",
        "payload": {"side": "BUY", "price": 0.53, "qty": "100",
                    "drop_copy_seq": "dc-1"},
    }
    ev = normalize(raw)
    assert ev.payload["side"] == "BUY"
    with pytest.raises(NormalizeError):
        normalize(dict(raw, payload={"side": "HOLD", "price": 0.5, "qty": "1"}))


# --- state machines ---------------------------------------------------------


def test_rfq_forward_transitions():
    assert rfq_allows(None, "OPEN")
    assert not rfq_allows(None, "QUOTED")
    assert rfq_allows("OPEN", "QUOTED")
    assert rfq_allows("OPEN", "CLOSED")      # public close straight from OPEN
    assert rfq_allows("QUOTED", "ACCEPTED")
    assert rfq_allows("ACCEPTED", "EXECUTED")  # monotonic jump ok


def test_rfq_no_regression_no_terminal_exit():
    assert not rfq_allows("CONFIRMED", "ACCEPTED")
    assert not rfq_allows("EXECUTED", "QUOTED")
    for terminal in ("CANCELLED", "EXPIRED", "CLOSED"):
        assert not rfq_allows(terminal, "OPEN")
        assert not rfq_allows(terminal, terminal)
    # any non-terminal may jump to terminal
    assert rfq_allows("QUOTED", "CANCELLED")
    assert rfq_allows("OPEN", "EXPIRED")


def test_quote_forward_transitions():
    assert quote_allows(None, "DRAFT")
    assert quote_allows(None, "ACTIVE")
    assert not quote_allows(None, "ACCEPTED")
    assert quote_allows("DRAFT", "ACTIVE")
    assert quote_allows("ACTIVE", "ACCEPTED")
    assert not quote_allows("CONFIRMED", "ACCEPTED")  # no regression


def test_quote_replacement_exception():
    # quote_draft_revised on a live quote -> REPLACED ...
    assert quote_target_status("quote_draft_revised", "ACTIVE") == "REPLACED"
    assert quote_target_status("quote_draft_revised", None) == "DRAFT"
    # ... and REPLACED -> ACTIVE is the one allowed non-monotonic move.
    assert quote_allows("REPLACED", "ACTIVE")
    assert not quote_allows("REPLACED", "DRAFT")
    assert not quote_allows("ACTIVE", "DRAFT")


def test_quote_terminal():
    assert quote_allows("ACTIVE", "DELETED")
    assert not quote_allows("DELETED", "ACTIVE")

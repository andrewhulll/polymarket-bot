"""Scripted fixture session for tests, replay, and the demo.

Wire shapes follow the Polymarket US gRPC contract (``polymarket.v1``);
see :mod:`combo_mm.events` for the contract notes. One deterministic session
(~11 RFQs) covering:

- RFQ-001: full lifecycle created -> draft revised -> quoted -> accepted ->
  confirmed -> executed. Its drop-copy fill lives in the SEPARATE drop-copy
  feed (fills reconcile via Drop Copy only); leg settlements arrive via a
  stream-invisible ``rfq_updated`` (the real contract exposes settlements
  only through ``GetRFQs``).
- RFQ-002: cancelled.  RFQ-003: expired.
- RFQ-004: race -- public ``rfq_closed`` arrives before private
  ``quote_accepted`` (RFQ stays CLOSED, quote still advances).
- RFQ-005: duplicate deliveries of the same events (same event_ids).
- RFQ-006: out-of-order -- ``quote_confirmed`` before ``quote_accepted``
  (the late accept must not regress state).
- Mid-stream disconnect at t=2200ms.
- RFQ-007: ``rfq_closed`` during the outage, invisible to the stream --
  only ``recovery_sync`` can catch it.
- RFQ-008: ``rfq_created`` during the outage, stream-invisible -- recovery
  must insert it.
- RFQ-009: created with no inline legs (reference fallback for the quoter).
- RFQ-010: cashOrderQty instead of qtyDecimal.
- RFQ-011: rfq_updated changing the leg list.
- Book snapshots per leg with timestamps (fresh around each RFQ's pricing).

Item schema: ``{"t": ms_offset, "kind": "event"|"book"|"disconnect", ...}``.
Event items: ``{"raw": {...}, "stream": True}`` (``stream=False`` = missed by
the stream but present in durable reads).

``build_drop_copy_feed()`` returns the execution-report records for the
session (modeled DropCopy shape -- see :mod:`combo_mm.dropcopy`), each with
a ``resume_token``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

__all__ = ["BASE_TS", "SELF_USER_ID", "build_session", "build_drop_copy_feed",
           "COMBOS"]

BASE_TS = datetime(2026, 9, 16, 14, 0, 0, tzinfo=timezone.utc)
SELF_USER_ID = "maker-001"

_LEGS = {
    "POTUS-2028": [
        {"symbol": "POTUS-2028-DEM", "side": "YES"},
        {"symbol": "POTUS-2028-GOP", "side": "NO"},
    ],
    "SENATE-2026": [
        {"symbol": "SEN-2026-D", "side": "YES"},
        {"symbol": "SEN-2026-R", "side": "NO"},
    ],
    "HOUSE-2026": [
        {"symbol": "HOU-2026-D", "side": "YES"},
        {"symbol": "HOU-2026-R", "side": "NO"},
    ],
}

COMBOS: List[Dict[str, Any]] = [
    {
        "symbol": sym,
        "tick_size": 0.001,
        "price_min": 0.001,
        "price_max": 0.999,
        "min_qty": 1.0,
        "legs": legs,
    }
    for sym, legs in _LEGS.items()
]


def _ts(t_ms: int) -> str:
    return (BASE_TS + timedelta(milliseconds=t_ms)).isoformat().replace("+00:00", "Z")


def _ev(event_id: str, event_type: str, rfq_id: str, t: int,
        stream: bool = True, **kw: Any) -> Dict[str, Any]:
    raw: Dict[str, Any] = {
        "event_id": event_id,
        "event_type": event_type,
        "rfq_id": rfq_id,
        "exchange_ts": _ts(t),
    }
    raw.update(kw)
    return {"t": t, "kind": "event", "stream": stream, "raw": raw}


def _rfq_payload(rfq_id: str, t: int, symbol: str, creator: str,
                 legs: List[Dict[str, Any]], status: str = "OPEN",
                 **size: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": rfq_id,
        "symbol": symbol,
        "rfqCreatorUserId": creator,
        "createdTime": _ts(t),
        "updatedTime": _ts(t),
        "restRemainder": False,
        "status": status,
        "comboLegs": legs,
    }
    payload.update(size)
    return payload


def _quote_payload(quote_id: str, rfq_id: str, t: int, symbol: str,
                   buy: str, sell: str, qty: str,
                   status: str = "ACTIVE", **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": quote_id,
        "rfqId": rfq_id,
        "creatorRfqUserId": SELF_USER_ID,
        "symbol": symbol,
        "status": status,
        "createdTime": _ts(t),
        "updatedTime": _ts(t),
        "buyPrice": buy,       # requester buys @ buyPrice = our ask
        "sellPrice": sell,     # requester sells @ sellPrice = our bid
        "restRemainder": False,
        "postOnly": True,
        "rfqCreatorUserId": "user-42",
        "buyQtyDecimal": qty,
        "sellQtyDecimal": qty,
    }
    payload.update(extra)
    return payload


def _book(t: int, symbol: str, bid: float, ask: float,
          size: float = 1000.0, seq: int = 0) -> Dict[str, Any]:
    return {
        "t": t,
        "kind": "book",
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "bid_size": size,
        "ask_size": size,
        "seq": seq,
        "ts": _ts(t),
    }


def _book_set(t: int, marks: Dict[str, tuple], seq_base: int = 0) -> List[Dict[str, Any]]:
    return [
        _book(t, sym, bid, ask, seq=seq_base + i)
        for i, (sym, (bid, ask)) in enumerate(marks.items())
    ]


def build_session() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (session_items, combos). Session is sorted by t."""
    s: List[Dict[str, Any]] = []

    # Initial books for every leg.
    s += _book_set(0, {
        "POTUS-2028-DEM": (0.52, 0.54),
        "POTUS-2028-GOP": (0.44, 0.46),
        "SEN-2026-D": (0.58, 0.60),
        "SEN-2026-R": (0.38, 0.40),
        "HOU-2026-D": (0.50, 0.52),
        "HOU-2026-R": (0.46, 0.48),
    })

    # RFQ-001: full lifecycle ------------------------------------------------
    s.append(_ev("evt-001", "rfq_created", "RFQ-001", 100, symbol="POTUS-2028",
                 payload=_rfq_payload("RFQ-001", 100, "POTUS-2028", "user-42",
                                      _LEGS["POTUS-2028"], qtyDecimal="100")))
    s.append(_ev("evt-002", "quote_draft_revised", "RFQ-001", 150,
                 quote_id="q-0001",
                 payload=_quote_payload("q-0001", "RFQ-001", 150, "POTUS-2028",
                                        "0.55", "0.51", "100", status="DRAFT")))
    s.append(_ev("evt-003", "quote_created", "RFQ-001", 200, quote_id="q-0001",
                 payload=_quote_payload("q-0001", "RFQ-001", 200, "POTUS-2028",
                                        "0.56", "0.50", "100")))
    # Replace: revised draft supersedes the live quote, then quote_created
    # (fired on create OR replace) brings the replacement live.
    s.append(_ev("evt-004", "quote_draft_revised", "RFQ-001", 250,
                 quote_id="q-0001",
                 payload=_quote_payload("q-0001", "RFQ-001", 250, "POTUS-2028",
                                        "0.57", "0.49", "100", status="DRAFT")))
    s.append(_ev("evt-005", "quote_created", "RFQ-001", 300, quote_id="q-0001",
                 payload=_quote_payload("q-0001", "RFQ-001", 300, "POTUS-2028",
                                        "0.57", "0.49", "100")))
    s.append(_ev("evt-006", "quote_accepted", "RFQ-001", 400, quote_id="q-0001",
                 payload=_quote_payload(
                     "q-0001", "RFQ-001", 400, "POTUS-2028",
                     "0.57", "0.49", "100", status="ACCEPTED",
                     acceptedSide="BUY", acceptedTime=_ts(400),
                     # last-look deadline well after the session
                     confirmationDeadline=_ts(3_600_000))))
    s.append(_ev("evt-007", "quote_confirmed", "RFQ-001", 500, quote_id="q-0001",
                 payload=_quote_payload(
                     "q-0001", "RFQ-001", 500, "POTUS-2028",
                     "0.57", "0.49", "100", status="CONFIRMED",
                     confirmedTime=_ts(500),
                     executionDeadline=_ts(3_600_000))))
    s.append(_ev("evt-008", "quote_executed", "RFQ-001", 600, quote_id="q-0001",
                 payload=_quote_payload(
                     "q-0001", "RFQ-001", 600, "POTUS-2028",
                     "0.57", "0.49", "100", status="EXECUTED",
                     orderId="ord-0001", clientOrderId="cli-0001",
                     executedTime=_ts(600))))
    # No drop_copy_fill on this stream: fills reconcile via Drop Copy only.
    # No settlement event either: leg settlements are picked up via GetRFQs.
    # This stream-invisible rfq_updated models exactly that -- the durable
    # read sees settled legs, the stream never does.
    s.append(_ev("evt-010", "rfq_updated", "RFQ-001", 700, symbol="POTUS-2028",
                 stream=False,
                 payload={
                     "id": "RFQ-001",
                     "updatedTime": _ts(700),
                     "comboLegs": [
                         {"symbol": "POTUS-2028-DEM", "side": "YES",
                          "settlementPrice": "1"},
                         {"symbol": "POTUS-2028-GOP", "side": "NO",
                          "settlementPrice": "0"},
                     ],
                 }))

    # RFQ-002: cancelled ------------------------------------------------------
    s.append(_ev("evt-011", "rfq_created", "RFQ-002", 800, symbol="POTUS-2028",
                 payload=_rfq_payload("RFQ-002", 800, "POTUS-2028", "user-43",
                                      _LEGS["POTUS-2028"], qtyDecimal="50",
                                      restRemainder=True)))
    s.append(_ev("evt-012", "rfq_cancelled", "RFQ-002", 900,
                 payload={"id": "RFQ-002", "status": "CANCELLED",
                          "updatedTime": _ts(900)}))

    # RFQ-003: expired (stream-visible here; the real stream never emits it --
    # expirations are client-derived. Kept visible so the fixture exercises
    # the terminal path; the client-derived path is covered by sweep tests.)
    s.append(_ev("evt-013", "rfq_created", "RFQ-003", 1000, symbol="SENATE-2026",
                 payload=_rfq_payload("RFQ-003", 1000, "SENATE-2026", "user-44",
                                      _LEGS["SENATE-2026"], cashOrderQty="500")))
    s.append(_ev("evt-014", "rfq_expired", "RFQ-003", 1100,
                 payload={"id": "RFQ-003", "status": "EXPIRED",
                          "updatedTime": _ts(1100)}))

    # RFQ-004: race -- rfq_closed before quote_accepted -------------------------
    s.append(_ev("evt-015", "rfq_created", "RFQ-004", 1200, symbol="POTUS-2028",
                 payload=_rfq_payload("RFQ-004", 1200, "POTUS-2028", "user-45",
                                      _LEGS["POTUS-2028"], qtyDecimal="25")))
    s.append(_ev("evt-016", "quote_created", "RFQ-004", 1300, quote_id="q-0004",
                 payload=_quote_payload("q-0004", "RFQ-004", 1300, "POTUS-2028",
                                        "0.56", "0.50", "25")))
    s.append(_ev("evt-017", "rfq_closed", "RFQ-004", 1400,
                 payload={"id": "RFQ-004", "status": "CLOSED",
                          "updatedTime": _ts(1400)}))
    s.append(_ev("evt-018", "quote_accepted", "RFQ-004", 1500, quote_id="q-0004",
                 payload=_quote_payload(
                     "q-0004", "RFQ-004", 1500, "POTUS-2028",
                     "0.56", "0.50", "25", status="ACCEPTED",
                     acceptedSide="BUY", acceptedTime=_ts(1500),
                     confirmationDeadline=_ts(3_600_000))))

    # RFQ-005: duplicate deliveries --------------------------------------------
    dup_created = _rfq_payload("RFQ-005", 1600, "HOUSE-2026", "user-46",
                              _LEGS["HOUSE-2026"], qtyDecimal="75")
    dup_quote = _quote_payload("q-0005", "RFQ-005", 1700, "HOUSE-2026",
                               "0.54", "0.48", "75")
    s.append(_ev("evt-019", "rfq_created", "RFQ-005", 1600, symbol="HOUSE-2026",
                 payload=dup_created))
    s.append(_ev("evt-020", "quote_created", "RFQ-005", 1700, quote_id="q-0005",
                 payload=dup_quote))
    s.append(_ev("evt-019", "rfq_created", "RFQ-005", 1750, symbol="HOUSE-2026",
                 payload=dup_created))
    s.append(_ev("evt-020", "quote_created", "RFQ-005", 1760, quote_id="q-0005",
                 payload=dup_quote))

    # RFQ-006: out-of-order -- confirmed before accepted -------------------------
    s.append(_ev("evt-021", "rfq_created", "RFQ-006", 1800, symbol="POTUS-2028",
                 payload=_rfq_payload("RFQ-006", 1800, "POTUS-2028", "user-47",
                                      _LEGS["POTUS-2028"], qtyDecimal="40")))
    s.append(_ev("evt-022", "quote_created", "RFQ-006", 1900, quote_id="q-0006",
                 payload=_quote_payload("q-0006", "RFQ-006", 1900, "POTUS-2028",
                                        "0.56", "0.50", "40")))
    s.append(_ev("evt-023", "quote_confirmed", "RFQ-006", 2000, quote_id="q-0006",
                 payload=_quote_payload(
                     "q-0006", "RFQ-006", 2000, "POTUS-2028",
                     "0.56", "0.50", "40", status="CONFIRMED",
                     confirmedTime=_ts(2000),
                     executionDeadline=_ts(3_600_000))))
    s.append(_ev("evt-024", "quote_accepted", "RFQ-006", 2100, quote_id="q-0006",
                 payload=_quote_payload(
                     "q-0006", "RFQ-006", 2100, "POTUS-2028",
                     "0.56", "0.50", "40", status="ACCEPTED",
                     acceptedSide="BUY", acceptedTime=_ts(2100),
                     confirmationDeadline=_ts(3_600_000))))

    # Mid-stream disconnect ------------------------------------------------------
    s.append({"t": 2200, "kind": "disconnect"})

    # Fresh SEN books, then RFQ-007 (close missed by the stream) -------------------
    s += _book_set(2250, {"SEN-2026-D": (0.59, 0.61), "SEN-2026-R": (0.37, 0.39)},
                   seq_base=10)
    s.append(_ev("evt-025", "rfq_created", "RFQ-007", 2300, symbol="SENATE-2026",
                 payload=_rfq_payload("RFQ-007", 2300, "SENATE-2026", "user-48",
                                      _LEGS["SENATE-2026"], qtyDecimal="60")))
    s.append(_ev("evt-026", "quote_created", "RFQ-007", 2350, quote_id="q-0007",
                 payload=_quote_payload("q-0007", "RFQ-007", 2350, "SENATE-2026",
                                        "0.60", "0.54", "60")))
    s.append(_ev("evt-027", "rfq_closed", "RFQ-007", 2400, stream=False,
                 payload={"id": "RFQ-007", "status": "CLOSED",
                          "updatedTime": _ts(2400)}))

    # Fresh HOU books, then RFQ-008 (creation missed by the stream) -------------------
    s += _book_set(2440, {"HOU-2026-D": (0.51, 0.53), "HOU-2026-R": (0.45, 0.47)},
                   seq_base=20)
    s.append(_ev("evt-028", "rfq_created", "RFQ-008", 2450, symbol="HOUSE-2026",
                 stream=False,
                 payload=_rfq_payload("RFQ-008", 2450, "HOUSE-2026", "user-49",
                                      _LEGS["HOUSE-2026"], qtyDecimal="30")))
    # A second, stream-visible RFQ-008 event the recovery path must not double-apply.
    s.append(_ev("evt-028b", "rfq_updated", "RFQ-008", 2460, symbol="HOUSE-2026",
                 payload={"id": "RFQ-008", "updatedTime": _ts(2460),
                          "comboLegs": _LEGS["HOUSE-2026"]}))

    # Fresh POTUS books, then RFQ-009 (no inline legs -> reference fallback) ------
    s += _book_set(2550, {"POTUS-2028-DEM": (0.53, 0.55), "POTUS-2028-GOP": (0.43, 0.45)},
                   seq_base=30)
    s.append(_ev("evt-029", "rfq_created", "RFQ-009", 2600, symbol="POTUS-2028",
                 payload=_rfq_payload("RFQ-009", 2600, "POTUS-2028", "user-50",
                                      [], qtyDecimal="20")))

    # Fresh SEN books, then RFQ-010 (cash size mode) ---------------------------------
    s += _book_set(2650, {"SEN-2026-D": (0.60, 0.62), "SEN-2026-R": (0.36, 0.38)},
                   seq_base=40)
    s.append(_ev("evt-030", "rfq_created", "RFQ-010", 2700, symbol="SENATE-2026",
                 payload=_rfq_payload("RFQ-010", 2700, "SENATE-2026", "user-51",
                                      _LEGS["SENATE-2026"], cashOrderQty="500")))

    # Fresh POTUS books incl. a new leg, then RFQ-011 (leg list updated) ---------------
    s += _book_set(2750, {
        "POTUS-2028-DEM": (0.54, 0.56),
        "POTUS-2028-GOP": (0.42, 0.44),
        "POTUS-2028-IND": (0.08, 0.10),
    }, seq_base=50)
    three_legs = _LEGS["POTUS-2028"] + [{"symbol": "POTUS-2028-IND", "side": "YES"}]
    s.append(_ev("evt-031", "rfq_created", "RFQ-011", 2800, symbol="POTUS-2028",
                 payload=_rfq_payload("RFQ-011", 2800, "POTUS-2028", "user-52",
                                      _LEGS["POTUS-2028"], qtyDecimal="35")))
    s.append(_ev("evt-032", "rfq_updated", "RFQ-011", 2900, symbol="POTUS-2028",
                 payload={"id": "RFQ-011", "updatedTime": _ts(2900),
                          "comboLegs": three_legs}))

    return sorted(s, key=lambda i: i["t"]), COMBOS


def build_drop_copy_feed() -> List[Dict[str, Any]]:
    """Modeled DropCopy execution reports for the session (see dropcopy.py).

    The taker bought RFQ-001 (acceptedSide BUY) against our ask: our fill is
    SELL 100 @ 0.57. A second record redelivers the same execution under a new
    resume token (drop-copy redelivery) to exercise idempotency.
    """
    return [
        {
            "fillId": "fill-0001",
            "dropCopySeq": "dc-0001",
            "rfqId": "RFQ-001",
            "quoteId": "q-0001",
            "symbol": "POTUS-2028",
            "side": "SELL",
            "price": "0.57",
            "qty": "100",
            "executedTime": _ts(650),
            "resume_token": "dc-0001",
        },
        {
            "fillId": "fill-0001",
            "dropCopySeq": "dc-0001",
            "rfqId": "RFQ-001",
            "quoteId": "q-0001",
            "symbol": "POTUS-2028",
            "side": "SELL",
            "price": "0.57",
            "qty": "100",
            "executedTime": _ts(650),
            "resume_token": "dc-0002",  # redelivery under a new token
        },
    ]

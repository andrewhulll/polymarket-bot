"""Canonical event model and state machines for the combo RFQ pipeline.

This module mirrors the Polymarket US gRPC contract (``polymarket.v1``,
``RFQAPI.StreamRFQEvents`` / ``DropCopyAPI``) as documented at
https://docs.polymarket.us/grpc-api/overview. Where the contract is silent
(e.g. the stream's envelope framing, exact ``Quote.status`` vocabulary), the
modeling choice is marked ``MODELING`` below.

Wire shapes (exact field names from the contract)
------------------------------------------------
- ``rfq_created`` (public): ``RFQ{id, qtyDecimal | cashOrderQty (mutually
  exclusive, optional strings), symbol, rfqCreatorUserId, createdTime,
  restRemainder, status (OPEN/CLOSED), updatedTime, comboLegs[ordered]:
  [{symbol, side, settlementPrice?}]}``.
- ``rfq_closed`` (public): RFQ deleted OR a quote was accepted. Treat as
  "stop quoting", NOT as "no quote was accepted" -- keep local quote state
  until the private quote event arrives or ``GetQuotes`` reconciliation
  resolves it.
- ``quote_created`` (visible to requester + quote creator only): fired on
  create OR replace. ``Quote{id, rfqId, creatorRfqUserId, symbol, status,
  createdTime, buyPrice, sellPrice, restRemainder, postOnly, rfqCreatorUserId,
  rfqCashOrderQty, buyQtyDecimal, sellQtyDecimal, updatedTime}``.
  Semantics: ``buyPrice`` = price at which the REQUESTER buys (maker sells =
  our ask); ``sellPrice`` = price at which the REQUESTER sells (maker buys =
  our bid).
- ``quote_deleted`` (requester + quote creator only).
- ``quote_accepted`` (requester + SELECTED quote creator only): adds
  ``acceptedSide, acceptedTime, confirmationDeadline``. The
  ``confirmationDeadline`` is the AUTHORITATIVE last-look deadline for
  ConfirmQuote/DeleteQuote -- never use a client-side timer for the action
  itself (we only *observe* its passing via the deadline sweeper).
- ``quote_confirmed``: adds ``confirmedTime, executionDeadline`` (scheduled
  paired-order submission time).
- ``quote_executed``: adds ``orderId / clientOrderId`` (recipient-specific),
  ``executedTime``. This means paired orders were ACCEPTED FOR SUBMISSION --
  NOT filled. Fills reconcile via Drop Copy only.

Contract notes that shape this model
------------------------------------
- ``settlementPrice`` is the raw YES/LONG result in [0,1] -- NEVER inverted
  for SELL-side legs at ingest. ``"0"`` is a valid settled price; an ABSENT
  field means no valid settlement. There is NO separate event when a leg
  later settles -- the latest settlement is picked up via ``GetRFQs`` during
  reconciliation (recovery syncs legs when the durable ``updatedTime`` is
  newer than local). Historical RFQs may carry NO ``comboLegs`` at all.
- The public stream does NOT emit expiration / done-away / pending-risk
  events. Expirations are derived client-side from deadlines (see
  :meth:`combo_mm.store.EventStore.sweep_deadlines`); ``rfq_expired`` events
  produced that way are marked ``client_derived=True`` on
  :class:`NormalizedEvent` and must never be mistaken for stream events.
- ``StreamRFQEvents`` takes an EMPTY request (no filters) and delivers only
  NEW events (no replay). No ordering guarantee across publishers/reconnects;
  duplicates must be tolerated. Hence idempotent apply is load-bearing:
  ``event_id`` dedup when present, AND per-entity ``updatedTime`` monotonic
  apply (a state change is projected only if its ``updatedTime`` is newer
  than the stored row's).
- MODELING: the stream envelope ``{event_id?, event_type, exchange_ts,
  rfq_id, quote_id?, symbol?, payload}`` is ours -- the real framing is
  defined by the proto bundle (TODO: fetch "Polymarket - Proto Files.zip";
  the Google Drive download failed, so the client is hand-modeled from the
  docs). ``exchange_ts`` stands in for ``createdTime``/``updatedTime``.
- MODELING: leg ``side`` vocabulary (``YES``/``NO``) follows our sim; the
  contract does not enumerate it.

RFQ lifecycle
-------------
``OPEN -> QUOTED -> ACCEPTED -> CONFIRMED -> EXECUTED``
Terminal: ``CANCELLED``, ``EXPIRED``, ``CLOSED``. ``rfq_closed`` may arrive
straight from ``OPEN`` (the public close can precede the private accept flow).
No transitions are allowed out of a terminal state; out-of-order or late events
that would regress state are logged and ignored (idempotent no-op).

Quote lifecycle
---------------
``DRAFT -> ACTIVE -> ACCEPTED -> CONFIRMED -> EXECUTED``
Terminal: ``DELETED``, ``EXPIRED``.

Documented exception (the ONE allowed non-monotonic transition):
a ``quote_draft_revised`` arriving while a quote is ``ACTIVE`` supersedes the
live quote, moving it to ``REPLACED``; the follow-up ``quote_created`` for the
replacement (the real contract fires ``quote_created`` on create OR replace)
then moves ``REPLACED -> ACTIVE``. Same-rank, explicitly allowed.

Quote identity is the exchange-assigned ``Quote.id`` when present, falling
back to deterministic ``f"{creatorRfqUserId}:{rfqId}"`` (one quote row per
maker per RFQ).

Documented race: the public ``rfq_closed`` may arrive before the private
``quote_accepted``. The RFQ row is terminal and stays terminal; quote rows are
still advanced by their own machine ("stop creating/replacing quotes for that
RFQ but retain quote state").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

RFQ_EVENT_TYPES = (
    "rfq_created",      # public: new combo RFQ opened
    "rfq_updated",      # RFQ update incl. leg-list changes (no status change)
    "rfq_cancelled",    # terminal
    "rfq_expired",      # terminal; CLIENT-DERIVED when client_derived=True
    "rfq_closed",       # public close, terminal (no selection info)
)

QUOTE_EVENT_TYPES = (
    "quote_draft_revised",  # internal draft revision (paper quoting flow)
    "quote_created",        # quote goes live; also fired on REPLACE
    "quote_deleted",        # terminal
    "quote_accepted",       # private: starts last look
    "quote_confirmed",
    "quote_executed",       # paired orders accepted for submission (NOT a fill)
)

POST_TRADE_EVENTS = (
    # No "settlement" event: leg settlements arrive via GetRFQs (settlementPrice
    # on comboLegs) and are picked up by recovery's leg sync.
    "drop_copy_fill",   # canonical fill record -- Drop Copy ONLY source of fills
)

ALL_EVENT_TYPES = RFQ_EVENT_TYPES + QUOTE_EVENT_TYPES + POST_TRADE_EVENTS

# ---------------------------------------------------------------------------
# RFQ state machine
# ---------------------------------------------------------------------------

RFQ_OPEN = "OPEN"
RFQ_QUOTED = "QUOTED"
RFQ_ACCEPTED = "ACCEPTED"
RFQ_CONFIRMED = "CONFIRMED"
RFQ_EXECUTED = "EXECUTED"
RFQ_CANCELLED = "CANCELLED"
RFQ_EXPIRED = "EXPIRED"
RFQ_CLOSED = "CLOSED"

RFQ_TERMINAL_STATUSES = frozenset({RFQ_CANCELLED, RFQ_EXPIRED, RFQ_CLOSED})

_RFQ_RANK = {
    RFQ_OPEN: 0,
    RFQ_QUOTED: 1,
    RFQ_ACCEPTED: 2,
    RFQ_CONFIRMED: 3,
    RFQ_EXECUTED: 4,
    RFQ_CANCELLED: 5,
    RFQ_EXPIRED: 5,
    RFQ_CLOSED: 5,
}

#: Event type -> RFQ status it drives (events absent here cause no RFQ change).
RFQ_EVENT_STATUS = {
    "rfq_created": RFQ_OPEN,
    "quote_created": RFQ_QUOTED,
    "quote_accepted": RFQ_ACCEPTED,
    "quote_confirmed": RFQ_CONFIRMED,
    "quote_executed": RFQ_EXECUTED,
    "rfq_cancelled": RFQ_CANCELLED,
    "rfq_expired": RFQ_EXPIRED,
    "rfq_closed": RFQ_CLOSED,
}


def rfq_allows(current: Optional[str], target: str) -> bool:
    """Return True if the RFQ may move from ``current`` to ``target``.

    Rules: nothing leaves a terminal state; any non-terminal state may jump to
    a terminal state (CLOSED may arrive directly from OPEN); otherwise movement
    must be monotonic (no regression).
    """
    if target not in _RFQ_RANK:
        return False
    if current is None:
        # Only creation may bootstrap an RFQ row.
        return target == RFQ_OPEN
    if current in RFQ_TERMINAL_STATUSES:
        return False
    if target in RFQ_TERMINAL_STATUSES:
        return True
    return _RFQ_RANK[target] >= _RFQ_RANK[current]


# ---------------------------------------------------------------------------
# Quote state machine
# ---------------------------------------------------------------------------

QUOTE_DRAFT = "DRAFT"
QUOTE_ACTIVE = "ACTIVE"
QUOTE_REPLACED = "REPLACED"
QUOTE_ACCEPTED = "ACCEPTED"
QUOTE_CONFIRMED = "CONFIRMED"
QUOTE_EXECUTED = "EXECUTED"
QUOTE_DELETED = "DELETED"
QUOTE_EXPIRED = "EXPIRED"

QUOTE_TERMINAL_STATUSES = frozenset({QUOTE_DELETED, QUOTE_EXPIRED})

_QUOTE_RANK = {
    QUOTE_DRAFT: 0,
    QUOTE_ACTIVE: 1,
    QUOTE_REPLACED: 1,  # same rank: superseded live quote awaiting its replacement
    QUOTE_ACCEPTED: 2,
    QUOTE_CONFIRMED: 3,
    QUOTE_EXECUTED: 4,
    QUOTE_DELETED: 5,
    QUOTE_EXPIRED: 5,
}

#: Event type -> quote status it drives (absent events cause no quote change).
QUOTE_EVENT_STATUS = {
    "quote_created": QUOTE_ACTIVE,
    "quote_accepted": QUOTE_ACCEPTED,
    "quote_confirmed": QUOTE_CONFIRMED,
    "quote_executed": QUOTE_EXECUTED,
    "quote_deleted": QUOTE_DELETED,
    # quote_draft_revised is special-cased in quote_target_status().
}


def quote_target_status(event_type: str, current: Optional[str]) -> Optional[str]:
    """Resolve the quote status an event drives, given the current status.

    ``quote_draft_revised`` maps to ``DRAFT`` for a fresh/absent quote, to
    ``REPLACED`` when it supersedes a live (``ACTIVE``) quote, and back to
    ``ACTIVE`` when the revision lands on an already-``REPLACED`` quote --
    the documented replacement exception: the revised draft IS the
    replacement going live.
    """
    if event_type == "quote_draft_revised":
        if current == QUOTE_ACTIVE:
            return QUOTE_REPLACED
        if current == QUOTE_REPLACED:
            return QUOTE_ACTIVE
        return QUOTE_DRAFT
    return QUOTE_EVENT_STATUS.get(event_type)


def quote_allows(current: Optional[str], target: str) -> bool:
    """Return True if the quote may move from ``current`` to ``target``.

    Monotonic except the documented replacement exception: a live quote may
    be superseded (``ACTIVE -> REPLACED``) by a revised draft, and the
    replacement goes live again (``REPLACED -> ACTIVE``). Both directions are
    same-rank, explicitly allowed. Same-state transitions are allowed as
    idempotent refreshes.
    """
    if target not in _QUOTE_RANK:
        return False
    if current is None:
        return target in (QUOTE_DRAFT, QUOTE_ACTIVE)
    if current in QUOTE_TERMINAL_STATUSES:
        return False
    if target in QUOTE_TERMINAL_STATUSES:
        return True
    if current == target:
        return True
    if {current, target} == {QUOTE_ACTIVE, QUOTE_REPLACED}:
        return True  # documented exception: supersede / replacement goes live
    return _QUOTE_RANK[target] > _QUOTE_RANK[current]


def deterministic_quote_id(maker_user_id: str, rfq_id: str) -> str:
    """Fallback quote identity when the exchange id is absent.

    One quote row per maker per RFQ.
    """
    return f"{maker_user_id}:{rfq_id}"


# ---------------------------------------------------------------------------
# Normalized event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedEvent:
    """A validated, type-coerced pipeline event.

    ``event_key`` is the idempotency key: the raw ``event_id`` when present,
    else a stable SHA-256 over ``event_type|rfq_id|quote_id|exchange_ts``.
    ``received_at`` is ingest time; ``event_at`` is exchange time (the wire
    ``createdTime``/``updatedTime``). ``payload`` carries the EXACT wire field
    names (``buyPrice``, ``qtyDecimal``, ``comboLegs`` ...).

    ``client_derived`` marks events the pipeline synthesized itself (currently
    only ``rfq_expired`` from the deadline sweeper) as opposed to exchange
    stream messages. The raw-first log records both; the flag keeps them
    distinguishable for audit and debugging.
    """

    event_key: str
    event_type: str
    rfq_id: Optional[str]
    quote_id: Optional[str]
    symbol: Optional[str]
    event_at: str          # exchange timestamp, ISO-8601 UTC
    received_at: str       # ingest timestamp, ISO-8601 UTC
    payload: Dict[str, Any] = field(default_factory=dict)
    client_derived: bool = False

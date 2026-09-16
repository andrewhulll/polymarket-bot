"""Transport adapters: the RFQ stream contract.

``RfqTransport`` mirrors the Polymarket US gRPC contract (``polymarket.v1``)
as documented at https://docs.polymarket.us/grpc-api/overview:

- ``RFQAPI.StreamRFQEvents``: server-side streaming, EMPTY request message
  (no filters). Auth scope ``read:orders`` goes in the ``authorization``
  gRPC metadata (Bearer token).
- Rate limit: 1 new stream per second per firm (burst 1) -- the consumer
  spaces stream opens accordingly; never hammer on reconnect.
- No replay: a new stream delivers only NEW events. No ordering guarantee
  across publishers/reconnects. Duplicates must be tolerated. There is no
  gap-free handoff between the durable reads and the subscription -- hence
  idempotent apply (``event_id`` dedup + per-entity ``updatedTime``
  monotonicity) is load-bearing.

Two implementations ship:

- :class:`SimulatedTransport`: in-memory fake exchange driven by a scripted
  session. Supports injected disconnects and stream-invisible events (present
  in durable reads, absent from the stream) so reconnect/recovery paths are
  exercisable without any network. Its cursor resumes after the last consumed
  position, which models "new stream delivers only new events".
- :class:`GrpcTransport`: stub. Every method raises ``NotImplementedError``
  naming exactly what is missing. No network code exists here by design.

No implementation in this module performs network I/O.
"""
from __future__ import annotations

import abc
import logging
from typing import Any, Dict, Iterator, List, Optional

from combo_mm.events import (
    RFQ_TERMINAL_STATUSES,
    RFQ_EVENT_STATUS,
    QUOTE_EVENT_STATUS,
    quote_target_status,
    rfq_allows,
)

log = logging.getLogger(__name__)

__all__ = [
    "StreamDisconnected",
    "RfqTransport",
    "SimulatedTransport",
    "GrpcTransport",
]


class StreamDisconnected(Exception):
    """Raised by ``stream_rfq_events`` when the stream drops."""


class RfqTransport(abc.ABC):
    """Abstract RFQ transport (mirrors ``polymarket.v1.RFQAPI``).

    Authentication: the access token goes in the ``authorization`` gRPC
    metadata as ``Bearer <token>`` with scope ``read:orders`` (see
    :mod:`combo_mm.auth`). ``PERMISSION_DENIED`` means a missing scope and is
    a fatal config error, not a retry case.
    """

    @abc.abstractmethod
    def get_rfq_user_id(self) -> str:
        """Our maker user id (used for quote identity + durable reads)."""

    @abc.abstractmethod
    def stream_rfq_events(self) -> Iterator[Dict[str, Any]]:
        """Open the RFQ event stream (``StreamRFQEvents``).

        The request message is EMPTY -- no filters. Yields raw stream items
        (event dicts and book snapshots). May raise
        :class:`StreamDisconnected` at any point. The generator is
        single-use; open a fresh one after a reconnect, respecting the
        1-stream-per-second-per-firm limit.
        """

    @abc.abstractmethod
    def get_rfqs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Durable read of RFQs (``GetRFQs``).

        ``status="OPEN"`` means actionable (non-terminal) RFQs -- the real
        call may need to cover our local QUOTED/ACCEPTED/... states; the
        adapter must map this to whatever query returns every RFQ that is not
        terminal. An empty list is a valid snapshot, not an error.
        """

    @abc.abstractmethod
    def get_quotes(self, user_filter: Any = None) -> List[Dict[str, Any]]:
        """Durable read of quotes (``GetQuotes``); ``user_filter="SELF"`` = ours.

        This is the durable recovery path for quote execution state. An empty
        list is a valid snapshot, not an error.
        """

    @abc.abstractmethod
    def get_combos(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Durable read of combo reference metadata."""


def _durable_rfq_states(session: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Fold ALL session events (including stream-invisible ones) into RFQ state."""
    states: Dict[str, Dict[str, Any]] = {}
    for item in sorted(session, key=lambda i: i.get("t", 0)):
        if item.get("kind") != "event":
            continue
        raw = item["raw"]
        etype, rfq_id = raw.get("event_type"), raw.get("rfq_id")
        if not rfq_id:
            continue
        payload = raw.get("payload", {})
        if etype == "rfq_created":
            states[rfq_id] = {
                "rfq_id": rfq_id,
                "symbol": raw.get("symbol") or payload.get("symbol"),
                "status": payload.get("status", "OPEN"),
                "qtyDecimal": payload.get("qtyDecimal"),
                "cashOrderQty": payload.get("cashOrderQty"),
                "rfqCreatorUserId": payload.get("rfqCreatorUserId"),
                "restRemainder": payload.get("restRemainder", False),
                "comboLegs": payload.get("comboLegs") or [],
                "createdTime": payload.get("createdTime") or raw.get("exchange_ts"),
                "updatedTime": payload.get("updatedTime")
                or payload.get("createdTime") or raw.get("exchange_ts"),
            }
            continue
        st = states.get(rfq_id)
        if st is None:
            continue
        target = RFQ_EVENT_STATUS.get(etype)
        if target and rfq_allows(st["status"], target):
            st["status"] = target
            st["updatedTime"] = payload.get("updatedTime") or raw.get("exchange_ts")
        if etype == "rfq_updated":
            st["comboLegs"] = payload.get("comboLegs") or st["comboLegs"]
            st["updatedTime"] = payload.get("updatedTime") or raw.get("exchange_ts")
    return states


def _durable_quote_states(session: List[Dict[str, Any]],
                          self_user_id: str) -> Dict[str, Dict[str, Any]]:
    """Fold ALL session events into per-quote durable state."""
    states: Dict[str, Dict[str, Any]] = {}
    for item in sorted(session, key=lambda i: i.get("t", 0)):
        if item.get("kind") != "event":
            continue
        raw = item.get("raw", {})
        etype = raw.get("event_type")
        if etype not in QUOTE_EVENT_STATUS and etype != "quote_draft_revised":
            continue
        payload = raw.get("payload", {})
        maker = payload.get("creatorRfqUserId") or self_user_id
        rfq_id = raw.get("rfq_id")
        quote_id = raw.get("quote_id") or payload.get("id") or f"{maker}:{rfq_id}"
        prev = states.get(quote_id)
        if etype == "quote_draft_revised" and prev and prev["status"] == "ACTIVE":
            target = "REPLACED"
        else:
            target = quote_target_status(etype, prev["status"] if prev else None)
        if target is None:
            continue
        states[quote_id] = {
            "quote_id": quote_id,
            "rfq_id": rfq_id,
            "creatorRfqUserId": maker,
            "status": target,
            "buyPrice": payload.get("buyPrice"),
            "sellPrice": payload.get("sellPrice"),
            "buyQtyDecimal": payload.get("buyQtyDecimal"),
            "sellQtyDecimal": payload.get("sellQtyDecimal"),
            "acceptedSide": payload.get("acceptedSide"),
            "confirmationDeadline": payload.get("confirmationDeadline"),
            "executionDeadline": payload.get("executionDeadline"),
            "orderId": payload.get("orderId"),
            "updatedTime": payload.get("updatedTime") or raw.get("exchange_ts"),
        }
    return states


class SimulatedTransport(RfqTransport):
    """In-memory fake exchange driven by a scripted session.

    Session items: ``{"t": ms, "kind": "event"|"book"|"disconnect", ...}``.
    Event items carry ``raw`` (the message dict) and ``stream`` (bool, default
    True). ``stream=False`` events are invisible to the stream but present in
    durable reads -- this is what makes recovery meaningful (e.g. a missed
    ``rfq_closed``, or leg settlements which the real contract only exposes
    via ``GetRFQs``).

    ``stream_rfq_events`` resumes after the last consumed position, so a
    reconnect continues mid-session (models "new stream delivers only new
    events"). Disconnect items raise :class:`StreamDisconnected` when reached.
    """

    def __init__(self, session: List[Dict[str, Any]], self_user_id: str = "maker-001",
                 combos: Optional[List[Dict[str, Any]]] = None) -> None:
        self._session = sorted(session, key=lambda i: i.get("t", 0))
        self._self_user_id = self_user_id
        self._combos = list(combos or [])
        self._pos = 0
        self.create_quote_calls: List[Dict[str, Any]] = []  # always empty in paper mode
        self._rfq_states = _durable_rfq_states(self._session)
        self._quote_states = _durable_quote_states(self._session, self_user_id)

    # -- identity ----------------------------------------------------------
    def get_rfq_user_id(self) -> str:
        return self._self_user_id

    # -- streaming ----------------------------------------------------------
    def stream_rfq_events(self) -> Iterator[Dict[str, Any]]:
        log.debug("sim stream opened at pos=%d", self._pos)
        while self._pos < len(self._session):
            item = self._session[self._pos]
            self._pos += 1
            if item.get("kind") == "disconnect":
                raise StreamDisconnected(
                    f"injected disconnect at t={item.get('t')}ms"
                )
            if item.get("kind") == "event" and not item.get("stream", True):
                continue  # missed by the stream; durable reads still see it
            yield item

    def reset_stream(self) -> None:
        """Rewind the stream cursor (tests only)."""
        self._pos = 0

    # -- durable reads (reflect FULL exchange state, incl. missed events) ----
    def get_rfqs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        states = list(self._rfq_states.values())
        if status is None:
            return states
        if status == "OPEN":
            # "OPEN" read = everything actionable (not terminal).
            return [s for s in states if s["status"] not in RFQ_TERMINAL_STATUSES]
        return [s for s in states if s["status"] == status]

    def get_quotes(self, user_filter: Any = None) -> List[Dict[str, Any]]:
        quotes = list(self._quote_states.values())
        if user_filter in ("SELF", self._self_user_id):
            return [q for q in quotes if q["creatorRfqUserId"] == self._self_user_id]
        if user_filter is not None:
            return [q for q in quotes if q["creatorRfqUserId"] == user_filter]
        return quotes

    def get_combos(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        if symbol is None:
            return list(self._combos)
        return [c for c in self._combos if c.get("symbol") == symbol]

    # -- outbound (paper mode: recorded, never sent) -------------------------
    def create_quote(self, **kwargs: Any) -> Dict[str, Any]:
        """Paper-mode stub: records the would-be RPC, performs no I/O."""
        self.create_quote_calls.append(dict(kwargs))
        return {"quote_id": kwargs.get("quote_id", "paper-quote"), "paper": True}


class GrpcTransport(RfqTransport):
    """Stub for the live Polymarket US gRPC transport.

    Every method raises ``NotImplementedError`` naming exactly what is
    missing. No network code exists here by design. To go live, Andrew must
    supply (see README "Going live"):

    - the proto bundle ("Polymarket - Proto Files.zip") and generated
      ``polymarket.v1`` stubs (``RFQAPI``, ``DropCopyAPI``),
    - the gRPC endpoint (preprod vs prod),
    - Auth0 credentials: auth0 domain, ``client_id``, ``audience``, and the
      firm's RSA private key (Private Key JWT),
    - the scopes ``read:orders`` (RFQ stream) and ``read:dropcopy``.
    """

    _MSG = (
        "live Polymarket US gRPC transport not implemented: missing the proto "
        "bundle ('Polymarket - Proto Files.zip') with generated polymarket.v1 "
        "stubs (RFQAPI/DropCopyAPI), the gRPC endpoint, and Auth0 credentials "
        "(auth0 domain, client_id, audience, RSA private key for Private Key "
        "JWT). See README 'Going live'."
    )

    def get_rfq_user_id(self) -> str:
        raise NotImplementedError(self._MSG)

    def stream_rfq_events(self) -> Iterator[Dict[str, Any]]:
        raise NotImplementedError(self._MSG)
        yield  # pragma: no cover -- makes this a generator function

    def get_rfqs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError(self._MSG)

    def get_quotes(self, user_filter: Any = None) -> List[Dict[str, Any]]:
        raise NotImplementedError(self._MSG)

    def get_combos(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError(self._MSG)

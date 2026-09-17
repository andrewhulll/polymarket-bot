"""Drop Copy: the source of truth for fills.

``DropCopyAPI`` (``polymarket.v1``) is streaming ONLY, with ``resume_token``
(auth scope ``read:dropcopy``). ``quote_executed`` on the RFQ stream means
paired orders were ACCEPTED FOR SUBMISSION -- NOT filled. Fills (and only
fills) reconcile through Drop Copy.

This module mirrors the ``RfqTransport`` pattern:

- :class:`DropCopyTransport`: abstract interface (``stream_drop_copy``).
- :class:`SimulatedDropCopyTransport`: scripted in-memory feed with resume
  tokens.
- :class:`DropCopyStub`: raises ``NotImplementedError`` (no credentials).

:drain_drop_copy:` pulls records, normalizes them to ``drop_copy_fill``
events, and applies them through the store's exactly-once write path
(idempotent on ``fillId``/``dropCopySeq``). It returns the last resume token;
the store persists it (``dropcopy_state``) so a later drain resumes.

MODELING: the execution-report field names below are modeled (the proto
bundle was unavailable -- see the TODO in :mod:`combo_mm.events`). The shape
is deliberately close to the RFQ stream envelope so ``normalize`` stays
uniform.
"""
from __future__ import annotations

import abc
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from combo_mm.normalize import NormalizeError, normalize

log = logging.getLogger(__name__)

__all__ = [
    "DropCopyTransport",
    "SimulatedDropCopyTransport",
    "DropCopyStub",
    "drain_drop_copy",
]


class DropCopyTransport(abc.ABC):
    """Abstract Drop Copy transport (mirrors ``polymarket.v1.DropCopyAPI``).

    Auth scope ``read:dropcopy`` goes in the ``authorization`` gRPC metadata
    (Bearer token); see :mod:`combo_mm.auth`.
    """

    @abc.abstractmethod
    def stream_drop_copy(
        self, resume_token: Optional[str] = None
    ) -> Iterator[Dict[str, Any]]:
        """Stream execution reports, resuming after ``resume_token``.

        Each record carries its own ``resume_token`` for the next resume
        point. The generator is single-use.
        """


class SimulatedDropCopyTransport(DropCopyTransport):
    """In-memory fake Drop Copy feed driven by scripted records."""

    def __init__(self, records: List[Dict[str, Any]]) -> None:
        self._records = list(records)

    def stream_drop_copy(
        self, resume_token: Optional[str] = None
    ) -> Iterator[Dict[str, Any]]:
        started = resume_token is None
        for rec in self._records:
            if not started:
                if rec.get("resume_token") == resume_token:
                    started = True
                continue
            yield rec


class DropCopyStub(DropCopyTransport):
    """Stub for the live Drop Copy transport.

    Raises ``NotImplementedError`` naming exactly what is missing (same
    requirements as :class:`combo_mm.stream.GrpcTransport`, plus the
    ``read:dropcopy`` auth scope).
    """

    _MSG = (
        "live Polymarket US Drop Copy transport not implemented: missing the "
        "proto bundle ('Polymarket - Proto Files.zip') with generated "
        "polymarket.v1 DropCopyAPI stubs, the gRPC endpoint, and Auth0 "
        "credentials (auth0 domain, client_id, audience, RSA private key), "
        "including the read:dropcopy scope. See README 'Going live'."
    )

    def stream_drop_copy(
        self, resume_token: Optional[str] = None
    ) -> Iterator[Dict[str, Any]]:
        raise NotImplementedError(self._MSG)
        yield  # pragma: no cover -- makes this a generator function


def _record_to_event_raw(record: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a Drop Copy execution report to the normalize envelope."""
    return {
        "event_id": f"dropcopy:{record.get('resume_token') or record.get('dropCopySeq')}",
        "event_type": "drop_copy_fill",
        "rfq_id": record.get("rfqId"),
        "quote_id": record.get("quoteId"),
        "symbol": record.get("symbol"),
        "exchange_ts": record.get("executedTime"),
        "payload": {
            "fillId": record.get("fillId"),
            "dropCopySeq": record.get("dropCopySeq"),
            "side": record.get("side"),
            "price": record.get("price"),
            "qty": record.get("qty"),
            "executedTime": record.get("executedTime"),
        },
    }


def drain_drop_copy(transport: DropCopyTransport, store: Any,
                    resume_token: Optional[str] = None,
                    *,
                    now: Optional[datetime] = None) -> Optional[str]:
    """Drain available Drop Copy records into the store. Returns last token.

    Each record becomes a ``drop_copy_fill`` event applied through the
    exactly-once write path (idempotent on ``fillId``/``dropCopySeq``, so
    redeliveries under new resume tokens are no-ops). The last resume token
    is persisted on the store for the next drain.
    """
    token = resume_token if resume_token is not None else store.get_drop_copy_token()
    last: Optional[str] = token
    applied = 0
    now = now or datetime.now(timezone.utc)
    for record in transport.stream_drop_copy(token):
        raw = _record_to_event_raw(record)
        try:
            event = normalize(raw, now=now)
        except NormalizeError:
            log.warning("dropping malformed drop-copy record: %r", record,
                        exc_info=True)
            continue
        if store.apply(event):
            applied += 1
        last = record.get("resume_token") or last
    store.set_drop_copy_token(last)
    log.info("drop-copy drain: %d new fills, resume_token=%s", applied, last)
    return last

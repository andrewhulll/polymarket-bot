"""Eligibility filter: pure pre-pricing gate for the shadow quoting engine.

Runs before the pricer on every candidate event, in a fixed order:

1. event type -- only ``rfq_created`` / ``rfq_updated`` can quote; anything
   else is an internal event the caller ignores silently (no row recorded).
2. RFQ present -- ``SKIP_NO_RFQ``.
3. RFQ status terminal -- ``SKIP_RFQ_CLOSED`` (reuses
   ``RFQ_TERMINAL_STATUSES`` from :mod:`combo_mm.events`).
4. Legs present (inline or via the reference fallback) -- ``SKIP_NO_LEGS``.
5. RFQ fresh in *exchange time* -- ``SKIP_STALE_RFQ``.

Pure function: no I/O, no clock reads. ``now_ms`` must be exchange time
(the event's ``event_at``), never wall clock, so replays stay
deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from combo_mm.events import RFQ_TERMINAL_STATUSES, NormalizedEvent

__all__ = [
    "Eligibility",
    "check_eligibility",
    "SKIP_NO_RFQ",
    "SKIP_RFQ_CLOSED",
    "SKIP_NO_LEGS",
    "SKIP_STALE_RFQ",
]

SKIP_NO_RFQ = "SKIP_NO_RFQ"
SKIP_RFQ_CLOSED = "SKIP_RFQ_CLOSED"
SKIP_NO_LEGS = "SKIP_NO_LEGS"
SKIP_STALE_RFQ = "SKIP_STALE_RFQ"

_QUOTABLE_EVENTS = ("rfq_created", "rfq_updated")


@dataclass(frozen=True)
class Eligibility:
    """Eligibility outcome.

    ``skip_reason`` is None when eligible. It is also None for internal
    (non-RFQ) event types -- the caller treats that case as "ignore
    silently" and records nothing.
    """

    eligible: bool
    skip_reason: Optional[str]


def _iso_to_ms(value: Optional[str]) -> int:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return 0


def check_eligibility(
    event: NormalizedEvent,
    rfq: Optional[Dict[str, Any]],
    *,
    now_ms: int,
    stale_rfq_ms: int,
    reference: Any = None,
) -> Eligibility:
    """Decide whether ``event``'s RFQ may be priced. See module docstring."""
    if event.event_type not in _QUOTABLE_EVENTS:
        return Eligibility(eligible=False, skip_reason=None)
    if rfq is None:
        return Eligibility(eligible=False, skip_reason=SKIP_NO_RFQ)
    if rfq.get("status") in RFQ_TERMINAL_STATUSES:
        return Eligibility(eligible=False, skip_reason=SKIP_RFQ_CLOSED)
    if not list(rfq.get("legs") or []):
        has_fallback = (
            reference is not None
            and bool(rfq.get("symbol"))
            and reference.get(rfq["symbol"]) is not None
        )
        if not has_fallback:
            return Eligibility(eligible=False, skip_reason=SKIP_NO_LEGS)
    updated_ms = _iso_to_ms(rfq.get("updated_time"))
    # Fail closed: a missing or unparseable timestamp means we cannot prove
    # freshness, so the RFQ is skipped rather than quoted.
    if not updated_ms or now_ms - updated_ms > stale_rfq_ms:
        return Eligibility(eligible=False, skip_reason=SKIP_STALE_RFQ)
    return Eligibility(eligible=True, skip_reason=None)

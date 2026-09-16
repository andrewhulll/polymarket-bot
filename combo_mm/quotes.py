"""Mirror of our own quote lifecycle for future quoter use.

``QuoteTracker`` is a read model over the store's ``quotes`` table (kept fresh
by :meth:`sync`, typically after recovery). It exposes the current quote state
per RFQ so a future quoter can decide whether to (re)quote without touching
the event log.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from combo_mm.store import EventStore

__all__ = ["QuoteTracker"]


class QuoteTracker:
    """In-memory mirror of the ``quotes`` table, keyed by RFQ."""

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._by_rfq: Dict[str, dict] = {}
        self.sync()

    def sync(self) -> None:
        """Rebuild the mirror from the store (call after recovery)."""
        by_rfq: Dict[str, dict] = {}
        for row in self._store.list_quotes():
            by_rfq[row["rfq_id"]] = dict(row)
        self._by_rfq = by_rfq

    def get(self, rfq_id: str) -> Optional[dict]:
        """Current quote state for an RFQ, or None if we never quoted it."""
        return self._by_rfq.get(rfq_id)

    def get_by_id(self, quote_id: str) -> Optional[dict]:
        row = self._store.get_quote(quote_id)
        return dict(row) if row else None

    def all(self) -> List[dict]:
        return list(self._by_rfq.values())

    def has_live_quote(self, rfq_id: str) -> bool:
        q = self._by_rfq.get(rfq_id)
        return q is not None and q["status"] in ("DRAFT", "ACTIVE", "REPLACED")

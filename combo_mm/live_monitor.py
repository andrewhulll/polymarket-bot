"""Live RFQ feed monitor: poll an :class:`EventSource` into the shadow pipeline.

The dashboard's "Live monitor" button. Each :meth:`LiveMonitor.poll_once`
pulls whatever the source has (Retail REST polling in practice), feeds book
snapshots to the leg book cache and the store, applies RFQ events, and runs
the shadow quoting engine on ``rfq_created`` / ``rfq_updated`` -- the same
path the backtest replays. Paper only: drafts are stored, never sent.

Unlike :class:`combo_mm.consumer.PollingConsumer` there is no background
thread: the caller (a Streamlit fragment on a timer) decides when to poll, so
the monitor never outlives the page session. Source failures are recorded
(exception class only -- never messages, which could carry credential
material) instead of raised.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from combo_mm.books import LegBookCache
from combo_mm.config import PipelineConfig
from combo_mm.engine import ShadowQuotingEngine
from combo_mm.normalize import NormalizeError, normalize
from combo_mm.pricer import Pricer
from combo_mm.reference import ReferenceCache
from combo_mm.sources import EventSource
from combo_mm.store import EventStore

log = logging.getLogger(__name__)

__all__ = ["LiveMonitor"]

_QUOTABLE = ("rfq_created", "rfq_updated")


class _NoCombos:
    """Reference transport stand-in: live RFQs must carry inline legs."""

    def get_combos(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        return []


class LiveMonitor:
    """Synchronous poll -> store -> shadow engine loop over one source."""

    def __init__(self, source: EventSource, store: EventStore,
                 config: Optional[PipelineConfig] = None, *,
                 pricer: Optional[Pricer] = None, source_label: str = "live") -> None:
        self.config = config or PipelineConfig(paper_mode=True)
        self.source = source
        self.store = store
        self.books = LegBookCache(staleness_ms=self.config.staleness_ms)
        reference = ReferenceCache(_NoCombos(), ttl_s=self.config.reference_ttl_s)  # type: ignore[arg-type]
        self.engine = ShadowQuotingEngine(store, self.books, reference, self.config,
                                          pricer=pricer)
        self.source_label = source_label
        self.polls = 0
        self.events_applied = 0
        self.books_seen = 0
        self.dropped = 0
        self.poll_errors = 0
        self.last_error: Optional[str] = None
        self.last_poll_at: Optional[str] = None
        self.started_at = datetime.now(timezone.utc).isoformat()

    @property
    def rfq_beta_enabled(self) -> Optional[bool]:
        """The Retail source's RFQ beta flag (None for sources without one)."""
        return getattr(self.source, "rfq_beta_enabled", None)

    def poll_once(self, now: Optional[datetime] = None) -> int:
        """Poll the source once; returns the number of items processed."""
        now = now or datetime.now(timezone.utc)
        self.last_poll_at = now.isoformat()
        try:
            items = self.source.poll(now)
        except Exception as exc:  # recorded, never raised into the page
            self.poll_errors += 1
            self.last_error = type(exc).__name__
            log.warning("live poll failed: %s", type(exc).__name__)
            return 0
        self.polls += 1
        self.last_error = getattr(self.source, "last_error", None)
        # Books first: a source emits the leg books for an RFQ in the same
        # poll (Retail does so after the events), and the engine prices
        # rfq_created immediately -- it must see those books.
        ordered = sorted(items, key=lambda item: item.get("kind") != "book")
        for n, item in enumerate(ordered):
            self._dispatch(item, now, n)
        return len(items)

    def _dispatch(self, item: Dict[str, Any], now: datetime, n: int) -> None:
        kind = item.get("kind")
        if kind == "book":
            self.books.update(symbol=item["symbol"], bid=item.get("bid"), ask=item.get("ask"),
                              bid_size=item.get("bid_size"), ask_size=item.get("ask_size"),
                              updated_at=item.get("ts"), seq=item.get("seq"))
            self.store.ingest_book(item["symbol"], item.get("bid"), item.get("ask"),
                                   item.get("bid_size", 0.0), item.get("ask_size", 0.0),
                                   item.get("seq", 0), item.get("ts"))
            self.books_seen += 1
            return
        if kind != "event":
            return
        raw = dict(item["raw"])
        raw.setdefault("event_id", f"{self.source_label}:{self.polls}:{n}")
        try:
            event = normalize(raw, now=now)
        except NormalizeError:
            self.dropped += 1
            log.warning("dropping malformed live item")
            return
        if item.get("client_derived"):
            event = replace(event, client_derived=True)
        if self.store.apply(event, source=self.source_label):
            self.events_applied += 1
            if event.event_type in _QUOTABLE:
                self.engine.maybe_quote(event)

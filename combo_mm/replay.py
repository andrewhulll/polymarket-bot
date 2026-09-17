"""Deterministic replay harness: fixture -> pipeline -> state digest.

Feeds session items through the pipeline with a virtual clock (no wall-clock
dependence): book snapshots update the cache + store, events normalize with
``received_at = base + t`` and apply through the exactly-once write path,
and the shadow quoting engine prices each new/updated RFQ. Used by tests
and the demo script. Returns the state digest plus counters.
:param include_stream_invisible: when True, stream-invisible events are also
    replayed (full-information replay, e.g. for the backtest). When False,
    only what the stream would deliver is replayed (recovery is then needed
    to converge, which tests exercise separately).
:param drain_drop_copy: when True, the fixture's Drop Copy feed is drained
    into the store after the stream replay (fills reconcile via Drop Copy
    only).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from combo_mm.books import LegBookCache
from combo_mm.config import PipelineConfig
from combo_mm.dropcopy import SimulatedDropCopyTransport, drain_drop_copy
from combo_mm.engine import ShadowQuotingEngine
from combo_mm.fixtures import BASE_TS, SELF_USER_ID
from combo_mm.normalize import normalize
from combo_mm.reference import ReferenceCache
from combo_mm.store import EventStore
from combo_mm.stream import SimulatedTransport

log = logging.getLogger(__name__)

__all__ = ["replay_session", "state_digest"]


def state_digest(store: EventStore) -> str:
    """Byte-for-byte state digest of the store's read model (64-char hex).

    Delegates to :meth:`EventStore.state_digest`; kept here so replay
    callers have a single import for the digest.
    """
    return store.state_digest()


def replay_session(session: List[Dict[str, Any]],
                   combos: List[Dict[str, Any]],
                   store: EventStore,
                   config: PipelineConfig,
                   *,
                   books: Optional[LegBookCache] = None,
                   self_user_id: str = SELF_USER_ID,
                   enable_shadow: bool = True,
                   include_stream_invisible: bool = True,
                   drain_drop_copy_records: Optional[List[Dict[str, Any]]] = None,
                   ) -> Dict[str, Any]:
    """Replay a scripted session deterministically."""
    transport = SimulatedTransport(session, self_user_id, combos)
    books = books or LegBookCache(staleness_ms=config.staleness_ms)
    reference = ReferenceCache(transport, ttl_s=config.reference_ttl_s)
    engine = (ShadowQuotingEngine(store, books, reference, config,
                                  params_version=config.params_version)
              if enable_shadow else None)

    counters = {"events": 0, "duplicates": 0, "books": 0, "decisions": 0}
    for item in sorted(session, key=lambda i: i.get("t", 0)):
        kind = item.get("kind")
        if kind == "disconnect":
            continue
        if kind == "book":
            books.update(
                symbol=item["symbol"], bid=item.get("bid"), ask=item.get("ask"),
                bid_size=item.get("bid_size"), ask_size=item.get("ask_size"),
                updated_at=item.get("ts"), seq=item.get("seq"),
            )
            store.ingest_book(
                item["symbol"], item.get("bid"), item.get("ask"),
                item.get("bid_size", 0.0), item.get("ask_size", 0.0),
                item.get("seq", 0), item.get("ts"))
            counters["books"] += 1
            continue
        if kind != "event":
            continue
        if not include_stream_invisible and not item.get("stream", True):
            continue
        now = BASE_TS + timedelta(milliseconds=item.get("t", 0))
        event = normalize(item["raw"], now=now)
        if store.apply(event):
            counters["events"] += 1
            if engine is not None and event.event_type in ("rfq_created", "rfq_updated"):
                # One engine run == one shadow_decisions row (quote, decline,
                # or skip).
                engine.maybe_quote(event)
                counters["decisions"] += 1
        else:
            counters["duplicates"] += 1

    if drain_drop_copy_records is not None:
        drain_drop_copy(SimulatedDropCopyTransport(drain_drop_copy_records),
                        store, now=BASE_TS)

    counters["digest"] = state_digest(store)
    return counters

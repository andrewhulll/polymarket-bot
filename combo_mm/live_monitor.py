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
import time
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from combo_mm.books import LegBookCache
from combo_mm.combo_markets import ComboMarketCatalog
from combo_mm.config import PipelineConfig
from combo_mm.engine import ShadowQuotingEngine
from combo_mm.events import NormalizedEvent
from combo_mm.normalize import NormalizeError, normalize
from combo_mm.pricer import Pricer
from combo_mm.quote_selections import QuoteSelectionStore
from combo_mm.reference import ReferenceCache
from combo_mm.rfq_screen import screen_legs
from combo_mm.sources import EventSource
from combo_mm.store import EventStore

log = logging.getLogger(__name__)

__all__ = ["LiveMonitor"]

_QUOTABLE = ("rfq_created", "rfq_updated")


def _iso_to_ms(value: Optional[str]) -> int:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return 0


class _NoCombos:
    """Reference transport stand-in: live RFQs must carry inline legs."""

    def get_combos(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        return []


class LiveMonitor:
    """Synchronous poll -> store -> shadow engine loop over one source.

    With a ``catalog`` every new RFQ is screened (:mod:`combo_mm.rfq_screen`)
    into the store's ``rfq_screen`` table, and RFQs with legs the catalog did
    not know yet are re-screened as the catalog grows. With ``selections``,
    trade broadcasts for selected RFQs are stored as accepted quotes; recent
    trades are also kept in memory so an RFQ picked after it traded still
    gets its accepted quote (:meth:`select`).
    """

    RECENT_TRADES_MAX = 50_000
    RESCREEN_MIN_INTERVAL_S = 10.0
    RESCREEN_BATCH = 5_000

    def __init__(self, source: EventSource, store: EventStore,
                 config: Optional[PipelineConfig] = None, *,
                 pricer: Optional[Pricer] = None, source_label: str = "live",
                 catalog: Optional[ComboMarketCatalog] = None,
                 selections: Optional[QuoteSelectionStore] = None) -> None:
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
        self.catalog = catalog
        self.selections = selections
        self.recent_trades: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.accepted_recorded = 0
        self._rescreened_version = -1
        self._last_rescreen = 0.0

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
        self._rescreen_unresolved()
        return len(items)

    # -- selection ------------------------------------------------------------
    def select(self, rfq_id: str, snapshot: Optional[Dict[str, Any]] = None) -> bool:
        """Mark an RFQ to quote; stores its accepted quote if it already traded."""
        if self.selections is None:
            raise RuntimeError("LiveMonitor has no selection store")
        added = self.selections.select(rfq_id, snapshot)
        trade = self.recent_trades.get(rfq_id)
        if trade is not None and self.selections.record_accepted(trade):
            self.accepted_recorded += 1
        return added

    # -- screening ------------------------------------------------------------
    def _screen_new_rfq(self, raw: Dict[str, Any], rfq_id: str) -> None:
        legs = [str(leg.get("symbol")) for leg in raw.get("comboLegs") or []
                if isinstance(leg, dict) and leg.get("symbol") is not None]
        if self.catalog is None or not legs:
            return
        result = screen_legs(self.catalog.resolve(legs))
        self.store.upsert_rfq_screen(
            rfq_id, n_legs=result.n_legs, n_resolved=result.n_resolved,
            n_nfl_legs=result.n_nfl_legs, screen=result.screen, rank=result.rank,
            catalog_version=self.catalog.version,
            direction=raw.get("direction") or None, side=_combo_side(raw),
            condition_id=raw.get("condition_id") or None,
            submission_deadline=raw.get("submission_deadline") or None)

    def _rescreen_unresolved(self) -> None:
        """Re-screen RFQs whose legs were unknown, once per catalog growth (throttled)."""
        catalog = self.catalog
        if catalog is None or catalog.version == self._rescreened_version:
            return
        if time.monotonic() - self._last_rescreen < self.RESCREEN_MIN_INTERVAL_S:
            return
        version = catalog.version
        rows = self.store.unresolved_screen_rfqs(version, limit=self.RESCREEN_BATCH)
        for row in rows:
            result = screen_legs(catalog.resolve(row["legs"]))
            self.store.upsert_rfq_screen(
                row["rfq_id"], n_legs=result.n_legs, n_resolved=result.n_resolved,
                n_nfl_legs=result.n_nfl_legs, screen=result.screen, rank=result.rank,
                catalog_version=version)
        if len(rows) < self.RESCREEN_BATCH:  # backlog drained for this version
            self._rescreened_version = version
            self._last_rescreen = time.monotonic()

    def _on_trade(self, raw: Dict[str, Any], rfq_id: str) -> None:
        if "price" not in raw and "size" not in raw:
            return  # not a gateway trade broadcast (e.g. a Retail rfq_closed)
        trade = {k: raw.get(k) for k in ("price", "size", "direction", "side",
                                         "requester_id", "condition_id", "executed_at")}
        trade["rfq_id"] = rfq_id
        self.recent_trades[rfq_id] = trade
        while len(self.recent_trades) > self.RECENT_TRADES_MAX:
            self.recent_trades.popitem(last=False)
        if self.selections is not None and self.selections.record_accepted(trade):
            self.accepted_recorded += 1

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
        if event.event_type == "rfq_closed" and event.rfq_id:
            # Before apply(): a redelivered trade is still a trade for a late selection.
            self._on_trade(raw, event.rfq_id)
        if self.store.apply(event, source=self.source_label):
            self.events_applied += 1
            if event.event_type == "rfq_created" and event.rfq_id:
                self._screen_new_rfq(raw, event.rfq_id)
            if event.event_type in _QUOTABLE:
                self._quote_and_measure(event, now)

    def _quote_and_measure(self, event: NormalizedEvent, now: datetime) -> None:
        """Run the engine, then record how long we took since the RFQ posted.

        ``now`` is the poll's wall-clock time (defaults to real time in
        live use, fixed in tests) -- using it instead of a fresh
        ``datetime.now()`` keeps this measurable/testable like the rest of
        the dispatch path. A posted time we can't parse skips the sample
        (nothing to measure against).
        """
        draft = self.engine.maybe_quote(event)
        posted_ms = _iso_to_ms(event.event_at)
        if posted_ms <= 0 or not event.rfq_id:
            return
        decided_ms = now.astimezone(timezone.utc).timestamp() * 1000
        latency_ms = decided_ms - posted_ms
        budget_ms = self.config.quote_latency_budget_ms
        over_budget = latency_ms > budget_ms
        self.store.record_quote_latency(
            rfq_id=event.rfq_id, event_type=event.event_type,
            posted_at=event.event_at,
            decided_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            latency_ms=latency_ms, quoted=draft is not None,
            over_budget=over_budget, source=self.source_label)
        if over_budget:
            log.warning(
                "SLOW: rfq=%s took %.0fms to %s (budget %dms) -- likely too "
                "slow to win this RFQ",
                event.rfq_id, latency_ms,
                "quote" if draft is not None else "decide against quoting",
                budget_ms)


def _combo_side(raw: Dict[str, Any]) -> Optional[str]:
    """Gateway legs inherit the combo side, so the first leg's side is the combo's."""
    legs = raw.get("comboLegs") or []
    side = legs[0].get("side") if legs and isinstance(legs[0], dict) else None
    return str(side) if side else None

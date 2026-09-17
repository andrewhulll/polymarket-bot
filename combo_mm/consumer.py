"""Stream consumer: reads the RFQ stream, dispatches without blocking.

Connection procedure (per the Polymarket US gRPC contract):

  1. Open the stream (``StreamRFQEvents``, EMPTY request, Bearer metadata with
     scope ``read:orders``). New streams deliver only NEW events -- no replay.
  2. ``GetRFQs(open)`` durable read.
  3. ``GetQuotes(self)`` durable read.
  4. Only then apply stream events.

The reader loop (``run()``) owns reconnects. On EVERY (re)connect it sets
state ``RECOVERING``, runs :func:`recovery_sync` (steps 2-3 reconcile
anything missed), and only then starts pulling from the stream. Each stream
item is handed to a dispatcher thread via a queue, so a slow handler never
blocks the reader; the dispatch queue is drained before each recovery so no
stream event interleaves mid-recovery.

Reconnect policy: stream opens respect the contract's 1-stream-per-second-
per-firm limit (``min_reconnect_interval_s``) ON TOP of exponential backoff
with jitter (``backoff_initial_ms`` -> ``backoff_max_ms``); ``max_reconnects``
caps attempts (``None`` = infinite).

Watchdog: if the reader sees no item for ``watchdog_silence_s``, it forces a
reconnect (treats silence as a dead stream). A hanging stream where the
generator neither yields nor raises is detected by running the stream pump in
a daemon thread with a join timeout.

Paper mode: any intended outbound quote RPC is logged as NOT SENT, never
issued (see :mod:`combo_mm.engine`). The real transport's ``create_quote``
is never called by this module.
"""
from __future__ import annotations

import logging
import queue
import random
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

from combo_mm.normalize import NormalizeError, normalize
from combo_mm.recovery import recovery_sync
from combo_mm.config import PipelineConfig
from combo_mm.sources import EventSource
from combo_mm.stream import RfqTransport, StreamDisconnected

log = logging.getLogger(__name__)

__all__ = ["ConsumerConfig", "PollingConsumer", "StreamConsumer", "StreamState"]


class StreamState:
    CONNECTING = "CONNECTING"
    RECOVERING = "RECOVERING"
    STREAMING = "STREAMING"
    STOPPED = "STOPPED"


class ConsumerConfig:
    def __init__(self, *,
                 max_reconnects: Optional[int] = None,
                 backoff_initial_ms: float = 250.0,
                 backoff_max_ms: float = 30_000.0,
                 min_reconnect_interval_s: float = 1.0,
                 watchdog_silence_s: float = 30.0,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_state: Optional[Callable[[str], None]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.max_reconnects = max_reconnects
        self.backoff_initial_ms = backoff_initial_ms
        self.backoff_max_ms = backoff_max_ms
        #: Minimum gap between stream opens: the contract allows 1 new
        #: stream per second per firm (burst 1).
        self.min_reconnect_interval_s = min_reconnect_interval_s
        self.watchdog_silence_s = watchdog_silence_s
        self.on_event = on_event
        self.on_state = on_state
        self.clock = clock or time.monotonic


def _coerce_consumer_config(config: Any) -> ConsumerConfig:
    """Accept a :class:`ConsumerConfig` or a :class:`PipelineConfig`.

    A ``PipelineConfig`` is translated to the reconnect-relevant knobs; the
    1-stream-per-second pacing is disabled for this translation because it
    only constrains the live transport, not simulated/test drivers.
    """
    if config is None:
        return ConsumerConfig()
    if isinstance(config, ConsumerConfig):
        return config
    if isinstance(config, PipelineConfig):
        return ConsumerConfig(
            max_reconnects=config.max_reconnects,
            backoff_initial_ms=config.backoff_initial_ms,
            backoff_max_ms=config.backoff_max_ms,
            min_reconnect_interval_s=0.0,
            watchdog_silence_s=config.watchdog_silence_s,
        )
    raise TypeError(
        f"config must be ConsumerConfig or PipelineConfig, "
        f"got {type(config).__name__}")


class StreamConsumer:
    """Reads the RFQ stream and dispatches items to a handler thread."""

    def __init__(self, transport: RfqTransport, store: Any,
                 config: Optional[Any] = None, *,
                 rng: Optional[random.Random] = None,
                 enable_shadow: bool = False) -> None:
        self._transport = transport
        self._store = store
        self._pipeline_config = (
            config if isinstance(config, PipelineConfig) else None)
        self._config = _coerce_consumer_config(config)
        #: Deterministic jitter source for the synchronous run() path.
        self._rng = rng if rng is not None else random.Random()
        self._jitter = (config.backoff_jitter
                        if isinstance(config, PipelineConfig) else 0.0)
        #: When True, run() prices RFQs with the shadow quoting engine
        #: (paper only).
        self._enable_shadow = enable_shadow
        #: One RecoveryReport per (re)connect, in order.
        self.recovery_log: List[Any] = []
        self._stop = threading.Event()
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._state = StreamState.CONNECTING
        self._state_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._dispatch_thread: Optional[threading.Thread] = None
        self._pump_thread: Optional[threading.Thread] = None
        self._attempts = 0
        self._last_stream_open = 0.0
        self.events_seen = 0
        self.events_applied = 0
        self.reconnects = 0

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._set_state(StreamState.CONNECTING)
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, name="rfq-dispatch", daemon=True)
        self._dispatch_thread.start()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="rfq-reader", daemon=True)
        self._reader_thread.start()
        log.info("consumer started (paper mode: outbound quotes NOT SENT)")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in (self._pump_thread, self._reader_thread):
            if t is not None:
                t.join(timeout=timeout)
        self._queue.put(None)
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=timeout)
        self._set_state(StreamState.STOPPED)
        log.info("consumer stopped")

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state
        cb = self._config.on_state
        if cb is not None:
            cb(state)

    # -- synchronous drain (deterministic test/sim driver) ----------------------
    def run(self) -> Dict[str, int]:
        """Drain the stream to exhaustion, reconnecting on drops.

        Synchronous counterpart to :meth:`start`/:meth:`stop`: opens the
        stream, runs :func:`recovery_sync` on EVERY (re)connect (appending
        the report to :attr:`recovery_log`), then applies items until the
        stream ends. A silent stream trips the watchdog and reconnects;
        only :class:`StreamDisconnected` triggers a reconnect -- other
        errors propagate to the caller.

        When ``enable_shadow`` was passed, RFQ creates/updates are priced
        by the shadow quoting engine (paper only: no outbound RPC, ever).

        Returns a stats dict: ``seen`` / ``applied`` / ``duplicates`` /
        ``books`` / ``reconnects`` / ``recoveries`` / ``shadow_decisions``.
        """
        from combo_mm.books import LegBookCache
        from combo_mm.engine import ShadowQuotingEngine
        from combo_mm.fixtures import BASE_TS
        from combo_mm.reference import ReferenceCache

        cfg = self._config
        pipeline = self._pipeline_config or PipelineConfig()
        books = LegBookCache(staleness_ms=pipeline.staleness_ms)
        reference = ReferenceCache(self._transport,
                                   ttl_s=pipeline.reference_ttl_s)
        engine = (ShadowQuotingEngine(self._store, books, reference, pipeline,
                                      params_version=pipeline.params_version)
                  if self._enable_shadow else None)

        stats = {"seen": 0, "applied": 0, "duplicates": 0, "books": 0,
                 "reconnects": 0, "recoveries": 0, "shadow_decisions": 0}

        def _now_for(item: Dict[str, Any]) -> datetime:
            # Deterministic ingest time: exchange-time base + item offset,
            # mirroring replay_session's virtual clock.
            return BASE_TS + timedelta(milliseconds=item.get("t", 0))

        def _handle(item: Dict[str, Any]) -> None:
            kind = item.get("kind")
            if kind == "book":
                books.update(
                    symbol=item["symbol"], bid=item.get("bid"),
                    ask=item.get("ask"), bid_size=item.get("bid_size", 0.0),
                    ask_size=item.get("ask_size", 0.0),
                    updated_at=item.get("ts"), seq=item.get("seq", 0))
                self._store.ingest_book(
                    item["symbol"], item.get("bid"), item.get("ask"),
                    item.get("bid_size", 0.0), item.get("ask_size", 0.0),
                    item.get("seq", 0), item.get("ts"))
                stats["books"] += 1
                return
            if kind != "event":
                log.warning("unknown stream item kind %r", kind)
                return
            raw = dict(item["raw"])
            raw.setdefault("event_id", f"stream:{stats['seen']}")
            try:
                event = normalize(raw, now=_now_for(item))
            except NormalizeError:
                log.warning("dropping malformed stream item", exc_info=True)
                return
            stats["seen"] += 1
            if self._store.apply(event, source="stream"):
                stats["applied"] += 1
                if (engine is not None
                        and event.event_type in ("rfq_created", "rfq_updated")):
                    # Every engine run records exactly one shadow_decisions
                    # row (quote, decline, or skip), so counting calls ==
                    # counting decisions.
                    engine.maybe_quote(event)
                    stats["shadow_decisions"] += 1
            else:
                stats["duplicates"] += 1

        reconnects = 0
        while True:
            self._set_state(StreamState.RECOVERING)
            self.recovery_log.append(
                recovery_sync(self._transport, self._store))
            stats["recoveries"] += 1
            self._set_state(StreamState.STREAMING)
            try:
                for item in self._iter_stream(cfg.watchdog_silence_s):
                    _handle(item)
            except StreamDisconnected as exc:
                log.warning("stream disconnected: %s", exc)
                reconnects += 1
                stats["reconnects"] = reconnects
                self.reconnects = reconnects
                if (cfg.max_reconnects is not None
                        and reconnects >= cfg.max_reconnects):
                    break
                self._sleep_backoff_sync(reconnects)
                continue
            break
        self._set_state(StreamState.STOPPED)
        self.events_seen = stats["seen"]
        self.events_applied = stats["applied"]
        return stats

    def _iter_stream(self, silence_s: float) -> Iterator[Dict[str, Any]]:
        """Yield stream items; :class:`StreamDisconnected` on watchdog silence.

        The generator runs in a daemon pump thread so a hanging stream
        (neither yields nor raises) cannot block the caller: the queue
        timeout turns silence into a reconnect.
        """
        stream = self._transport.stream_rfq_events()
        pump_queue: "queue.Queue[Any]" = queue.Queue()

        def _pump() -> None:
            try:
                for item in stream:
                    pump_queue.put(("item", item))
            except StreamDisconnected as exc:
                pump_queue.put(("disconnect", exc))
            except Exception as exc:  # noqa: BLE001 - surfaced as reconnect
                pump_queue.put(("error", exc))
            else:
                # Generator exhausted: for this draining driver, done.
                # (A live stream never just ends; the reader loop treats
                # that as a disconnect instead.)
                pump_queue.put(("end", None))

        pump = threading.Thread(target=_pump, name="rfq-run-pump", daemon=True)
        pump.start()
        while True:
            try:
                kind, payload = pump_queue.get(timeout=silence_s)
            except queue.Empty:
                raise StreamDisconnected(
                    f"watchdog: no stream items for {silence_s:.0f}s")
            if kind == "item":
                yield payload
            elif kind == "disconnect":
                raise payload
            elif kind == "error":
                raise StreamDisconnected(f"pump error: {payload!r}")
            else:
                return

    def _sleep_backoff_sync(self, reconnects: int) -> None:
        """Jittered exponential backoff for the synchronous run() path."""
        cfg = self._config
        delay_ms = min(cfg.backoff_initial_ms * (2 ** (reconnects - 1)),
                       cfg.backoff_max_ms)
        delay_s = (delay_ms / 1000.0) * self._rng.uniform(
            1.0 - self._jitter, 1.0 + self._jitter)
        log.info("reconnect backoff: sleeping %.2fs", delay_s)
        time.sleep(delay_s)

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    # -- reader loop --------------------------------------------------------------
    def _reader_loop(self) -> None:
        cfg = self._config
        while not self._stop.is_set():
            if cfg.max_reconnects is not None and \
                    self._attempts >= cfg.max_reconnects:
                log.error("max reconnects (%d) reached; stopping",
                          cfg.max_reconnects)
                self._stop.set()
                break
            self._attempts += 1
            attempt = self._attempts
            try:
                self._set_state(StreamState.CONNECTING)
                self._pace_stream_open()
                stream = self._transport.stream_rfq_events()
                # 1-3: the stream is open; durable reads now, then events.
                self._set_state(StreamState.RECOVERING)
                self._drain_dispatch_queue()
                report = recovery_sync(self._transport, self._store)
                log.info("recovery complete: %s", report)
                self._set_state(StreamState.STREAMING)
                self.reconnects += 1
                self._pump_stream(stream)
            except StreamDisconnected as exc:
                log.warning("stream disconnected (attempt %d): %s", attempt, exc)
                self._sleep_backoff(attempt)
            except Exception:
                log.exception("reader loop error (attempt %d)", attempt)
                self._sleep_backoff(attempt)

    def _pace_stream_open(self) -> None:
        """Enforce the 1-stream-per-second-per-firm limit."""
        cfg = self._config
        now = cfg.clock()
        wait = cfg.min_reconnect_interval_s - (now - self._last_stream_open)
        if wait > 0:
            log.debug("pacing stream open: sleeping %.2fs", wait)
            self._stop.wait(wait)
        self._last_stream_open = cfg.clock()

    def _drain_dispatch_queue(self) -> None:
        """Block until every queued item has been dispatched.

        Called after opening the stream and BEFORE the durable reads, so no
        stream event interleaves with recovery application. The previous
        generation's pump writes only to its own private queue (discarded on
        reconnect), so the only items that can be in the dispatch queue here
        predate the disconnect -- ``join()`` waits exactly for those. The
        dispatch thread owns consumption; the reader never pulls items
        itself (doing so would race the dispatcher on a sentinel).
        """
        self._queue.join()

    def _pump_stream(self, stream: Any) -> None:
        """Consume the stream via a daemon pump thread (watchdog-safe).

        A hanging generator (neither yields nor raises) would block the
        reader forever; the join timeout turns silence into a reconnect.
        """
        pump_queue: "queue.Queue[Any]" = queue.Queue()

        def _pump() -> None:
            try:
                for item in stream:
                    pump_queue.put(("item", item))
                    if self._stop.is_set():
                        break
            except StreamDisconnected as exc:
                pump_queue.put(("disconnect", exc))
            except Exception as exc:  # noqa: BLE001 - surfaced as reconnect
                pump_queue.put(("error", exc))

        self._pump_thread = threading.Thread(
            target=_pump, name="rfq-pump", daemon=True)
        self._pump_thread.start()
        last_item = self._config.clock()
        silence = self._config.watchdog_silence_s
        while not self._stop.is_set():
            self._pump_thread.join(timeout=1.0)
            # Drain BEFORE checking liveness: a dead pump may have left
            # items (and the disconnect signal) in the queue.
            while True:
                try:
                    kind, payload = pump_queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "item":
                    self._queue.put(payload)
                    last_item = self._config.clock()
                elif kind == "disconnect":
                    raise payload
                else:
                    raise StreamDisconnected(f"pump error: {payload!r}")
            if not self._pump_thread.is_alive():
                # Stream ended without a disconnect signal: the generator
                # was exhausted. Treat as a disconnect so the reader
                # reconnects (a live stream never just ends).
                raise StreamDisconnected("stream ended without disconnect")
            if self._config.clock() - last_item > silence:
                raise StreamDisconnected(
                    f"watchdog: no stream items for {silence:.0f}s")

    def _sleep_backoff(self, attempt: int) -> None:
        cfg = self._config
        delay_ms = min(cfg.backoff_initial_ms * (2 ** (attempt - 1)),
                       cfg.backoff_max_ms)
        delay_s = (delay_ms / 1000.0) * random.uniform(0.8, 1.2)
        log.info("reconnect backoff: sleeping %.2fs (attempt %d)",
                 delay_s, attempt)
        self._stop.wait(delay_s)

    # -- dispatch loop --------------------------------------------------------------
    def _dispatch_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            self._dispatch_item(item)
            self._queue.task_done()

    def _dispatch_item(self, item: Dict[str, Any]) -> None:
        cfg = self._config
        self.events_seen += 1
        try:
            status = _dispatch_source_item(
                self._store,
                item,
                on_item=cfg.on_event,
                now=datetime.now(timezone.utc),
                source="stream",
                default_event_id=f"stream:{self.events_seen}",
            )
        except Exception:
            log.exception("dispatch error")
        else:
            if status == "applied":
                self.events_applied += 1


def _dispatch_source_item(
    store: Any,
    item: Dict[str, Any],
    *,
    on_item: Optional[Callable[[Dict[str, Any]], None]],
    now: datetime,
    source: str,
    default_event_id: str,
) -> str:
    """Apply one stream/EventSource item; shared by both consumers.

    Returns ``"applied"`` / ``"duplicate"`` / ``"dropped"`` / ``"book"`` /
    ``"unknown"``. An item-level ``"client_derived": True`` flag (used by
    poll-based sources for locally inferred closures/expiries) is copied
    onto the normalized event so the audit log can tell our inferences
    from exchange facts.
    """
    kind = item.get("kind")
    if kind == "book":
        store.ingest_book(
            item["symbol"], item.get("bid"), item.get("ask"),
            item.get("bid_size", 0.0), item.get("ask_size", 0.0),
            item.get("seq", 0), item.get("ts"))
        status = "book"
    elif kind == "event":
        raw = dict(item["raw"])
        if "event_id" not in raw:
            raw["event_id"] = default_event_id
        try:
            event = normalize(raw, now=now)
        except NormalizeError:
            log.warning("dropping malformed stream item", exc_info=True)
            return "dropped"
        if item.get("client_derived"):
            event = replace(event, client_derived=True)
        status = "applied" if store.apply(event, source=source) else "duplicate"
    else:
        log.warning("unknown stream item kind %r", kind)
        return "unknown"
    if on_item is not None:
        on_item(item)
    return status


class PollingConsumer:
    """Drive any :class:`EventSource` on a fixed poll interval.

    The retail path has no durable-read/recovery step: each poll *is* the
    durable read (full RFQ list diff), and the source emits only
    ``rfq_created`` / ``rfq_updated`` / ``rfq_closed`` plus book snapshots,
    which flow through the same ``_dispatch_source_item`` path as stream
    items.

    ``poll_once()`` is synchronous and lets source errors propagate
    (:class:`StreamDisconnected` included) so tests and the dashboard get
    precise control; the background thread started by ``start()`` converts
    failures into backoff + retry, preserving the pacing contract.
    """

    def __init__(self, source: EventSource, store: Any, *,
                 poll_interval_s: float = 5.0,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 source_label: str = "poller",
                 backoff_initial_s: float = 1.0,
                 backoff_max_s: float = 30.0) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self._source = source
        self._store = store
        self._interval = poll_interval_s
        self._on_event = on_event
        self._source_label = source_label
        self._backoff_initial = backoff_initial_s
        self._backoff_max = backoff_max_s
        self._backoff_s = backoff_initial_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.polls = 0
        self.items_seen = 0
        self.items_applied = 0
        self.last_error: Optional[str] = None

    def poll_once(self, now: Optional[datetime] = None) -> int:
        """Run one poll and dispatch every item; returns items dispatched.

        Raises whatever the source raises (``StreamDisconnected`` on a
        transport drop). The item count includes book snapshots.
        """
        now = now or datetime.now(timezone.utc)
        items = self._source.poll(now)
        count = 0
        for item in items:
            self.items_seen += 1
            status = _dispatch_source_item(
                self._store,
                item,
                on_item=self._on_event,
                now=now,
                source=self._source_label,
                default_event_id=f"poller:{self.polls}:{count}",
            )
            if status == "applied":
                self.items_applied += 1
            count += 1
        self.polls += 1
        self.last_error = None
        return count

    def start(self) -> "PollingConsumer":
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="rfq-poller", daemon=True)
        self._thread.start()
        log.info("polling consumer started (interval %.1fs, paper mode: "
                 "outbound quotes NOT SENT)", self._interval)
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        log.info("polling consumer stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except StreamDisconnected as exc:
                log.warning("polling source disconnected: %s", exc)
                self._backoff()
            except Exception:
                # Log the class only: source errors must never carry
                # credential material into the logs.
                log.warning("poll failed", exc_info=True)
                self._backoff()
            else:
                self._backoff_s = self._backoff_initial
                self._stop.wait(self._interval)

    def _backoff(self) -> None:
        delay = self._backoff_s
        log.info("poll backoff: sleeping %.1fs", delay)
        self._stop.wait(delay)
        self._backoff_s = min(self._backoff_s * 2.0, self._backoff_max)

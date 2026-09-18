"""Run the NFL pricing model on live RFQs we want to quote, and log the bid/ask.

The live feed can deliver a couple of hundred RFQs a second and pricing needs
network reads (leg books) plus a numeric solve, so pricing never runs on the
polling thread. :class:`LiveQuoter` takes RFQs on a bounded queue and a pool
of worker threads prices them concurrently, writing every outcome -- quote or
decline -- to the
durable :class:`~combo_mm.quote_selections.QuoteSelectionStore` and to the log:

    QUOTE rfq=<id> BUY YES 25 shares | bid 0.271 / ask 0.333 (fair 0.302,
    naive 0.329, corr -270 bps) | NOT SENT (paper)

Two triggers:

- ``auto`` -- the RFQ passed the live screen (:mod:`combo_mm.rfq_screen`):
  an NFL game contributes two or more legs, which is exactly the flow the
  correlation model exists for.
- ``manual`` -- the dashboard's "Quote this RFQ" button.

Paper only. Nothing in this module (or its imports) can submit a quote; the
price is computed, logged, and stored.
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from combo_mm.nfl.live_pricer import LiveQuote, LiveRfq, NflLivePricer
from combo_mm.quote_selections import QuoteSelectionStore

log = logging.getLogger(__name__)

__all__ = ["LiveQuoter"]


class LiveQuoter:
    """Queue + worker pool around :class:`NflLivePricer`; stores every priced quote.

    ``workers`` is the number of pricing threads draining the queue. The
    pricer, book source, params provider, and quote store are all safe for
    concurrent use (each guards its shared caches with a lock), so raising
    ``workers`` on a multi-core machine cuts the queue wait that dominates
    end-to-end quote latency during RFQ bursts. Pricing decisions stay
    per-RFQ independent, so completion order across workers does not matter.
    """

    def __init__(self, pricer: NflLivePricer, store: QuoteSelectionStore, *,
                 max_queue: int = 500, workers: int = 1, start_worker: bool = True,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 on_decision: Optional[Callable[[LiveRfq, LiveQuote, datetime, datetime], None]] = None) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self.pricer = pricer
        self.store = store
        self.workers = workers
        self._clock = clock
        self.on_decision = on_decision
        self._queue: "queue.Queue[tuple[LiveRfq, str]]" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._warmed = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self.submitted = 0
        self.priced = 0
        self.quoted = 0
        self.declined = 0
        self.dropped = 0
        self.errors = 0
        self.last_error: Optional[str] = None
        self.last_quote: Optional[LiveQuote] = None
        if start_worker:
            self.start()

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> "LiveQuoter":
        if any(t.is_alive() for t in self._threads):
            return self
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._run, name=f"live-quoter-{i}", daemon=True)
            for i in range(self.workers)
        ]
        for t in self._threads:
            t.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    # -- submission -----------------------------------------------------------
    def submit(self, rfq: LiveRfq, trigger: str = "auto") -> bool:
        """Queue an RFQ for pricing; False if already priced for this trigger or the queue is full."""
        key = (rfq.rfq_id, trigger)
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            self.submitted += 1
        try:
            self._queue.put_nowait((rfq, trigger))
        except queue.Full:
            with self._lock:
                self.dropped += 1
                self.submitted -= 1
                self._seen.discard(key)
            log.warning("live quoter queue full; dropped rfq=%s", rfq.rfq_id)
            # The caller records a durable queue-full decline if needed.
            return False
        return True

    def price_now(self, rfq: LiveRfq, trigger: str = "manual") -> LiveQuote:
        """Price one RFQ on the calling thread (the dashboard button, and tests)."""
        return self._handle(rfq, trigger)

    def drain(self, timeout: float = 0.0) -> int:
        """Price everything queued, on the calling thread. Returns how many were priced."""
        n = 0
        while True:
            try:
                rfq, trigger = self._queue.get(timeout=timeout) if timeout else self._queue.get_nowait()
            except queue.Empty:
                return n
            self._handle(rfq, trigger)
            self._queue.task_done()
            n += 1

    # -- worker ---------------------------------------------------------------
    def _run(self) -> None:
        # Warmup once: the lazy numpy/scipy imports and the params read cost
        # seconds, and only the first worker to get here pays for them.
        if not self._warmed.is_set():
            with self._lock:
                if not self._warmed.is_set():
                    try:
                        self.pricer.warmup()   # imports + params off the first RFQ's clock
                    except Exception as exc:
                        self.last_error = f"warmup {type(exc).__name__}"
                        log.warning("pricer warmup failed: %s", type(exc).__name__)
                    finally:
                        self._warmed.set()
        while not self._stop.is_set():
            try:
                rfq, trigger = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._handle(rfq, trigger)
            finally:
                self._queue.task_done()

    def _handle(self, rfq: LiveRfq, trigger: str) -> LiveQuote:
        started = self._clock()
        quote = self.pricer.price(rfq, now=started)
        with self._lock:
            self.priced += 1
            if quote.quoted:
                self.quoted += 1
            else:
                self.declined += 1
                if quote.reason_code == "PRICER_ERROR":
                    self.errors += 1
                    self.last_error = quote.reason_detail
            self.last_quote = quote
        if quote.quoted:
            log.info("QUOTE rfq=%s %s %s %s %s | bid %.3f / ask %.3f (fair %.4f, naive %.4f, "
                     "corr %+.0f bps, confidence %.2f) | NOT SENT (paper)",
                     rfq.rfq_id, rfq.direction, rfq.side, rfq.size, rfq.size_unit,
                     quote.bid or 0.0, quote.ask or 0.0, quote.fair or 0.0,
                     quote.naive_yes or 0.0, quote.corr_adjustment_bps or 0.0, quote.confidence)
        else:
            log.info("no quote rfq=%s reason=%s (%s)", rfq.rfq_id, quote.reason_code,
                     quote.reason_detail)
        try:
            self.store.record_priced_quote(quote.to_dict(), trigger)
        except Exception as exc:  # storage trouble must not kill the worker
            with self._lock:
                self.errors += 1
                self.last_error = f"store {type(exc).__name__}"
            log.warning("failed to store priced quote: %s", type(exc).__name__)
        if self.on_decision is not None:
            try:
                self.on_decision(rfq, quote, started, self._clock())
            except Exception as exc:
                with self._lock:
                    self.errors += 1
                    self.last_error = f"decision {type(exc).__name__}"
                log.warning("failed to record live decision: %s", type(exc).__name__)
        return quote

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"submitted": self.submitted, "priced": self.priced, "quoted": self.quoted,
                    "declined": self.declined, "dropped": self.dropped, "errors": self.errors,
                    "queued": self.queued, "last_error": self.last_error}

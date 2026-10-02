"""Keep the capture database's write-ahead log from growing without bound.

SQLite only recycles the ``-wal`` file when a checkpoint can finish, and a
checkpoint cannot pass a page that an open reader still needs. On a busy
capture (hundreds of RFQs/s plus a dashboard reading the same file) the
automatic checkpoint can fall behind for hours; the log then reached gigabytes,
which slows every read and write. :class:`WalCheckpointer` runs a PASSIVE
checkpoint on a timer (never waits on readers) and, when the log is still
large afterwards, a short-budget TRUNCATE to reset the file.

It runs on its own thread: a checkpoint that waits on a reader must not hold
up the polling loop.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from combo_mm.store import EventStore

log = logging.getLogger(__name__)

__all__ = ["WalCheckpointer"]


class WalCheckpointer:
    def __init__(self, store: EventStore, *, interval_s: float = 30.0,
                 truncate_above_bytes: int = 256 * 1024 * 1024) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.store = store
        self.interval_s = interval_s
        self.truncate_above_bytes = truncate_above_bytes
        self.checkpoints = 0
        self.truncates = 0
        self.last_error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "WalCheckpointer":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wal-checkpoint",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def run_once(self) -> None:
        """One maintenance pass (also called directly by tests)."""
        try:
            busy, wal_pages, moved = self.store.checkpoint("PASSIVE")
            self.checkpoints += 1
            size = self.store.wal_bytes()
            if size > self.truncate_above_bytes:
                busy, _, _ = self.store.checkpoint("TRUNCATE")
                self.truncates += 1
                after = self.store.wal_bytes()
                if busy:
                    log.warning("WAL is %.0f MB and a reader is blocking TRUNCATE "
                                "(%d/%d pages checkpointed); will retry",
                                size / 1e6, moved, wal_pages)
                else:
                    log.info("WAL truncated: %.0f MB -> %.0f MB", size / 1e6, after / 1e6)
            self.last_error = None
        except Exception as exc:  # maintenance must never take the capture down
            self.last_error = type(exc).__name__
            log.warning("WAL checkpoint failed: %s", type(exc).__name__)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.run_once()

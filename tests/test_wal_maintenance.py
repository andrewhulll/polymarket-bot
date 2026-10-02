"""WAL checkpointing keeps the capture database's log file bounded."""
from __future__ import annotations

import os

import pytest

from combo_mm.store import WAL_SIZE_LIMIT_BYTES, EventStore
from combo_mm.wal_maintenance import WalCheckpointer


def _fill(store, n=300):
    with store._conn:
        store._conn.executemany(
            "INSERT INTO rfq(rfq_id,symbol,status) VALUES (?,?,'OPEN')",
            [(f"R{i}", "C" * 200) for i in range(n)])


def test_checkpoint_reports_and_validates(tmp_path):
    store = EventStore(str(tmp_path / "c.db"))
    _fill(store)
    busy, wal_pages, moved = store.checkpoint("PASSIVE")
    assert busy == 0 and wal_pages >= moved >= 0
    with pytest.raises(ValueError):
        store.checkpoint("DROP TABLE rfq")
    store.close()


def test_in_memory_store_has_no_wal():
    assert EventStore().wal_bytes() == 0


def test_journal_size_limit_is_set(tmp_path):
    store = EventStore(str(tmp_path / "c.db"))
    assert store._conn.execute("PRAGMA journal_size_limit").fetchone()[0] == WAL_SIZE_LIMIT_BYTES
    store.close()


def test_truncate_resets_a_large_wal(tmp_path):
    store = EventStore(str(tmp_path / "c.db"))
    store._conn.execute("PRAGMA wal_autocheckpoint=0")    # let the log grow
    _fill(store, 2000)
    before = store.wal_bytes()
    assert before > 100_000
    WalCheckpointer(store, truncate_above_bytes=1).run_once()
    assert store.wal_bytes() < before / 10
    store.close()


def test_blocked_truncate_is_logged_not_raised(tmp_path, caplog):
    path = str(tmp_path / "c.db")
    store = EventStore(path)
    store._conn.execute("PRAGMA wal_autocheckpoint=0")
    _fill(store, 500)
    import sqlite3
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM rfq").fetchone()     # pins the current WAL snapshot
    _fill_more = sqlite3.connect(path)
    _fill_more.execute("INSERT INTO rfq(rfq_id,symbol,status) VALUES ('LATE','x','OPEN')")
    _fill_more.commit()
    checkpointer = WalCheckpointer(store, truncate_above_bytes=1)
    with caplog.at_level("WARNING"):
        checkpointer.run_once()          # must neither raise nor hang
    assert checkpointer.last_error is None
    assert "blocking TRUNCATE" in caplog.text
    reader.close()
    _fill_more.close()
    store.close()


def test_background_thread_runs_and_stops(tmp_path):
    store = EventStore(str(tmp_path / "c.db"))
    checkpointer = WalCheckpointer(store, interval_s=0.05).start()
    deadline = 50
    while checkpointer.checkpoints == 0 and deadline:
        import time
        time.sleep(0.05)
        deadline -= 1
    checkpointer.stop()
    assert checkpointer.checkpoints >= 1
    assert checkpointer._thread is None
    store.close()


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        WalCheckpointer(EventStore(), interval_s=0)

"""Dashboard capture startup and duplicate-reader protection."""
from datetime import datetime, timezone
import sqlite3
from unittest.mock import Mock

from combo_mm.capture_process import CaptureLock, ensure_capture_running


def test_capture_lock_allows_only_one_owner(tmp_path):
    path = tmp_path / "rfq_capture.lock"
    first, second = CaptureLock(path), CaptureLock(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_fresh_reader_heartbeat_prevents_launch(tmp_path, monkeypatch):
    data_dir = tmp_path / "data" / "live"
    data_dir.mkdir(parents=True)
    with sqlite3.connect(data_dir / "rfq_capture.db") as db:
        db.execute("CREATE TABLE live_engine_health (id INTEGER, heartbeat_at TEXT)")
        db.execute("INSERT INTO live_engine_health VALUES (1, ?)",
                   (datetime.now(timezone.utc).isoformat(),))
    popen = Mock(side_effect=AssertionError("duplicate capture"))
    monkeypatch.setattr("combo_mm.capture_process.subprocess.Popen", popen)
    assert ensure_capture_running(tmp_path) is None
    popen.assert_not_called()


def test_missing_reader_is_started(tmp_path, monkeypatch):
    monkeypatch.setattr("combo_mm.capture_process.GatewayCredentials.from_env",
                        lambda: object())
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr("combo_mm.capture_process.subprocess.Popen", popen)
    acquire = Mock(side_effect=[True, False])
    monkeypatch.setattr("combo_mm.capture_process.CaptureLock.acquire", acquire)
    assert ensure_capture_running(tmp_path) is None
    assert popen.call_args.args[0][-3:] == [str(tmp_path / "scripts" / "capture_live_rfqs.py"),
                                              "--data-dir", str(tmp_path / "data" / "live")]

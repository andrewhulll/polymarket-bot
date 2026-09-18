"""Start the receive-only RFQ capture once for a dashboard host."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from combo_mm.intl_gateway import GatewayCredentials


class CaptureLock:
    """A process-held lock that is released by the OS when its owner exits."""

    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.file.close()
            self.file = None
            return False
        return True

    def release(self) -> None:
        if self.file is None:
            return
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()
        self.file = None


def _recent_heartbeat(db_path: Path, max_age_s: float = 10.0) -> bool:
    """Recognize a reader started before the process lock was introduced."""
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True,
                             timeout=0.2) as db:
            row = db.execute("SELECT heartbeat_at FROM live_engine_health WHERE id=1").fetchone()
        if row is None:
            return False
        age = (datetime.now(timezone.utc) -
               datetime.fromisoformat(row[0].replace("Z", "+00:00"))).total_seconds()
        return age < max_age_s
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False


def ensure_capture_running(repo: Path) -> Optional[str]:
    """Return an error for the UI, or None when capture is running."""
    data_dir = repo / "data" / "live"
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = CaptureLock(data_dir / "rfq_capture.lock")
    if not lock.acquire():
        return None
    lock.release()
    if _recent_heartbeat(data_dir / "rfq_capture.db"):
        return None
    try:
        GatewayCredentials.from_env()
    except Exception as exc:
        return f"RFQ capture could not start: {type(exc).__name__}: {exc}"

    script = repo / "scripts" / "capture_live_rfqs.py"
    log_path = data_dir / "capture.log"
    try:
        with log_path.open("a", encoding="utf-8") as log:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {
                "start_new_session": True}
            process = subprocess.Popen(
                [sys.executable, str(script), "--data-dir", str(data_dir)],
                cwd=repo, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, **kwargs)
        for _ in range(30):
            if process.poll() is not None:
                return f"RFQ capture exited during startup; see {log_path}."
            probe = CaptureLock(data_dir / "rfq_capture.lock")
            if not probe.acquire():
                return None
            probe.release()
            time.sleep(0.1)
        return f"RFQ capture did not become ready; see {log_path}."
    except OSError as exc:
        return f"RFQ capture could not start: {exc}"

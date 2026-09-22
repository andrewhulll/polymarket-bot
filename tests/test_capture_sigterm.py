"""SIGTERM drains the capture loop cleanly so a supervisor can restart it.

Runs the real scripts/capture_live_rfqs.py with dummy gateway credentials
(the adapter's websocket thread fails to connect in the background, which
is fine -- the main loop still ticks heartbeats). SIGTERM must stop the
loop, run the finally-drain (adapter + store + lock release), and exit 0.
A supervisor-spawned replacement can then take the lock immediately.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from combo_mm.capture_process import CaptureLock, read_heartbeat_age_s

REPO = Path(__file__).resolve().parents[1]
DUMMY_CREDS = {
    "POLYMARKET_API_KEY": "dummy-key",
    "POLYMARKET_SECRET": "dummy-secret",
    "POLYMARKET_PASSPHRASE": "dummy-passphrase",
    "POLYMARKET_ADDRESS": "0xdummy",
}


@pytest.mark.skipif(os.name == "nt", reason="Windows SIGTERM terminates the child process")
def test_sigterm_drains_and_releases_lock(tmp_path):
    pytest.importorskip("websockets", reason="live capture dependency is optional")
    data_dir = tmp_path / "data" / "live"
    env = dict(os.environ, **DUMMY_CREDS)
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "capture_live_rfqs.py"),
         "--data-dir", str(data_dir), "--log-every", "3600"],
        cwd=REPO, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)
    try:
        # Wait for the first heartbeat (up to 60 s).
        db_path = data_dir / "rfq_capture.db"
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if read_heartbeat_age_s(db_path) is not None:
                break
            time.sleep(0.2)
        if proc.poll() is not None:
            output = proc.stdout.read()
            raise AssertionError(
                f"capture exited before writing a heartbeat\n{output}")
        assert read_heartbeat_age_s(db_path) is not None, \
            "capture never wrote a heartbeat"

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=30)
        assert rc == 0, f"capture exited {rc} on SIGTERM"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    out = proc.stdout.read()

    # The finally-drain ran: clean shutdown log line, no traceback.
    assert "capture stopped" in out
    assert "Traceback" not in out
    # The lock is free: a supervisor-spawned replacement can start at once.
    lock = CaptureLock(data_dir / "rfq_capture.lock")
    assert lock.acquire()
    lock.release()

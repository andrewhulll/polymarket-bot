"""Dead-man's heartbeat checker: freshness helper, exit codes, deploy units."""
import importlib.util
import plistlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from combo_mm.capture_process import (DEFAULT_MAX_HEARTBEAT_AGE_S,
                                      heartbeat_is_fresh,
                                      read_heartbeat_age_s)

REPO = Path(__file__).resolve().parents[1]


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_heartbeat", REPO / "scripts" / "check_heartbeat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_heartbeat(db_path: Path, when: datetime) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS live_engine_health "
                   "(id INTEGER PRIMARY KEY, started_at TEXT, heartbeat_at TEXT)")
        db.execute("INSERT OR REPLACE INTO live_engine_health VALUES (1, ?, ?)",
                   (when.isoformat(), when.isoformat()))


def test_read_heartbeat_age_missing_db(tmp_path):
    assert read_heartbeat_age_s(tmp_path / "rfq_capture.db") is None


def test_read_heartbeat_age_no_row(tmp_path):
    db_path = tmp_path / "rfq_capture.db"
    db_path.touch()
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE live_engine_health (id INTEGER, heartbeat_at TEXT)")
    assert read_heartbeat_age_s(db_path) is None


def test_read_heartbeat_age_fresh_and_stale(tmp_path):
    db_path = tmp_path / "rfq_capture.db"
    now = datetime.now(timezone.utc)
    _write_heartbeat(db_path, now)
    age = read_heartbeat_age_s(db_path)
    assert age is not None and age < 5
    _write_heartbeat(db_path, now - timedelta(hours=2))
    age = read_heartbeat_age_s(db_path)
    assert age is not None and age > 7000


def test_read_heartbeat_age_garbage_timestamp(tmp_path):
    db_path = tmp_path / "rfq_capture.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE live_engine_health (id INTEGER, heartbeat_at TEXT)")
        db.execute("INSERT INTO live_engine_health VALUES (1, 'not-a-time')")
    assert read_heartbeat_age_s(db_path) is None


def test_heartbeat_is_fresh_threshold(tmp_path):
    db_path = tmp_path / "rfq_capture.db"
    _write_heartbeat(db_path, datetime.now(timezone.utc) - timedelta(minutes=5))
    assert heartbeat_is_fresh(db_path, max_age_s=600)
    assert not heartbeat_is_fresh(db_path, max_age_s=60)
    assert heartbeat_is_fresh(db_path)  # default is 10 minutes


def test_checker_exit_codes(tmp_path):
    checker = _load_checker()
    db_path = tmp_path / "rfq_capture.db"
    # Missing database -> stale.
    assert checker.main(["--data-dir", str(tmp_path), "--quiet"]) == 2
    # Fresh heartbeat -> 0.
    _write_heartbeat(db_path, datetime.now(timezone.utc))
    assert checker.main(["--data-dir", str(tmp_path), "--quiet"]) == 0
    # Stale heartbeat -> 2.
    _write_heartbeat(db_path, datetime.now(timezone.utc) - timedelta(hours=1))
    assert checker.main(["--data-dir", str(tmp_path), "--quiet"]) == 2
    # Custom threshold honored.
    _write_heartbeat(db_path, datetime.now(timezone.utc) - timedelta(minutes=5))
    assert checker.main(["--data-dir", str(tmp_path), "--quiet",
                         "--max-age-minutes", "1"]) == 2
    assert checker.main(["--data-dir", str(tmp_path), "--quiet",
                         "--max-age-minutes", "30"]) == 0


def test_checker_output_has_no_secrets(tmp_path, capsys):
    checker = _load_checker()
    _write_heartbeat(tmp_path / "rfq_capture.db", datetime.now(timezone.utc))
    assert checker.main(["--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "POLYMARKET" not in out


def _read_deploy(name: str) -> str:
    return (REPO / "deploy" / name).read_text()


def test_launchd_capture_plist_valid():
    plist = plistlib.loads(_read_deploy("com.polymarket-bot.capture.plist").encode())
    assert plist["Label"] == "com.polymarket-bot.capture"
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    args = plist["ProgramArguments"]
    assert any("capture_live_rfqs.py" in a for a in args)
    assert any("--data-dir" == a for a in args)
    # No secrets baked into the unit; the checkout path is a placeholder
    # the install doc tells the operator to replace.
    text = _read_deploy("com.polymarket-bot.capture.plist")
    assert "POLYMARKET" not in text
    assert "__REPO_DIR__" in text


def test_launchd_heartbeat_check_plist_valid():
    plist = plistlib.loads(_read_deploy("com.polymarket-bot.heartbeat-check.plist").encode())
    assert plist["Label"] == "com.polymarket-bot.heartbeat-check"
    assert plist["StartInterval"] == 300
    assert any("check_heartbeat.py" in a for a in plist["ProgramArguments"])


def _systemd_section(text: str, section: str) -> dict:
    out, current = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            out.setdefault(current, {})
        elif "=" in line and current is not None:
            key, value = line.split("=", 1)
            out[current][key.strip()] = value.strip()
    return out.get(section, {})


def test_systemd_capture_service_restarts():
    text = _read_deploy("polymarket-bot-capture.service")
    service = _systemd_section(text, "Service")
    assert service["Restart"] == "always"
    assert "RestartSec" in service
    assert "capture_live_rfqs.py" in service["ExecStart"]
    assert "POLYMARKET" not in text


def test_systemd_heartbeat_timer():
    service = _systemd_section(_read_deploy("polymarket-bot-heartbeat-check.service"), "Service")
    assert service["Type"] == "oneshot"
    assert "check_heartbeat.py" in service["ExecStart"]
    timer = _systemd_section(_read_deploy("polymarket-bot-heartbeat-check.timer"), "Timer")
    assert "OnUnitActiveSec" in timer
    install = _systemd_section(_read_deploy("polymarket-bot-heartbeat-check.timer"), "Install")
    assert install.get("WantedBy") == "timers.target"

"""Live dashboard server: read-only JSON API plus a static frontend.

Streamlit re-runs the whole script on every refresh, which makes the live
dashboard flicker and lose scroll position while the capture feed updates.
This server keeps one page loaded in the browser; the page polls small JSON
endpoints and patches the DOM in place, so nothing ever full-refreshes.

Read-only by construction: every endpoint opens the capture database through
:func:`dashboard.live_view_models.connect_readonly`, and the headless capture
process remains the sole writer. There is no POST/PUT/DELETE surface, and
nothing here can submit a quote -- paper or otherwise.

Run from the repository root::

    python -m dashboard.server [--port 8000] [--data-dir data/live]
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from dashboard import live_view_models as vm

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"index.html": "text/html; charset=utf-8",
                "app.js": "application/javascript; charset=utf-8",
                "style.css": "text/css; charset=utf-8"}
PAGE_SIZE = 500


class _Waiting(Exception):
    """The capture database is not there (or not readable) yet."""


def _db_path() -> Path:
    return Path(_CONFIG["data_dir"]) / "rfq_capture.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if not path.exists():
        raise _Waiting(f"{path} not found yet")
    try:
        return vm.connect_readonly(path)
    except (OSError, sqlite3.Error) as exc:
        raise _Waiting(f"{path} unreadable ({type(exc).__name__})")


def _count(conn: sqlite3.Connection, only_quotable: bool) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM rfq r LEFT JOIN rfq_screen s USING (rfq_id) "
        "WHERE (? = 0 OR s.screen = 'QUOTABLE')", (int(only_quotable),)).fetchone()[0]


def _pricing_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM rfq_screen WHERE screen='QUOTABLE'").fetchone()[0]


def _json(payload: Any, status: int = 200) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(payload).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "LiveDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the log quiet
        log.debug(fmt, *args)

    # -- routing ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/":
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                name = unquote(path[len("/static/"):])
                if name in STATIC_FILES:
                    self._serve_static(name)
                else:
                    self._respond(*_json({"error": "not found"}, 404))
            elif path == "/api/health":
                self._respond(*self._api_health())
            elif path == "/api/rfqs":
                self._respond(*self._api_rfqs(query))
            elif path == "/api/pricing":
                self._respond(*self._api_pricing(query))
            elif path.startswith("/api/pricing/"):
                self._respond(*self._api_pricing_one(unquote(path[len("/api/pricing/"):])))
            elif path == "/api/performance":
                self._respond(*self._api_performance())
            elif path == "/api/engine":
                self._respond(*self._api_engine())
            else:
                self._respond(*_json({"error": "not found"}, 404))
        except _Waiting as exc:
            self._respond(*_json({"waiting": True, "detail": str(exc)}))
        except (OSError, sqlite3.Error) as exc:
            log.warning("dashboard api error: %s", type(exc).__name__)
            self._respond(*_json({"error": f"database error ({type(exc).__name__})"}, 503))
        except Exception:  # never leak a traceback to the browser
            log.exception("dashboard api blew up")
            self._respond(*_json({"error": "internal error"}, 500))

    # -- responses --------------------------------------------------------
    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, name: str) -> None:
        data = (STATIC_DIR / name).read_bytes()
        self._respond(200, STATIC_FILES[name], data)

    # -- api --------------------------------------------------------------
    def _api_health(self) -> tuple[int, str, bytes]:
        try:
            with closing(_connect()):
                waiting = False
        except _Waiting:
            waiting = True
        return _json({"ok": True, "waiting": waiting,
                      "db": str(_db_path()), "page_size": PAGE_SIZE})

    def _api_rfqs(self, query: dict) -> tuple[int, str, bytes]:
        only = query.get("only_quotable", ["0"])[0] == "1"
        page = max(1, int(query.get("page", ["1"])[0] or 1))
        with closing(_connect()) as conn:
            rows = vm.rfqs(conn, only_quotable=only, limit=PAGE_SIZE,
                           offset=(page - 1) * PAGE_SIZE)
            return _json({"total": _count(conn, only), "page": page,
                          "page_size": PAGE_SIZE, "rows": rows})

    def _api_pricing(self, query: dict) -> tuple[int, str, bytes]:
        page = max(1, int(query.get("page", ["1"])[0] or 1))
        with closing(_connect()) as conn:
            rows = vm.pricing(conn, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
            return _json({"total": _pricing_count(conn), "page": page,
                          "page_size": PAGE_SIZE, "rows": rows})

    def _api_pricing_one(self, rfq_id: str) -> tuple[int, str, bytes]:
        with closing(_connect()) as conn:
            rows = vm.pricing(conn, limit=1, rfq_id=rfq_id)
            if not rows:
                return _json({"error": "unknown rfq"}, 404)
            return _json(rows[0])

    def _api_performance(self) -> tuple[int, str, bytes]:
        with closing(_connect()) as conn:
            return _json(vm.performance(conn))

    def _api_engine(self) -> tuple[int, str, bytes]:
        from combo_mm.config import PipelineConfig
        with closing(_connect()) as conn:
            return _json(vm.engine_status(conn, PipelineConfig().quote_latency_budget_ms))


_CONFIG = {"data_dir": str(REPO / "data" / "live")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=str(REPO / "data" / "live"),
                        help="capture output directory (default: data/live)")
    args = parser.parse_args(argv)
    _CONFIG["data_dir"] = args.data_dir
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    log.info("live dashboard at http://127.0.0.1:%d (read-only; db=%s)",
             args.port, _db_path())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

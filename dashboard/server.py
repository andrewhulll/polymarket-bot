"""Live dashboard server: read-only JSON API plus a static frontend.

Streamlit re-runs the whole script on every refresh, which makes the live
dashboard flicker and lose scroll position while the capture feed updates.
This server keeps one page loaded in the browser; the page polls small JSON
endpoints and patches the DOM in place, so nothing ever full-refreshes.

Read-only by construction: every live endpoint opens the capture database through
:func:`dashboard.live_view_models.connect_readonly`, and the headless capture
process remains the sole writer. The only write-capable routes are two tightly
allowlisted NFL research runners (``/api/nfl/run`` and ``/api/nfl/explorer/price``),
which execute only the whitelisted research scripts against the repo's research
inputs -- they can never touch the live database, credentials, or the network.
Nothing here can submit a quote -- paper or otherwise.

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
                "style.css": "text/css; charset=utf-8",
                "js/core.js": "application/javascript; charset=utf-8",
                "js/rfqs.js": "application/javascript; charset=utf-8",
                "js/pricing.js": "application/javascript; charset=utf-8",
                "js/performance.js": "application/javascript; charset=utf-8",
                "js/engine.js": "application/javascript; charset=utf-8",
                "js/nfl.js": "application/javascript; charset=utf-8",
                "vendor/vega.min.js": "application/javascript; charset=utf-8",
                "vendor/vega-lite.min.js": "application/javascript; charset=utf-8",
                "vendor/vega-embed.min.js": "application/javascript; charset=utf-8"}
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


def _pricing_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM rfq_screen WHERE screen='QUOTABLE'").fetchone()[0]


def _json(payload: Any, status: int = 200) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(payload).encode()


def _nfl_api():
    """The NFL research module, imported lazily. It imports cleanly even
    without the research stack (pandas/altair); handlers check DEPS_OK and
    return 503 for compute-heavy endpoints in that case."""
    from dashboard import nfl_api
    return nfl_api


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
            elif path.startswith("/api/rfqs/"):
                self._respond(*self._api_rfqs_one(unquote(path[len("/api/rfqs/"):])))
            elif path == "/api/pricing":
                self._respond(*self._api_pricing(query))
            elif path.startswith("/api/pricing/"):
                self._respond(*self._api_pricing_one(unquote(path[len("/api/pricing/"):])))
            elif path == "/api/performance":
                self._respond(*self._api_performance())
            elif path == "/api/fills":
                self._respond(*self._api_fills(query))
            elif path == "/api/exposure":
                self._respond(*self._api_exposure())
            elif path == "/api/latency/histogram":
                self._respond(*self._api_latency_histogram(query))
            elif path == "/api/risk":
                self._respond(*self._api_risk())
            elif path == "/api/engine":
                self._respond(*self._api_engine())
            elif path == "/api/nfl/meta":
                self._respond(*self._api_nfl_meta())
            elif path == "/api/nfl/games":
                self._respond(*self._api_nfl_games())
            elif path.startswith("/api/nfl/"):
                self._respond(*self._api_nfl_view(path[len("/api/nfl/"):], query))
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

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        # The only POST surface: the NFL research actions (explorer pricing and
        # the allowlisted offline research scripts). Everything else is read-only.
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/nfl/explorer/price":
                self._respond(*self._api_nfl_explorer_price())
            elif parsed.path == "/api/nfl/run":
                self._respond(*self._api_nfl_run())
            else:
                self._respond(*_json({"error": "not found"}, 404))
        except ValueError as exc:  # bad request body
            self._respond(*_json({"error": str(exc)}, 400))
        except Exception:
            log.exception("dashboard api blew up")
            self._respond(*_json({"error": "internal error"}, 500))

    def _read_json_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 1_000_000:
            raise ValueError("missing or oversized request body")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ValueError("invalid JSON body") from None

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
        screen = query.get("screen", [None])[0] or None
        search = query.get("search", [None])[0] or None
        page = max(1, int(query.get("page", ["1"])[0] or 1))
        with closing(_connect()) as conn:
            rows = vm.rfqs(conn, only_quotable=only, screen=screen, search=search,
                           limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
            total = vm.rfqs_count(conn, only_quotable=only, screen=screen, search=search)
            return _json({"total": total, "page": page,
                          "page_size": PAGE_SIZE, "rows": rows})

    def _api_rfqs_one(self, rfq_id: str) -> tuple[int, str, bytes]:
        with closing(_connect()) as conn:
            detail = vm.rfq_detail(conn, rfq_id)
            if detail is None:
                return _json({"error": "unknown rfq"}, 404)
            return _json(detail)

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

    def _api_fills(self, query: dict) -> tuple[int, str, bytes]:
        page = max(1, int(query.get("page", ["1"])[0] or 1))
        with closing(_connect()) as conn:
            rows = vm.fills(conn, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
            return _json({"total": vm.fills_count(conn), "page": page,
                          "page_size": PAGE_SIZE, "rows": rows})

    def _api_exposure(self) -> tuple[int, str, bytes]:
        with closing(_connect()) as conn:
            return _json(vm.exposure(conn))

    def _api_latency_histogram(self, query: dict) -> tuple[int, str, bytes]:
        try:
            n = int(query.get("n", ["40"])[0] or 40)
        except (TypeError, ValueError):
            n = 40
        n = max(1, min(200, n))
        with closing(_connect()) as conn:
            return _json(vm.latency_histogram(conn, n_buckets=n))

    def _api_risk(self) -> tuple[int, str, bytes]:
        with closing(_connect()) as conn:
            return _json(vm.risk_feed(conn))

    def _api_engine(self) -> tuple[int, str, bytes]:
        try:
            from combo_mm.config import PipelineConfig
            budget = PipelineConfig().quote_latency_budget_ms
        except ImportError:  # running outside the repo (tests); use the default
            budget = 400.0
        with closing(_connect()) as conn:
            return _json(vm.engine_status(conn, budget))

    # -- NFL research -----------------------------------------------------
    def _api_nfl_meta(self) -> tuple[int, str, bytes]:
        nfl = _nfl_api()
        brief = nfl.meta_brief()
        if brief["has_results"] and nfl.DEPS_OK:
            brief["filter_options"] = nfl.filter_options()
        return _json(brief)

    def _api_nfl_games(self) -> tuple[int, str, bytes]:
        nfl = _nfl_api()
        if not nfl.DEPS_OK:
            return _json({"error": "nfl research deps unavailable"}, 503)
        return _json({"games": nfl.explorer_games()})

    def _api_nfl_view(self, view: str, query: dict) -> tuple[int, str, bytes]:
        nfl = _nfl_api()
        if not nfl.DEPS_OK:
            return _json({"error": "nfl research deps unavailable"}, 503)
        if view not in nfl.VIEWS:
            return _json({"error": "unknown nfl view"}, 404)
        data = nfl.load()
        if data is None:
            return _json({"has_results": False, "empty": True, "view": view})
        flat = {k: v[0] for k, v in query.items() if v}
        f = nfl.apply_filters(data, nfl.parse_filter_params(data, flat))
        meta_cfg = data["meta"]
        try:
            if view == "overview":
                payload = nfl.view_overview(f, meta_cfg)
            elif view == "combo_pricing":
                payload = nfl.view_combo_pricing(f, flat.get("heat_metric", "Realized - naive"))
            elif view == "calibration":
                try:
                    width = float(flat.get("width", 0.05) or 0.05)
                except (TypeError, ValueError):
                    width = 0.05
                payload = nfl.view_calibration(f, width, flat.get("combo", "All filtered"))
            elif view == "structure":
                payload = nfl.view_structure(f, meta_cfg, flat.get("param", "sigma_at_mean_points"))
            elif view == "sensitivity":
                fams = flat.get("sens_families")
                families = fams.split(",") if fams else ["spread x total", "ML x total"]
                try:
                    thr = float(flat.get("thr", 0.01) or 0.01)
                except (TypeError, ValueError):
                    thr = 0.01
                payload = nfl.view_sensitivity(f, meta_cfg, families, thr)
            else:  # params
                payload = nfl.view_params(flat.get("params_file"))
        except (ValueError, KeyError) as exc:
            return _json({"error": f"bad nfl params ({exc})"}, 400)
        payload["view"] = view
        payload["has_results"] = True
        return _json(payload)

    def _api_nfl_explorer_price(self) -> tuple[int, str, bytes]:
        nfl = _nfl_api()
        if not nfl.DEPS_OK:
            return _json({"error": "nfl research deps unavailable"}, 503)
        body = self._read_json_body()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return _json(nfl.explorer_price(body))

    def _api_nfl_run(self) -> tuple[int, str, bytes]:
        nfl = _nfl_api()
        body = self._read_json_body()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        name = body.get("script")
        if name not in nfl.RUN_ALLOWLIST:
            return _json({"error": f"unknown script {name!r}; allowed: {sorted(nfl.RUN_ALLOWLIST)}"}, 400)
        options = body.get("options") or {}
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
        return _json(nfl.run_script(name, options))


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

"""Session-only RFQ screener rows served from the capture process's memory.

Only successfully paper-quoted RFQs belong in the durable event store. This
feed lets the separate dashboard show every other screened RFQ while capture
is running, without writing those requests to SQLite, JSONL, or a spool file.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


class TransientRfqs:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: OrderedDict[str, tuple[dict, dict]] = OrderedDict()

    def put(self, row: dict, detail: dict) -> None:
        with self._lock:
            rfq_id = row["rfq_id"]
            self._rows[rfq_id] = (row, detail)
            self._rows.move_to_end(rfq_id)

    def change(self, rfq_id: str, **fields: Any) -> None:
        with self._lock:
            item = self._rows.get(rfq_id)
            if item is None:
                return
            row, detail = item
            row.update(fields)
            detail["rfq"].update(fields)

    def decision(self, rfq_id: str, quote: Any) -> None:
        with self._lock:
            item = self._rows.get(rfq_id)
            if item is None:
                return
            row, detail = item
            row["status"] = "DECLINED"
            row["reason_code"] = quote.reason_code
            detail["rfq"]["status"] = "DECLINED"
            detail["pricing"] = {
                "status": quote.status, "reason_code": quote.reason_code,
                "reason_detail": quote.reason_detail,
                "fair": quote.fair, "naive": quote.naive,
                "priced_at": quote.priced_at,
            }

    def remove(self, rfq_id: str) -> None:
        with self._lock:
            self._rows.pop(rfq_id, None)

    def detail(self, rfq_id: str) -> dict | None:
        with self._lock:
            item = self._rows.get(rfq_id)
            return None if item is None else json.loads(json.dumps(item[1]))

    def page(self, *, limit: int, screen: str | None = None,
             search: str | None = None) -> dict:
        with self._lock:
            rows = []
            total = 0
            needle = search.casefold() if search else None
            for rfq_id in reversed(self._rows):
                row = self._rows[rfq_id][0]
                if screen and row.get("screen") != screen:
                    continue
                if needle and not (str(row.get("rfq_id", "")).casefold().startswith(needle)
                                   or str(row.get("symbol", "")).casefold().startswith(needle)):
                    continue
                total += 1
                if len(rows) < limit:
                    rows.append(dict(row))
            return {"total": total, "rows": rows}


class TransientRfqServer:
    """Local read-only HTTP bridge from capture memory to the dashboard."""

    def __init__(self, feed: TransientRfqs, data_dir: Path, port: int = 8765) -> None:
        source = str(data_dir.resolve())

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/rfqs":
                    query = parse_qs(parsed.query)
                    try:
                        limit = max(0, int(query.get("limit", ["500"])[0]))
                    except ValueError:
                        limit = 500
                    result = feed.page(limit=limit,
                                       screen=query.get("screen", [None])[0],
                                       search=query.get("search", [None])[0])
                    result["source"] = source
                    self._send(result)
                elif parsed.path.startswith("/rfqs/"):
                    detail = feed.detail(unquote(parsed.path[len("/rfqs/"):]))
                    self._send({"source": source, "detail": detail}, 200 if detail else 404)
                else:
                    self._send({"error": "not found"}, 404)

            def _send(self, value: dict, status: int = 200) -> None:
                body = json.dumps(value, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="transient-rfq-server", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

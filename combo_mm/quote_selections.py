"""Durable record of the live RFQs we chose to quote and their accepted quotes.

The live monitor's event store is a fresh temp DB per run; this store is a
separate SQLite file that survives restarts. It holds only:

- ``selected_rfqs`` -- RFQs picked on the dashboard, with a snapshot of the
  request (direction, size, deadline, resolved legs) taken at pick time so
  the record stands on its own once the run's temp DB is gone.
- ``accepted_quotes`` -- the confirmed ``RFQ_TRADE`` broadcast (accepted
  blended price, matched size, execution time) for a *selected* RFQ.
  Trades for RFQs we did not pick are never written.
- ``priced_quotes`` -- what the NFL pricing model
  (:mod:`combo_mm.nfl.live_pricer`) said for an RFQ we wanted to quote: the
  bid and ask we would have shown, the fair value behind them, and the full
  per-leg / per-game detail (or the decline code). One row per (rfq, trigger)
  so an auto-priced RFQ and a later manual re-price are both kept.

Receive-only: selecting an RFQ records intent, pricing records a price;
nothing here sends a quote.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

__all__ = ["QuoteSelectionStore"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class QuoteSelectionStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS selected_rfqs (
                    rfq_id TEXT PRIMARY KEY,
                    selected_at TEXT NOT NULL,
                    direction TEXT,
                    side TEXT,
                    size REAL,
                    size_unit TEXT,
                    submission_deadline TEXT,
                    condition_id TEXT,
                    created_time TEXT,
                    screen TEXT,
                    legs_json TEXT
                );
                CREATE TABLE IF NOT EXISTS priced_quotes (
                    rfq_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    priced_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    reason_detail TEXT,
                    side TEXT,
                    direction TEXT,
                    size REAL,
                    size_unit TEXT,
                    fair REAL,
                    naive REAL,
                    bid REAL,
                    ask REAL,
                    bid_qty TEXT,
                    ask_qty TEXT,
                    response_action TEXT,
                    response_price REAL,
                    corr_adjustment_bps REAL,
                    spread_bps_total REAL,
                    confidence REAL,
                    legs_label TEXT,
                    model_version TEXT,
                    params_version TEXT,
                    latency_ms REAL,
                    after_deadline INTEGER,
                    detail_json TEXT NOT NULL,
                    PRIMARY KEY (rfq_id, trigger)
                );
                CREATE INDEX IF NOT EXISTS idx_priced_at ON priced_quotes(priced_at DESC);
                CREATE TABLE IF NOT EXISTS accepted_quotes (
                    rfq_id TEXT PRIMARY KEY
                        REFERENCES selected_rfqs(rfq_id) ON DELETE CASCADE,
                    price REAL,
                    size REAL,
                    direction TEXT,
                    side TEXT,
                    requester_id TEXT,
                    condition_id TEXT,
                    executed_at TEXT,
                    recorded_at TEXT NOT NULL
                );
                """
            )
        self._selected: Set[str] = {r["rfq_id"] for r in
                                    self._conn.execute("SELECT rfq_id FROM selected_rfqs")}

    # -- selection ------------------------------------------------------------
    def is_selected(self, rfq_id: str) -> bool:
        return rfq_id in self._selected

    def selected_ids(self) -> Set[str]:
        return set(self._selected)

    def select(self, rfq_id: str, snapshot: Optional[Dict[str, Any]] = None) -> bool:
        """Mark an RFQ to quote; returns False if it was already selected."""
        s = snapshot or {}
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO selected_rfqs (rfq_id, selected_at, direction, side, size, "
                "size_unit, submission_deadline, condition_id, created_time, screen, legs_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rfq_id, _now_iso(), s.get("direction"), s.get("side"), s.get("size"),
                 s.get("size_unit"), s.get("submission_deadline"), s.get("condition_id"),
                 s.get("created_time"), s.get("screen"), json.dumps(s.get("legs") or [])))
            self._selected.add(rfq_id)
            return cur.rowcount == 1

    def unselect(self, rfq_id: str) -> None:
        """Drop the selection and any accepted quote stored for it."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM accepted_quotes WHERE rfq_id = ?", (rfq_id,))
            self._conn.execute("DELETE FROM selected_rfqs WHERE rfq_id = ?", (rfq_id,))
            self._selected.discard(rfq_id)

    # -- accepted quotes ------------------------------------------------------
    def record_accepted(self, trade: Dict[str, Any]) -> bool:
        """Store a trade broadcast if its RFQ is selected; returns True when written."""
        rfq_id = str(trade.get("rfq_id") or "")
        if rfq_id not in self._selected:
            return False
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO accepted_quotes (rfq_id, price, size, direction, side, "
                "requester_id, condition_id, executed_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rfq_id, _float(trade.get("price")), _float(trade.get("size")),
                 trade.get("direction"), trade.get("side"), trade.get("requester_id"),
                 trade.get("condition_id"), trade.get("executed_at"), _now_iso()))
            return cur.rowcount == 1

    # -- priced quotes ---------------------------------------------------------
    def record_priced_quote(self, quote: Dict[str, Any], trigger: str = "auto") -> None:
        """Store one pricing outcome (``LiveQuote.to_dict()``); re-pricing replaces it."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO priced_quotes (rfq_id, trigger, priced_at, status, "
                "reason_code, reason_detail, side, direction, size, size_unit, fair, naive, bid, "
                "ask, bid_qty, ask_qty, response_action, response_price, corr_adjustment_bps, "
                "spread_bps_total, confidence, legs_label, model_version, params_version, "
                "latency_ms, after_deadline, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(quote.get("rfq_id")), trigger, quote.get("priced_at"), quote.get("status"),
                 quote.get("reason_code"), quote.get("reason_detail"), quote.get("side"),
                 quote.get("direction"), _float(quote.get("size")), quote.get("size_unit"),
                 _float(quote.get("fair")),
                 _float(quote.get("naive", quote.get("naive_yes"))), _float(quote.get("bid")),
                 _float(quote.get("ask")), quote.get("bid_qty"), quote.get("ask_qty"),
                 quote.get("response_action"), _float(quote.get("response_price")),
                 _float(quote.get("corr_adjustment_bps")), _float(quote.get("spread_bps_total")),
                 _float(quote.get("confidence")), quote.get("legs_label"),
                 quote.get("model_version"), quote.get("params_version"),
                 _float(quote.get("latency_ms")), int(bool(quote.get("after_deadline"))),
                 json.dumps(quote, sort_keys=True, default=str)))

    def list_priced_quotes(self, limit: int = 200, rfq_id: Optional[str] = None
                           ) -> List[Dict[str, Any]]:
        """Priced quotes newest first (optionally for one RFQ), detail parsed back out."""
        sql = "SELECT * FROM priced_quotes"
        args: List[Any] = []
        if rfq_id is not None:
            sql += " WHERE rfq_id = ?"
            args.append(rfq_id)
        sql += " ORDER BY priced_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.pop("detail_json") or "{}")
            except ValueError:
                d["detail"] = {}
            d["after_deadline"] = bool(d.get("after_deadline"))
            out.append(d)
        return out

    def priced_quote_stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT reason_code, COUNT(*) AS n FROM priced_quotes GROUP BY reason_code").fetchall()
        return {r["reason_code"]: r["n"] for r in rows}

    def list_selected(self) -> List[Dict[str, Any]]:
        """Selected RFQs newest first, joined with their accepted quote (if any)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, a.price AS accepted_price, a.size AS accepted_size, "
                "a.executed_at AS accepted_executed_at, a.requester_id AS accepted_requester "
                "FROM selected_rfqs s LEFT JOIN accepted_quotes a USING (rfq_id) "
                "ORDER BY s.selected_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["legs"] = json.loads(d.pop("legs_json") or "[]")
            out.append(d)
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

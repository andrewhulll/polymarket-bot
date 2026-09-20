"""Durable record of the live RFQs we chose to quote and their accepted quotes.

The headless capture and the dashboard share one durable SQLite file. This
store owns the selection, NFL pricing, and settlement tables within it:

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
- ``quote_settlements`` -- what the final scores said about a priced quote:
  per-leg settlement prices, the combo value, the Brier pair (model vs
  naive) and the counterfactual 1-unit edges. Written by
  ``scripts/settle_live_quotes.py``; ``priced_quotes`` is never mutated here.

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
                -- What the NFL model quoted is immutable (priced_quotes); whether it was
                -- right is a separate fact learned later, after the final whistle.
                CREATE TABLE IF NOT EXISTS quote_settlements (
                    rfq_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    settled_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason_detail TEXT,
                    combo_value REAL,
                    n_legs INTEGER NOT NULL,
                    n_legs_settled INTEGER NOT NULL,
                    legs_json TEXT NOT NULL,
                    fair REAL,
                    bid REAL,
                    ask REAL,
                    naive REAL,
                    brier REAL,
                    naive_brier REAL,
                    edge_vs_naive REAL,
                    hypo_edge_bid REAL,
                    hypo_edge_ask REAL,
                    scores_vintage TEXT,
                    model_version TEXT,
                    params_version TEXT,
                    PRIMARY KEY (rfq_id, trigger)
                );
                CREATE INDEX IF NOT EXISTS idx_quote_settlements_status
                    ON quote_settlements(status);
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

    # -- quote settlements ----------------------------------------------------
    # Settling a stored quote after the final whistle. priced_quotes is never
    # mutated here: the model's quote-time record is immutable, settlement is
    # a separate fact. Writes are idempotent on PRIMARY KEY (rfq_id, trigger);
    # ``settled_at`` keeps the first computation so re-runs are stable.

    _SETTLEMENT_COLUMNS = (
        "rfq_id, trigger, settled_at, status, reason_detail, combo_value, "
        "n_legs, n_legs_settled, legs_json, fair, bid, ask, naive, brier, "
        "naive_brier, edge_vs_naive, hypo_edge_bid, hypo_edge_ask, "
        "scores_vintage, model_version, params_version"
    )

    def record_quote_settlement(self, row: Dict[str, Any]) -> None:
        """Write one settlement row (from ``settle_live.settle_quote``).

        Idempotent: ``ON CONFLICT`` replaces the row but keeps the original
        ``settled_at``, so re-running the same input is a no-op.
        """
        legs_json = json.dumps(row.get("legs") or [])
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO quote_settlements ({self._SETTLEMENT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (rfq_id, trigger) DO UPDATE SET "
                "settled_at = quote_settlements.settled_at, "
                "status = excluded.status, reason_detail = excluded.reason_detail, "
                "combo_value = excluded.combo_value, n_legs = excluded.n_legs, "
                "n_legs_settled = excluded.n_legs_settled, legs_json = excluded.legs_json, "
                "fair = excluded.fair, bid = excluded.bid, ask = excluded.ask, "
                "naive = excluded.naive, brier = excluded.brier, "
                "naive_brier = excluded.naive_brier, edge_vs_naive = excluded.edge_vs_naive, "
                "hypo_edge_bid = excluded.hypo_edge_bid, hypo_edge_ask = excluded.hypo_edge_ask, "
                "scores_vintage = excluded.scores_vintage, "
                "model_version = excluded.model_version, params_version = excluded.params_version",
                (row["rfq_id"], row["trigger"], _now_iso(), row["status"],
                 row.get("reason_detail"), row.get("combo_value"),
                 row["n_legs"], row["n_legs_settled"], legs_json,
                 row.get("fair"), row.get("bid"), row.get("ask"), row.get("naive"),
                 row.get("brier"), row.get("naive_brier"), row.get("edge_vs_naive"),
                 row.get("hypo_edge_bid"), row.get("hypo_edge_ask"),
                 row.get("scores_vintage"), row.get("model_version"),
                 row.get("params_version")))

    def get_quote_settlement(self, rfq_id: str, trigger: str = "auto"
                             ) -> Optional[Dict[str, Any]]:
        """One settlement row, legs parsed, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM quote_settlements WHERE rfq_id = ? AND trigger = ?",
                (rfq_id, trigger)).fetchone()
        return self._settlement_dict(row) if row is not None else None

    def list_quote_settlements(self, status: Optional[str] = None,
                               limit: int = 2000) -> List[Dict[str, Any]]:
        """Settlement rows newest first (optionally by status), legs parsed."""
        sql = "SELECT * FROM quote_settlements"
        args: List[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY settled_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._settlement_dict(r) for r in rows]

    def quotes_needing_settlement(self, since: Optional[str] = None,
                                  limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """QUOTED quotes with no terminal settlement row.

        A quote is due when it has no settlement row, or its row is
        PENDING/UNRESOLVED (a newer nflverse pull may now settle it).
        Declines carry no fair price, so only QUOTED rows are scored.
        """
        sql = (
            "SELECT q.* FROM priced_quotes q "
            "LEFT JOIN quote_settlements s ON s.rfq_id = q.rfq_id AND s.trigger = q.trigger "
            "WHERE q.status = 'QUOTED' "
            "AND (s.status IS NULL OR s.status IN ('PENDING', 'UNRESOLVED'))"
        )
        args: List[Any] = []
        if since is not None:
            sql += " AND q.priced_at >= ?"
            args.append(since)
        sql += " ORDER BY q.priced_at ASC, q.rowid ASC"
        if limit is not None:
            sql += " LIMIT ?"
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
            out.append(d)
        return out

    @staticmethod
    def _settlement_dict(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        try:
            d["legs"] = json.loads(d.pop("legs_json") or "[]")
        except ValueError:
            d["legs"] = []
        return d

    # -- accepted quote lookup for settlement ---------------------------------
    def get_accepted_quote(self, rfq_id: str) -> Optional[Dict[str, Any]]:
        """The accepted quote for one RFQ, if the pick-to-quote flow stored one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accepted_quotes WHERE rfq_id = ?", (rfq_id,)).fetchone()
        return dict(row) if row is not None else None


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

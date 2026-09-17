"""Read-only SQLite projections for the live dashboard.

The headless capture process is the sole owner of live writes. These functions
accept a connection so they can be tested without Streamlit or a network.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                           timeout=0.2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, args)]


def rfqs(conn: sqlite3.Connection, *, only_quotable: bool = False,
         limit: int = 500, offset: int = 0) -> list[dict]:
    rows = _rows(conn, """
        SELECT r.*, s.screen, s.n_legs, s.n_resolved, s.n_nfl_legs, s.checks_json,
               s.direction, s.side, s.submission_deadline
        FROM rfq r LEFT JOIN rfq_screen s USING (rfq_id)
        WHERE (? = 0 OR s.screen = 'QUOTABLE')
        ORDER BY r.rowid DESC LIMIT ? OFFSET ?
    """, (int(only_quotable), limit, offset))
    for row in rows:
        row["filters"] = json.loads(row["checks_json"]) if row["checks_json"] else {
            "known legs": row["n_legs"] is not None and row["n_resolved"] == row["n_legs"],
            "NFL same game": row["screen"] == "QUOTABLE",
            "no unsupported same game": row["screen"] != "OTHER_SAME_GAME",
        }
        row["quotable"] = row["screen"] == "QUOTABLE"
    return rows


def pricing(conn: sqlite3.Connection, limit: int = 500, offset: int = 0,
            rfq_id: str | None = None) -> list[dict]:
    rows = _rows(conn, """
        SELECT r.rfq_id, r.created_time, r.status AS rfq_status, s.n_legs,
               p.status, p.reason_code, p.reason_detail, p.response_price,
               p.response_action, p.size, p.size_unit, p.fair, p.naive,
               p.detail_json,
               COALESCE(t.price, (SELECT CASE WHEN s.direction = 'BUY'
                   THEN MIN(q.buy_price) ELSE MAX(q.sell_price) END
                   FROM quotes q WHERE q.rfq_id = r.rfq_id AND q.origin = 'live'))
                   AS market_price,
               CASE WHEN t.price IS NOT NULL THEN 'accepted trade'
                    ELSE 'best observed competitor' END AS market_source,
               t.size AS market_size,
               l.wait_ms, l.compute_ms
        FROM rfq_screen s JOIN rfq r USING (rfq_id)
        LEFT JOIN priced_quotes p ON p.rfq_id = r.rfq_id AND p.trigger = 'auto'
        LEFT JOIN live_trades t ON t.rfq_id = r.rfq_id
        LEFT JOIN quote_latency l ON l.id = (
            SELECT MAX(id) FROM quote_latency WHERE rfq_id = r.rfq_id
              AND source = 'live_capture')
        WHERE s.screen = 'QUOTABLE' AND (? IS NULL OR r.rfq_id = ?)
        ORDER BY r.rowid DESC LIMIT ? OFFSET ?
    """, (rfq_id, rfq_id, limit, offset))
    for row in rows:
        row["status"] = row["status"] or "PENDING"
        row["reason_code"] = row["reason_code"] or "AWAITING_DECISION"
        row["detail"] = json.loads(row.pop("detail_json") or "{}")
        if row["market_price"] is not None and row["response_price"] is not None:
            sign = -1 if row["response_action"] == "BUY" else 1
            row["edge_vs_market"] = sign * (row["market_price"] - row["response_price"])
        else:
            row["edge_vs_market"] = None
    return rows


def performance(conn: sqlite3.Connection) -> dict[str, Any]:
    """Paper fills: our quote would have beaten the observed RFQ trade price."""
    candidates = _rows(conn, """
        SELECT p.*, t.price AS market_price, t.executed_at, r.created_time, s.n_legs
        FROM priced_quotes p JOIN live_trades t USING (rfq_id)
        JOIN rfq r USING (rfq_id) JOIN rfq_screen s USING (rfq_id)
        WHERE p.trigger = 'auto' AND p.status = 'QUOTED'
          AND EXISTS (SELECT 1 FROM quotes q WHERE q.rfq_id = p.rfq_id
                      AND q.status = 'shadow')
        ORDER BY p.priced_at
    """)
    fills = []
    for row in candidates:
        if row["executed_at"] and row["priced_at"]:
            try:
                decided = datetime.fromisoformat(row["priced_at"].replace("Z", "+00:00"))
                executed = datetime.fromisoformat(row["executed_at"].replace("Z", "+00:00"))
                if decided > executed:
                    continue
            except (ValueError, TypeError):
                pass
        price, market = row["response_price"], row["market_price"]
        if price is None or market is None:
            continue
        action = row["response_action"]
        if action == "BUY" and price < market or action == "SELL" and price > market:
            continue
        qty = float(row["size"] or 0)
        if row["size_unit"] == "notional" and price > 0:
            qty /= price
        sign = 1 if action == "BUY" else -1
        detail = json.loads(row["detail_json"] or "{}")
        games = detail.get("games") or []
        game = ", ".join(str(g.get("game") or g.get("label") or "") for g in games) or "Unknown"
        def leg_family(leg: dict) -> str:
            kind = str(leg.get("canonical") or "").upper()
            if "ML" in kind or "MONEYLINE" in kind:
                return "Moneyline"
            if "SPREAD" in kind or "COVER" in kind:
                return "Spread"
            if "TOTAL" in kind or kind in ("OVER", "UNDER"):
                return "Total"
            return "Other"
        family = " + ".join(sorted(leg_family(leg) for leg in detail.get("legs") or []))
        family = family or "Unknown"
        settlements = _rows(conn, "SELECT settlement_price FROM rfq_legs WHERE rfq_id = ?",
                            (row["rfq_id"],))
        settled = bool(settlements) and all(x["settlement_price"] is not None for x in settlements)
        outcome_yes = float(all(x["settlement_price"] >= 0.5 for x in settlements)) if settled else None
        outcome = (1 - outcome_yes if row["side"] == "NO" else outcome_yes) if settled else None
        fills.append({"rfq_id": row["rfq_id"], "time": row["priced_at"],
                      "expected_pnl": sign * (float(row["fair"] or price) - price) * qty,
                      "realized_pnl": sign * (outcome - price) * qty if settled else None,
                      "net_notional": sign * price * qty, "game": game,
                      "family": family, "n_legs": row["n_legs"]})
    cumulative = exposure = 0.0
    curve = []
    for fill in fills:
        cumulative += fill["realized_pnl"] or 0
        exposure += fill["net_notional"] if fill["realized_pnl"] is None else 0
        curve.append({"time": fill["time"], "realized_pnl": cumulative,
                      "net_notional": exposure})
    def breakdown(key: str) -> list[dict]:
        groups = defaultdict(list)
        for fill in fills:
            groups[fill[key]].append(fill)
        return [{key: name, "shadow_fills": len(group),
                 "expected_pnl": sum(x["expected_pnl"] for x in group),
                 "realized_pnl": sum(x["realized_pnl"] or 0 for x in group)}
                for name, group in sorted(groups.items(), key=lambda item: str(item[0]))]
    peak = trough = downswing = upswing = 0.0
    for point in curve:
        value = point["realized_pnl"]
        peak = max(peak, value)
        trough = min(trough, value)
        downswing = min(downswing, value - peak)
        upswing = max(upswing, value - trough)
    quoted = conn.execute("SELECT COUNT(DISTINCT q.rfq_id) FROM quotes q "
                          "JOIN priced_quotes p ON p.rfq_id=q.rfq_id "
                          "WHERE q.status='shadow' AND p.trigger='auto' "
                          "AND p.status='QUOTED'").fetchone()[0]
    return {"quoted": quoted,
            "shadow_fills": len(fills), "win_rate": len(fills) / quoted if quoted else 0,
            "expected_pnl": sum(x["expected_pnl"] for x in fills),
            "realized_pnl": cumulative, "net_notional": exposure,
            "max_downswing": downswing, "max_upswing": upswing,
            "curve": curve, "by_family": breakdown("family"),
            "by_legs": breakdown("n_legs"), "by_game": breakdown("game")}


def engine_status(conn: sqlite3.Connection, budget_ms: float = 400) -> dict[str, Any]:
    health = conn.execute("SELECT * FROM live_engine_health WHERE id=1").fetchone()
    samples = _rows(conn, "SELECT wait_ms, compute_ms FROM quote_latency "
                    "WHERE source='live_capture' ORDER BY id DESC LIMIT 5000")
    def distribution(key: str) -> dict:
        values = sorted(x[key] for x in samples if x[key] is not None)
        if not values:
            return {"p50": None, "p95": None, "max": None}
        return {"p50": values[len(values) // 2],
                "p95": values[min(len(values) - 1, int(len(values) * .95))],
                "max": values[-1]}
    return {"health": dict(health) if health else None,
            "wait": distribution("wait_ms"), "compute": distribution("compute_ms"),
            "budget_ms": budget_ms, "samples": len(samples),
            "reasons": _rows(conn, "SELECT reason_code, COUNT(*) AS n FROM priced_quotes "
                             "WHERE trigger='auto' AND status != 'QUOTED' "
                             "GROUP BY reason_code ORDER BY n DESC"),
            "drafts": _rows(conn, "SELECT quote_id, rfq_id, buy_price, sell_price, "
                            "buy_qty_decimal, sell_qty_decimal, created_time FROM quotes "
                            "WHERE status='shadow' ORDER BY rowid DESC LIMIT 500")}

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
    # Generous busy timeout: the capture process owns all writes, and on
    # Windows a write transaction blocks readers (rollback-journal mode).
    # Reads just wait for the writer instead of failing.
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                           timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, args)]


def rfqs(conn: sqlite3.Connection, *, only_quotable: bool = False,
         screen: str | None = None, search: str | None = None,
         limit: int = 500, offset: int = 0) -> list[dict]:
    rows = _rows(conn, """
        SELECT r.*, s.screen, s.n_legs, s.n_resolved, s.n_nfl_legs, s.checks_json,
               s.direction, s.side, s.submission_deadline
        FROM rfq r LEFT JOIN rfq_screen s USING (rfq_id)
        WHERE (? = 0 OR s.screen = 'QUOTABLE')
          AND (? IS NULL OR s.screen = ?)
          AND (? IS NULL OR r.rfq_id LIKE ? ESCAPE '\\' OR r.symbol LIKE ? ESCAPE '\\')
        ORDER BY r.rowid DESC LIMIT ? OFFSET ?
    """, (int(only_quotable), screen, screen, search,
          _like_prefix(search), _like_prefix(search), limit, offset))
    for row in rows:
        row["filters"] = json.loads(row["checks_json"]) if row["checks_json"] else {
            "known legs": row["n_legs"] is not None and row["n_resolved"] == row["n_legs"],
            "NFL same game": row["screen"] == "QUOTABLE",
            "no unsupported same game": row["screen"] != "OTHER_SAME_GAME",
        }
        row["quotable"] = row["screen"] == "QUOTABLE"
    return rows


def _like_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def rfqs_count(conn: sqlite3.Connection, *, only_quotable: bool = False,
               screen: str | None = None, search: str | None = None) -> int:
    return conn.execute("""
        SELECT COUNT(*) FROM rfq r LEFT JOIN rfq_screen s USING (rfq_id)
        WHERE (? = 0 OR s.screen = 'QUOTABLE')
          AND (? IS NULL OR s.screen = ?)
          AND (? IS NULL OR r.rfq_id LIKE ? ESCAPE '\\' OR r.symbol LIKE ? ESCAPE '\\')
    """, (int(only_quotable), screen, screen, search,
          _like_prefix(search), _like_prefix(search))).fetchone()[0]


def pricing(conn: sqlite3.Connection, limit: int = 500, offset: int = 0,
            rfq_id: str | None = None) -> list[dict]:
    rows = _rows(conn, """
        SELECT r.rfq_id, r.created_time, r.status AS rfq_status, s.n_legs,
               p.status, p.reason_code, p.reason_detail, p.response_price,
               p.response_action, p.size, p.size_unit, p.fair, p.naive,
               p.detail_json, p.priced_at, p.side,
               q.model_version, q.params_version, q.decided_by,
               COALESCE(t.price, p.naive) AS market_price,
               CASE WHEN t.price IS NOT NULL THEN 'accepted trade'
                    ELSE 'leg-implied (naive)' END AS market_source,
               t.size AS market_size,
               l.wait_ms, l.compute_ms
        FROM rfq_screen s JOIN rfq r USING (rfq_id)
        LEFT JOIN priced_quotes p ON p.rfq_id = r.rfq_id AND p.trigger = 'auto'
        LEFT JOIN live_trades t ON t.rfq_id = r.rfq_id
        LEFT JOIN quotes q ON q.rfq_id = r.rfq_id AND q.status = 'shadow'
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
        # A lower ask wins a requester BUY; a higher bid wins a requester SELL.
        sign = 1 if row["response_action"] == "SELL" else -1
        if row["market_price"] is not None and row["response_price"] is not None:
            row["edge_vs_market"] = sign * (row["market_price"] - row["response_price"])
        else:
            row["edge_vs_market"] = None
        if row["market_price"] is not None and row["fair"] is not None:
            row["model_edge"] = sign * (row["fair"] - row["market_price"])
        else:
            row["model_edge"] = None
    return rows


def _fill_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Quoted RFQs that could have filled against the observed market."""
    return _rows(conn, """
        SELECT p.*,
               COALESCE(t.price, p.naive) AS market_price,
               CASE WHEN t.price IS NOT NULL THEN 'accepted trade'
                    ELSE 'leg-implied (naive)' END AS market_source,
               t.executed_at, r.created_time, s.n_legs
        FROM priced_quotes p LEFT JOIN live_trades t ON t.rfq_id = p.rfq_id
        JOIN rfq r ON r.rfq_id = p.rfq_id JOIN rfq_screen s ON s.rfq_id = p.rfq_id
        WHERE p.trigger = 'auto' AND p.status = 'QUOTED'
          AND EXISTS (SELECT 1 FROM quotes q WHERE q.rfq_id = p.rfq_id
                      AND q.status = 'shadow')
        ORDER BY p.priced_at
    """)


def _leg_family(leg: dict) -> str:
    kind = str(leg.get("canonical") or "").upper()
    if "ML" in kind or "MONEYLINE" in kind:
        return "Moneyline"
    if "SPREAD" in kind or "COVER" in kind:
        return "Spread"
    if "TOTAL" in kind or kind in ("OVER", "UNDER"):
        return "Total"
    return "Other"


def _to_fill(conn: sqlite3.Connection, row: dict) -> dict | None:
    """Apply the shadow-fill rule; return the enriched fill or None."""
    if row["executed_at"] and row["priced_at"]:
        try:
            decided = datetime.fromisoformat(row["priced_at"].replace("Z", "+00:00"))
            executed = datetime.fromisoformat(row["executed_at"].replace("Z", "+00:00"))
            if decided > executed:
                return None
        except (ValueError, TypeError):
            pass
    price, market = row["response_price"], row["market_price"]
    if price is None or market is None:
        return None
    action = row["response_action"]
    if action == "BUY" and price < market or action == "SELL" and price > market:
        return None
    qty = float(row["size"] or 0)
    if row["size_unit"] == "notional" and price > 0:
        qty /= price
    sign = 1 if action == "BUY" else -1
    detail = json.loads(row["detail_json"] or "{}")
    games = detail.get("games") or []
    game = ", ".join(str(g.get("game") or g.get("label") or "") for g in games) or "Unknown"
    family = " + ".join(sorted(_leg_family(leg) for leg in detail.get("legs") or []))
    family = family or "Unknown"
    settlements = _rows(conn, "SELECT settlement_price FROM rfq_legs WHERE rfq_id = ?",
                        (row["rfq_id"],))
    settled = [x for x in settlements if x["settlement_price"] is not None]
    outcome_yes = float(all(x["settlement_price"] >= 0.5 for x in settlements)) \
        if settlements and len(settled) == len(settlements) else None
    outcome = (1 - outcome_yes if row["side"] == "NO" else outcome_yes) \
        if outcome_yes is not None else None
    fair = row["fair"]
    return {"rfq_id": row["rfq_id"], "time": row["priced_at"],
            "game": game, "family": family, "n_legs": row["n_legs"],
            "side": row["side"], "response_action": action,
            "size": row["size"], "size_unit": row["size_unit"],
            "naive": row["naive"], "fair": fair, "our_price": price,
            "market_price": market, "market_source": row["market_source"],
            "quote_edge": sign * (market - price),
            "model_edge": sign * (fair - market) if fair is not None else None,
            "expected_pnl": sign * (float(fair or price) - price) * qty,
            "realized_pnl": sign * (outcome - price) * qty if outcome is not None else None,
            "net_notional": sign * price * qty,
            "settled_legs": len(settled), "total_legs": len(settlements)}


def _compute_fills(conn: sqlite3.Connection) -> list[dict]:
    """All shadow fills, oldest first (for cumulative curves)."""
    out = []
    for row in _fill_candidates(conn):
        fill = _to_fill(conn, row)
        if fill is not None:
            out.append(fill)
    return out


def fills(conn: sqlite3.Connection, limit: int = 500, offset: int = 0) -> list[dict]:
    """Enriched shadow-fill ledger, newest first."""
    return _compute_fills(conn)[::-1][offset:offset + limit]


def fills_count(conn: sqlite3.Connection) -> int:
    return len(_compute_fills(conn))


def performance(conn: sqlite3.Connection) -> dict[str, Any]:
    """Paper fills: our quote would have beaten the observed market price.

    The market is the accepted RFQ trade when one is observed; otherwise the
    leg-implied (naive) price stored at decision time. Accepted trades are
    participant-private on the quoter gateway, so the naive fallback is the
    common case in live operation.
    """
    fills = _compute_fills(conn)
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
            "by_legs": breakdown("n_legs"), "by_game": breakdown("game"),
            "by_market_source": breakdown("market_source")}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _kill_switch(conn: sqlite3.Connection) -> dict | None:
    if not _has_table(conn, "kill_switch_events"):
        return None
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT state, ts, trigger FROM kill_switch_events "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


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
                            "WHERE status='shadow' ORDER BY rowid DESC LIMIT 500"),
            "kill_switch": _kill_switch(conn)}


def rfq_detail(conn: sqlite3.Connection, rfq_id: str) -> dict | None:
    """Everything about one RFQ: the row, its screen, legs+books, raw events,
    and the latest pricing decision."""
    conn.row_factory = sqlite3.Row
    rfq = conn.execute("SELECT rfq_id, symbol, creator_user_id, qty_decimal, cash_order_qty, "
                       "created_time, updated_time, rest_remainder, status "
                       "FROM rfq WHERE rfq_id = ?", (rfq_id,)).fetchone()
    if rfq is None:
        return None
    screen = conn.execute("SELECT * FROM rfq_screen WHERE rfq_id = ?", (rfq_id,)).fetchone()
    screen_row = dict(screen) if screen else None
    if screen_row is not None:
        screen_row["checks"] = json.loads(screen_row.pop("checks_json") or "{}")
    legs = _rows(conn, """
        SELECT l.symbol, l.side, l.settlement_price,
               b.bid, b.ask, b.bid_size, b.ask_size, b.seq, b.ts AS book_ts
        FROM rfq_legs l LEFT JOIN books b ON b.symbol = l.symbol
        WHERE l.rfq_id = ?
    """, (rfq_id,))
    events = _rows(conn, "SELECT event_type, source, client_derived, recorded_at "
                         "FROM raw_events WHERE rfq_id = ? ORDER BY id", (rfq_id,))
    pricing = conn.execute("""
        SELECT p.*, q.model_version, q.params_version, q.decided_by
        FROM priced_quotes p
        LEFT JOIN quotes q ON q.rfq_id = p.rfq_id AND q.status = 'shadow'
        WHERE p.rfq_id = ? ORDER BY p.rowid DESC LIMIT 1
    """, (rfq_id,)).fetchone()
    pricing_row = dict(pricing) if pricing else None
    if pricing_row is not None:
        pricing_row["detail"] = json.loads(pricing_row.pop("detail_json") or "{}")
    return {"rfq": dict(rfq), "screen": screen_row, "legs": legs,
            "events": events, "pricing": pricing_row}


def latency_histogram(conn: sqlite3.Connection, n_buckets: int = 40) -> dict:
    """Wait/compute latency histograms for the Engine tab."""
    def histogram(key: str) -> list[dict]:
        values = [r[0] for r in conn.execute(
            f"SELECT {key} FROM quote_latency WHERE source = 'live_capture' "
            f"AND {key} IS NOT NULL")]
        if not values:
            return []
        lo, hi = min(values), max(values)
        n = max(1, min(n_buckets, len(values)))
        if hi == lo:
            return [{"lo": lo, "hi": hi, "n": len(values)}]
        width = (hi - lo) / n
        counts = [0] * n
        for value in values:
            idx = min(n - 1, int((value - lo) / width))
            counts[idx] += 1
        return [{"lo": lo + i * width, "hi": lo + (i + 1) * width, "n": counts[i]}
                for i in range(n)]
    return {"wait": histogram("wait_ms"), "compute": histogram("compute_ms")}


def risk_feed(conn: sqlite3.Connection, limit: int = 100) -> dict:
    """Risk events (newest first) plus the latest kill-switch state."""
    events = _rows(conn, "SELECT ts, rfq_id, quote_id, game_id, action, reason "
                         "FROM risk_events ORDER BY id DESC LIMIT ?", (limit,))
    return {"events": events, "kill_switch": _kill_switch(conn)}


def exposure(conn: sqlite3.Connection, limit: int = 500) -> dict:
    """Latest exposure snapshot plus the trailing series (oldest first)."""
    rows = _rows(conn, "SELECT ts, equity, buying_power, pending_wcl, executed_wcl, total_wcl "
                       "FROM exposure_snapshots ORDER BY id DESC LIMIT ?", (limit,))
    series = rows[::-1]
    return {"latest": series[-1] if series else None, "series": series}

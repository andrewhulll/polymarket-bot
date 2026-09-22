"""Read-only SQLite projections for the live dashboard.

The headless capture process is the sole owner of live writes. These functions
accept a connection so they can be tested without Streamlit or a network.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from combo_mm.backtest.metrics import swings as _swings
from combo_mm.paper_capital import PaperCapitalState, replay_paper_capital


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
         screen: str | None = None, status: str | None = None,
         game: str | None = None, search: str | None = None,
         limit: int = 500, offset: int = 0) -> list[dict]:
    rows = _rows(conn, """
        SELECT r.*, s.screen, s.n_legs, s.n_resolved, s.n_nfl_legs, s.checks_json,
               s.direction, s.side, s.submission_deadline,
               t.price AS trade_price, t.executed_at AS trade_executed_at,
               p.detail_json AS pricing_detail_json
        FROM rfq r LEFT JOIN rfq_screen s USING (rfq_id)
        LEFT JOIN live_trades t USING (rfq_id)
        LEFT JOIN priced_quotes p ON p.rfq_id = r.rfq_id AND p.trigger = 'auto'
        WHERE (? = 0 OR s.screen = 'QUOTABLE')
          AND (? IS NULL OR s.screen = ?)
          AND (? IS NULL OR r.status = ?)
          AND (? IS NULL OR p.detail_json LIKE ? ESCAPE '\\')
          AND (? IS NULL OR r.rfq_id LIKE ? ESCAPE '\\' OR r.symbol LIKE ? ESCAPE '\\')
        ORDER BY r.rowid DESC LIMIT ? OFFSET ?
    """, (int(only_quotable), screen, screen, status, status, game,
          _like_contains(game), search,
          _like_prefix(search), _like_prefix(search), limit, offset))
    for row in rows:
        row["filters"] = json.loads(row["checks_json"]) if row["checks_json"] else {
            "known legs": row["n_legs"] is not None and row["n_resolved"] == row["n_legs"],
            "NFL same game": row["screen"] == "QUOTABLE",
            "no unsupported same game": row["screen"] != "OTHER_SAME_GAME",
        }
        row["quotable"] = row["screen"] == "QUOTABLE"
        detail = json.loads(row.pop("pricing_detail_json") or "{}")
        games = detail.get("games") or []
        row["game"] = ", ".join(str(g.get("game") or g.get("label") or "")
                                  for g in games).strip(", ") or None
        row["family"] = " + ".join(sorted(_leg_family(leg)
                                             for leg in detail.get("legs") or [])) or None
    return rows


def _like_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _like_contains(value: str | None) -> str | None:
    prefix = _like_prefix(value)
    return None if prefix is None else "%" + prefix


def rfqs_count(conn: sqlite3.Connection, *, only_quotable: bool = False,
               screen: str | None = None, status: str | None = None,
               game: str | None = None, search: str | None = None) -> int:
    # The unfiltered feed is the common polling path. Counting through the
    # screen join scans millions of rows on a live capture and can take longer
    # than the browser's refresh interval, leaving the RFQ table blank.
    if search is None and status is None and game is None:
        if screen is None and not only_quotable:
            return conn.execute("SELECT COUNT(*) FROM rfq").fetchone()[0]
        if screen is not None and only_quotable and screen != "QUOTABLE":
            return 0
        if only_quotable or screen == "QUOTABLE":
            # rank 0 is exactly QUOTABLE and has an existing index in the
            # capture schema. Small legacy/test databases may lack rank.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(rfq_screen)")}
            if "rank" in columns:
                return conn.execute("SELECT COUNT(*) FROM rfq_screen WHERE rank = 0").fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM rfq_screen WHERE screen = 'QUOTABLE'").fetchone()[0]
        selected = screen or "QUOTABLE"
        return conn.execute("SELECT COUNT(*) FROM rfq_screen WHERE screen = ?",
                            (selected,)).fetchone()[0]
    return conn.execute("""
        SELECT COUNT(*) FROM rfq r LEFT JOIN rfq_screen s USING (rfq_id)
        LEFT JOIN priced_quotes p ON p.rfq_id = r.rfq_id AND p.trigger = 'auto'
        WHERE (? = 0 OR s.screen = 'QUOTABLE')
          AND (? IS NULL OR s.screen = ?)
          AND (? IS NULL OR r.status = ?)
          AND (? IS NULL OR p.detail_json LIKE ? ESCAPE '\\')
          AND (? IS NULL OR r.rfq_id LIKE ? ESCAPE '\\' OR r.symbol LIKE ? ESCAPE '\\')
    """, (int(only_quotable), screen, screen, status, status, game,
          _like_contains(game), search,
          _like_prefix(search), _like_prefix(search))).fetchone()[0]


def rfq_filter_options(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Observed status and game choices for the historical RFQ filters."""
    statuses = [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT status FROM rfq WHERE status IS NOT NULL ORDER BY status")]
    games = set()
    if _has_table(conn, "priced_quotes"):
        for row in conn.execute("SELECT detail_json FROM priced_quotes WHERE trigger='auto'"):
            try:
                detail = json.loads(row[0] or "{}")
            except (TypeError, ValueError):
                continue
            for item in detail.get("games") or []:
                value = item.get("game") or item.get("label")
                if value:
                    games.add(str(value))
    return {"statuses": statuses, "games": sorted(games)}


def pricing(conn: sqlite3.Connection, limit: int = 500, offset: int = 0,
            rfq_id: str | None = None) -> list[dict]:
    rows = _rows(conn, """
        SELECT r.rfq_id, r.created_time, r.status AS rfq_status, s.n_legs,
               p.status, p.reason_code, p.reason_detail, p.response_price,
               p.response_action, p.size, p.size_unit, p.fair, p.naive,
               p.detail_json, p.priced_at, p.side, p.after_deadline,
               q.model_version, q.params_version, q.decided_by,
               t.price AS market_price,
               CASE WHEN t.price IS NOT NULL THEN 'accepted trade' END AS market_source,
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
    capital = _paper_capital_state(conn)
    for row in rows:
        if row["rfq_id"] in capital.rejected_ids:
            row["status"] = "DECLINED"
            row["reason_code"] = "RISK_CAPITAL"
            row["reason_detail"] = "paper equity was already fully allocated"
            row["response_price"] = None
        row["status"] = row["status"] or "PENDING"
        row["reason_code"] = row["reason_code"] or "AWAITING_DECISION"
        row["detail"] = json.loads(row.pop("detail_json") or "{}")
        # A lower ask wins a requester BUY; a higher bid wins a requester SELL.
        sign = -1 if row["response_action"] == "SELL" else 1
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
    """Quoted RFQs scored as shadow fills.

    Every auto-quoted shadow RFQ lands in the ledger. An observed accepted
    ``RFQ_TRADE`` only *removes* a fill: when the trade price beats our quote
    (a lower ask when we sell, a higher bid when we buy) we lost the auction.
    No observed trade means the quote stands as an assumed win -- an
    optimistic paper assumption, flagged via ``market_source``. Late
    (after-deadline) quotes stay in the ledger, flagged on the fill.
    """
    return _rows(conn, """
        SELECT p.*,
               t.price AS market_price,
               CASE WHEN t.price IS NOT NULL THEN 'accepted trade' END AS market_source,
               t.executed_at, r.created_time, s.n_legs, s.direction
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
    if price is None:
        return None
    action = row["response_action"]
    if market is not None:
        # An observed accepted trade beats our quote -> we lost the auction.
        # No observed trade -> the quote stands as an assumed win.
        if action == "BUY" and price < market or action == "SELL" and price > market:
            return None
    quoted_qty = row.get("bid_qty" if action == "BUY" else "ask_qty")
    if quoted_qty is not None:
        # Risk may have reduced the response side below the requested RFQ size.
        qty = float(quoted_qty)
        size, size_unit = qty, "shares"
    else:
        # Historical quotes did not persist side quantities.
        qty = float(row["size"] or 0)
        if row["size_unit"] == "notional" and price > 0:
            qty /= price
        size, size_unit = row["size"], row["size_unit"]
    if qty <= 0:
        return None
    sign = 1 if action == "BUY" else -1
    detail = json.loads(row["detail_json"] or "{}")
    games = detail.get("games") or []
    game = ", ".join(str(g.get("game") or g.get("label") or "") for g in games) or "Unknown"
    family = " + ".join(sorted(_leg_family(leg) for leg in detail.get("legs") or []))
    family = family or "Unknown"
    settlements = _rows(conn, "SELECT symbol, settlement_price FROM rfq_legs WHERE rfq_id = ?",
                        (row["rfq_id"],))
    settled = [x for x in settlements if x["settlement_price"] is not None]
    outcome_yes = float(all(x["settlement_price"] >= 0.5 for x in settlements)) \
        if settlements and len(settled) == len(settlements) else None
    outcome = (1 - outcome_yes if row["side"] == "NO" else outcome_yes) \
        if outcome_yes is not None else None
    market_keys = tuple(dict.fromkeys(
        str(leg.get("slug") or leg.get("label") or leg.get("position_id"))
        for leg in detail.get("legs") or []
        if leg.get("slug") or leg.get("label") or leg.get("position_id")))
    if not market_keys:
        market_keys = tuple(dict.fromkeys(str(leg["symbol"]) for leg in settlements))
    if not market_keys:
        market_keys = (str(row["rfq_id"]),)
    team_keys = tuple(dict.fromkeys(
        str(team) for entry in games for team in (entry.get("away"), entry.get("home"))
        if team))
    fair = row["fair"]
    return {"rfq_id": row["rfq_id"], "time": row["priced_at"],
            "after_deadline": bool(row["after_deadline"]),
            "game": game, "family": family, "n_legs": row["n_legs"],
            "requester_side": row.get("direction"),
            "market_keys": market_keys, "team_keys": team_keys,
            "quantity": qty,
            "side": row["side"], "response_action": action,
            "size": size, "size_unit": size_unit,
            "requested_size": row["size"], "requested_size_unit": row["size_unit"],
            "naive": row["naive"], "fair": fair, "our_price": price,
            "market_price": market,
            "market_source": row["market_source"] or "no observed trade",
            "quote_edge": sign * (market - price) if market is not None else None,
            "model_edge": (sign * (fair - market)
                           if fair is not None and market is not None else None),
            "expected_pnl": sign * (float(fair or price) - price) * qty,
            "realized_pnl": sign * (outcome - price) * qty if outcome is not None else None,
            "net_notional": sign * price * qty,
            "settled_legs": len(settled), "total_legs": len(settlements)}


def _paper_capital_state(conn: sqlite3.Connection,
                         equity_limit: float | None = None) -> PaperCapitalState:
    if equity_limit is None:
        from combo_mm.inventory import InventoryProvider
        equity_limit = InventoryProvider(_InventoryStore(conn))().equity
    return replay_paper_capital(conn, equity_limit)


def _compute_fills(conn: sqlite3.Connection, equity_limit: float | None = None,
                   capital: PaperCapitalState | None = None) -> list[dict]:
    """Capital-limited shadow fills, oldest first (for cumulative curves)."""
    if not _has_table(conn, "priced_quotes"):
        return []
    capital = capital or _paper_capital_state(conn, equity_limit)
    out = []
    for row in _fill_candidates(conn):
        if row["rfq_id"] in capital.rejected_ids:
            continue
        fill = _to_fill(conn, row)
        if fill is not None:
            if fill["realized_pnl"] is None:
                proposed = fill["net_notional"]
                allocated = capital.allocations.get(row["rfq_id"])
                if allocated is None or abs(allocated) < 1e-9:
                    continue
                fraction = allocated / proposed
                if fraction < 1 - 1e-9:
                    fill["original_size"] = fill["size"]
                    fill["size"] = float(fill["size"]) * fraction
                    fill["quantity"] *= fraction
                    fill["expected_pnl"] *= fraction
                    fill["net_notional"] = allocated
                    fill["capacity_limited"] = True
            out.append(fill)
    return out


def fills(conn: sqlite3.Connection, limit: int = 500, offset: int = 0) -> list[dict]:
    """Enriched shadow-fill ledger, newest first."""
    return _compute_fills(conn)[::-1][offset:offset + limit]


def fills_count(conn: sqlite3.Connection) -> int:
    return len(_compute_fills(conn))


def performance(conn: sqlite3.Connection) -> dict[str, Any]:
    """Paper fills: every auto-quoted shadow RFQ, minus the ones an observed
    accepted trade beat."""
    capital = _paper_capital_state(conn)
    fills = _compute_fills(conn, capital=capital)
    cumulative = exposure = 0.0
    curve = []
    for fill in fills:
        cumulative += fill["realized_pnl"] or 0
        exposure += fill["net_notional"] if fill["realized_pnl"] is None else 0
        exposure = max(-capital.equity, min(capital.equity, exposure))
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
    sw = _swings([(p["time"], p["realized_pnl"]) for p in curve])
    downswing, upswing = -sw["max_downswing"], sw["max_upswing"]
    quoted = len(capital.admitted_ids)
    stored_declines = conn.execute(
        "SELECT COUNT(DISTINCT rfq_id) FROM priced_quotes "
        "WHERE trigger='auto' AND status!='QUOTED'").fetchone()[0]
    received = conn.execute("SELECT COUNT(*) FROM rfq").fetchone()[0]
    return {"quoted": quoted,
            "rfqs_received": received,
            "rfqs_declined": stored_declines + len(capital.rejected_ids),
            "rfqs_executed": conn.execute(
                "SELECT COUNT(DISTINCT rfq_id) FROM fills").fetchone()[0],
            "quote_rate": quoted / max(1, received),
            "shadow_fills": len(fills), "win_rate": len(fills) / quoted if quoted else 0,
            "expected_pnl": sum(x["expected_pnl"] for x in fills),
            "realized_pnl": cumulative, "net_notional": capital.net_notional,
            "max_downswing": downswing, "max_upswing": upswing,
            "curve": curve, "by_family": breakdown("family"),
            "by_legs": breakdown("n_legs"), "by_game": breakdown("game"),
            "by_requester_side": breakdown("requester_side"),
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


def correlation_lift(conn: sqlite3.Connection, window: int = 1000,
                     degenerate_bps: float = 1.0, degenerate_frac: float = 0.95) -> dict[str, Any]:
    """Distribution of |corr_adjustment_bps| over the most recent auto-quoted RFQs.

    ``corr_adjustment_bps = (fair - naive) * 1e4`` (see
    ``combo_mm.nfl.live_pricer``): how much the joint correlation model
    moved the price off the naive independent-leg product. Under
    ``market_lift`` a same-game combo's only source of dependence is
    ``Cov(margin, total) = sigma_home^2 - sigma_away^2``
    (``combo_mm.nfl.params_io.matchup_covariance``), so a params file with
    equal sigmas for every game -- ``league_constant`` with ``var_slope ==
    0`` -- makes every quote's fair value equal its naive price exactly.
    That is a silent failure mode: the RFQ still prices and quotes fine, it
    just isn't using the model the bot exists to run. ``degenerate`` flags
    it: True when ``degenerate_frac`` or more of the sampled quotes show
    less than ``degenerate_bps`` of adjustment.
    """
    columns = {r[1] for r in conn.execute("PRAGMA table_info(priced_quotes)")}
    if "corr_adjustment_bps" not in columns:
        return {"n": 0, "mean_abs_bps": None, "p50_abs_bps": None, "p95_abs_bps": None,
                "max_abs_bps": None, "frac_degenerate": None, "degenerate": None,
                "threshold_bps": degenerate_bps}
    rows = _rows(conn, """
        SELECT corr_adjustment_bps FROM priced_quotes
        WHERE trigger = 'auto' AND status = 'QUOTED' AND corr_adjustment_bps IS NOT NULL
        ORDER BY rowid DESC LIMIT ?
    """, (window,))
    values = sorted(abs(r["corr_adjustment_bps"]) for r in rows)
    n = len(values)
    if n == 0:
        return {"n": 0, "mean_abs_bps": None, "p50_abs_bps": None, "p95_abs_bps": None,
                "max_abs_bps": None, "frac_degenerate": None, "degenerate": None,
                "threshold_bps": degenerate_bps}
    frac_below = sum(1 for v in values if v < degenerate_bps) / n
    return {
        "n": n,
        "mean_abs_bps": sum(values) / n,
        "p50_abs_bps": values[n // 2],
        "p95_abs_bps": values[min(n - 1, int(n * 0.95))],
        "max_abs_bps": values[-1],
        "frac_degenerate": frac_below,
        "degenerate": frac_below >= degenerate_frac,
        "threshold_bps": degenerate_bps,
    }


def engine_status(conn: sqlite3.Connection, budget_ms: float = 400) -> dict[str, Any]:
    health = conn.execute("SELECT * FROM live_engine_health WHERE id=1").fetchone()
    # delivery/queue and fetch/solve are added by a later migration; a stale DB
    # opened read-only may not have them, so only select the columns present.
    have = {r[1] for r in conn.execute("PRAGMA table_info(quote_latency)")}
    cols = [c for c in ("wait_ms", "compute_ms", "delivery_ms", "queue_ms",
                        "fetch_ms", "solve_ms") if c in have]
    samples = _rows(conn, f"SELECT {', '.join(cols)} FROM quote_latency "
                    "WHERE source='live_capture' ORDER BY id DESC LIMIT 5000") if cols else []
    def distribution(key: str) -> dict:
        if key not in have:
            return {"p50": None, "p95": None, "max": None}
        values = sorted(x[key] for x in samples if x[key] is not None)
        if not values:
            return {"p50": None, "p95": None, "max": None}
        return {"p50": values[len(values) // 2],
                "p95": values[min(len(values) - 1, int(len(values) * .95))],
                "max": values[-1]}
    return {"health": dict(health) if health else None,
            "wait": distribution("wait_ms"), "compute": distribution("compute_ms"),
            "delivery": distribution("delivery_ms"), "queue": distribution("queue_ms"),
            "fetch": distribution("fetch_ms"), "solve": distribution("solve_ms"),
            "budget_ms": budget_ms, "samples": len(samples),
            "reasons": _rows(conn, "SELECT reason_code, COUNT(*) AS n FROM priced_quotes "
                             "WHERE trigger='auto' AND status != 'QUOTED' "
                             "GROUP BY reason_code ORDER BY n DESC"),
            "drafts": _rows(conn, "SELECT quote_id, rfq_id, buy_price, sell_price, "
                            "buy_qty_decimal, sell_qty_decimal, created_time FROM quotes "
                            "WHERE status='shadow' ORDER BY rowid DESC LIMIT 500"),
            "kill_switch": _kill_switch(conn),
            "correlation_lift": correlation_lift(conn)}


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
    trade = conn.execute("SELECT price, size, executed_at FROM live_trades WHERE rfq_id = ?",
                         (rfq_id,)).fetchone()
    return {"rfq": dict(rfq), "screen": screen_row, "legs": legs,
            "events": events, "pricing": pricing_row,
            "trade": dict(trade) if trade else None}


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
    events = _rows(conn, "SELECT ts, rfq_id, quote_id, game_id, action, reason, detail_json "
                         "FROM risk_events ORDER BY id DESC LIMIT ?", (limit,))
    for event in events:
        try:
            detail = json.loads(event.pop("detail_json") or "{}").get("detail") or {}
            event["reason_detail"] = detail.get("message", "")
        except (ValueError, TypeError, AttributeError):
            event["reason_detail"] = ""
    return {"events": events, "kill_switch": _kill_switch(conn)}


def exposure(conn: sqlite3.Connection, limit: int = 500) -> dict:
    """Latest exposure snapshot plus the trailing series (oldest first)."""
    rows = _rows(conn, "SELECT ts, equity, buying_power, pending_wcl, executed_wcl, total_wcl "
                       "FROM exposure_snapshots ORDER BY id DESC LIMIT ?", (limit,))
    series = rows[::-1]
    return {"latest": series[-1] if series else None, "series": series}


class _InventoryStore:
    """Read-only adapter exposing ``EventStore.inventory_rows()`` over a raw
    connection, so the dashboard can rebuild the live inventory snapshot
    without opening a writable handle on the capture database. The SELECTs
    mirror ``combo_mm.store.EventStore.inventory_rows`` exactly."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def inventory_rows(self):
        rfqs = [dict(r) for r in self._conn.execute(
            "SELECT r.rfq_id, r.symbol, r.status, r.updated_time, "
            "s.submission_deadline FROM rfq r LEFT JOIN rfq_screen s "
            "ON s.rfq_id=r.rfq_id ORDER BY r.rfq_id")]
        for rfq in rfqs:
            rfq["legs"] = [dict(r) for r in self._conn.execute(
                "SELECT symbol, side, settlement_price FROM rfq_legs "
                "WHERE rfq_id=? ORDER BY rowid", (rfq["rfq_id"],))]
        quotes = [dict(r) for r in self._conn.execute(
            "SELECT quote_id, rfq_id, symbol, status, origin, buy_price, "
            "sell_price, buy_qty_decimal, sell_qty_decimal, created_time "
            "FROM quotes ORDER BY rowid")]
        fills = [dict(r) for r in self._conn.execute(
            "SELECT fill_id, rfq_id, quote_id, symbol, side, price, qty, "
            "executed_time FROM fills ORDER BY fill_id")]
        last = self._conn.execute(
            "SELECT state FROM kill_switch_events ORDER BY id DESC LIMIT 1").fetchone()
        return rfqs, quotes, fills, bool(last and last["state"] == "tripped")


def _quote_game_resolver(conn: sqlite3.Connection):
    """Recover catalog game IDs from captured quote inputs for dashboard inventory."""
    games = {}
    if _has_table(conn, "priced_quotes"):
        for row in conn.execute(
                "SELECT detail_json FROM priced_quotes WHERE trigger='auto' AND status='QUOTED'"):
            try:
                detail = json.loads(row[0] or "{}")
            except (ValueError, TypeError):
                continue
            for leg in detail.get("legs") or ():
                if leg.get("position_id") and leg.get("game"):
                    games[str(leg["position_id"])] = str(leg["game"])
    return games.get


def inventory_state(conn: sqlite3.Connection) -> dict:
    """Current paper inventory rebuilt from the event store.

    Returns the full ``InventoryState`` snapshot (exposures by game/market/team,
    pending vs executed, equity, buying power, realized PnL) plus the latest
    kill-switch event. Backs the dashboard Inventory tab.
    """
    from combo_mm.inventory import InventoryProvider
    provider = InventoryProvider(_InventoryStore(conn),
                                 game_resolver=_quote_game_resolver(conn))
    recorded_state = provider()
    capital = _paper_capital_state(conn, recorded_state.equity)
    paper_fills = _compute_fills(conn, recorded_state.equity, capital)
    # A simulated fill consumes its RFQ's quote. Do not reserve that same
    # draft as pending while also showing it as paper executed exposure.
    consumed_or_rejected = {
        fill["rfq_id"] for fill in paper_fills if fill["realized_pnl"] is None
    } | capital.rejected_ids
    state = provider(exclude_pending_rfqs=consumed_or_rejected)
    snap = state.to_snapshot()
    snap["kill_switch_event"] = _kill_switch(conn)
    paper_executed = defaultdict(float)
    paper_markets = defaultdict(float)
    paper_teams = defaultdict(float)
    paper_net_by_game = defaultdict(float)
    for fill in paper_fills:
        if fill["realized_pnl"] is not None:
            continue
        qty = fill["quantity"]
        price = fill["our_price"]
        loss = qty * (price if fill["response_action"] == "BUY" else 1 - price)
        paper_executed[fill["game"]] += loss
        paper_net_by_game[fill["game"]] += qty if fill["response_action"] == "BUY" else -qty
        for market in fill["market_keys"]:
            paper_markets[market] += loss
        for team in fill["team_keys"]:
            paper_teams[team] += loss
    for field, additions in (("executed", paper_executed),
                             ("markets", paper_markets),
                             ("teams", paper_teams),
                             ("net_by_game", paper_net_by_game)):
        for key, amount in additions.items():
            snap[field][key] = snap[field].get(key, 0.0) + amount
    snap["exposures"] = {
        key: snap["pending"].get(key, 0.0) + snap["executed"].get(key, 0.0)
        for key in sorted(set(snap["pending"]) | set(snap["executed"]))
    }
    snap["paper_wcl"] = sum(paper_executed.values())
    paper_net = capital.net_notional
    snap["paper_net_notional"] = paper_net
    paper_buying_power = min(state.buying_power, state.equity - abs(paper_net))
    snap["buying_power"] = 0.0 if abs(paper_buying_power) < 1e-9 else paper_buying_power
    now = datetime.now(timezone.utc)
    activity = _rows(conn, """
        SELECT q.quote_id, q.rfq_id, q.created_time, q.buy_price, q.sell_price,
               q.buy_qty_decimal, q.sell_qty_decimal, r.status AS rfq_status,
               s.submission_deadline
        FROM quotes q JOIN rfq r ON r.rfq_id=q.rfq_id
        LEFT JOIN rfq_screen s ON s.rfq_id=r.rfq_id
        WHERE q.origin='shadow'
        ORDER BY q.rowid DESC LIMIT 20
    """)
    for row in activity:
        deadline = row["submission_deadline"]
        open_rfq = row["rfq_status"] in ("OPEN", "QUOTED", "RFQ_STATUS_OPEN", "RFQ_STATUS_QUOTED")
        expired = False
        if deadline:
            try:
                expires = (datetime.fromtimestamp(int(deadline) / 1000, timezone.utc)
                           if str(deadline).isdigit() else
                           datetime.fromisoformat(str(deadline).replace("Z", "+00:00")))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                expired = expires <= now
            except (ValueError, OverflowError):
                pass
        row["state"] = ("rejected" if row["rfq_id"] in capital.rejected_ids else
                        "closed" if not open_rfq else
                        "expired" if expired
                        else "open")
        if row["rfq_id"] in capital.rejected_ids:
            row["reason_code"] = "RISK_CAPITAL"
    rejected = _rows(conn, """
        SELECT ts AS created_time, rfq_id, reason AS reason_code, detail_json
        FROM risk_events WHERE action='reject' ORDER BY id DESC LIMIT 20
    """)
    for row in rejected:
        try:
            detail = json.loads(row.pop("detail_json") or "{}").get("detail") or {}
        except (ValueError, TypeError, AttributeError):
            detail = {}
        if detail.get("source") != "live_capture":
            continue
        activity.append({"rfq_id": row["rfq_id"],
                         "created_time": row["created_time"],
                         "state": "rejected", "reason_code": row["reason_code"],
                         "buy_price": None, "sell_price": None,
                         "buy_qty_decimal": None, "sell_qty_decimal": None})
    activity.sort(key=lambda row: row["created_time"] or "", reverse=True)
    snap["activity"] = {
        "paper_quotes": len(capital.admitted_ids),
        "recorded_fills": conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
        "recent_quotes": activity[:20],
    }
    if _has_table(conn, "priced_quotes"):
        paper_events = _rows(conn, """
            SELECT p.priced_at AS ts, p.rfq_id, p.status, p.reason_code
            FROM priced_quotes p
            WHERE p.trigger='auto' AND EXISTS (
                SELECT 1 FROM quotes q WHERE q.rfq_id=p.rfq_id AND q.origin='shadow')
            ORDER BY p.rowid DESC LIMIT 100
        """)
    else:
        paper_events = []
    snap["paper_events"] = []
    for row in paper_events:
        capital_rejected = row["rfq_id"] in capital.rejected_ids
        snap["paper_events"].append({
            "ts": row["ts"], "rfq_id": row["rfq_id"],
            "action": "paper decline" if capital_rejected or row["status"] != "QUOTED"
                      else "paper quote",
            "reason": "RISK_CAPITAL" if capital_rejected
                      else row["reason_code"] or row["status"],
        })
    snap["paper_events"].extend(
        {"ts": fill["time"], "rfq_id": fill["rfq_id"],
         "action": "capital cap", "reason": "paper fill size reduced to fit equity"}
        for fill in paper_fills if fill.get("capacity_limited"))
    return snap

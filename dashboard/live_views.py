"""Streamlit renderer for the read-only live capture database."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from combo_mm.config import PipelineConfig
from dashboard import live_view_models as vm

TABS = ("RFQs", "Pricing & quoting", "Performance", "Engine status", "NFL correlation")


def render(path: str | Path, nfl_tab) -> None:
    tab = st.session_state.get("live_tab", TABS[0])
    for column, label in zip(st.columns(5), TABS):
        if column.button(label, key=f"live_nav_{label}",
                         type="primary" if tab == label else "secondary",
                         width="stretch"):
            st.session_state["live_tab"] = label
            tab = label
    if tab == "NFL correlation":
        nfl_tab.render()
        return
    try:
        with closing(vm.connect_readonly(path)) as conn:
            if tab == "RFQs":
                _rfqs(conn)
            elif tab == "Pricing & quoting":
                _pricing(conn)
            elif tab == "Performance":
                _performance(conn)
            else:
                _engine(conn)
    except (OSError, sqlite3.Error) as exc:
        st.info(f"Waiting for the headless capture database ({type(exc).__name__}).")


def _rfqs(conn) -> None:
    st.header("Live RFQs")
    only = st.toggle("Show only RFQs we want to quote", key="live_only_quotable")
    total = conn.execute("SELECT COUNT(*) FROM rfq r LEFT JOIN rfq_screen s USING (rfq_id) "
                         "WHERE (? = 0 OR s.screen = 'QUOTABLE')", (int(only),)).fetchone()[0]
    page = st.number_input("Page (500 RFQs per page)", min_value=1, step=1,
                           key="live_rfq_page")
    rows = vm.rfqs(conn, only_quotable=only, offset=(page - 1) * 500)
    st.caption("Newest first. The checks come from the live screening rules and configured "
               "share minimum. Cash-notional sizing is checked during pricing.")
    if not rows:
        st.info("No RFQs match this view yet.")
        return
    st.caption(f"Showing {len(rows):,} of {total:,} matching RFQs")
    display = [{"RFQ": r["rfq_id"], "Want to quote": "✅" if r["quotable"] else "—",
                "Screen": r["screen"] or "PENDING", "Known legs": r["filters"]["known legs"],
                "NFL same game": r["filters"]["NFL same game"],
                "No unsupported same game": r["filters"]["no unsupported same game"],
                "Size minimum": next((value for key, value in r["filters"].items()
                                      if key.startswith("at least ")), None),
                "Legs": r["n_legs"], "Size": r["qty_decimal"] or r["cash_order_qty"],
                "Posted": r["created_time"]} for r in rows]
    selection = st.dataframe(pd.DataFrame(display), hide_index=True, width="stretch",
                             on_select="rerun", selection_mode="single-row",
                             key=f"live_rfq_table_{page}_{only}")
    selected = selection.selection.rows
    if selected:
        picked = rows[selected[0]]["rfq_id"]
        if st.session_state.get("live_handled_selection") != picked:
            st.session_state["live_handled_selection"] = picked
            st.session_state["live_selected_rfq"] = picked
            st.session_state["live_tab"] = "Pricing & quoting"
            st.rerun()


def _pricing(conn) -> None:
    st.header("Pricing & quoting")
    total = conn.execute("SELECT COUNT(*) FROM rfq_screen WHERE screen='QUOTABLE'").fetchone()[0]
    page = st.number_input("Page (500 RFQs per page)", min_value=1, step=1,
                           key="live_pricing_page")
    rows = vm.pricing(conn, offset=(page - 1) * 500)
    if not rows:
        st.info("No filter-passing RFQs yet.")
        return
    st.caption(f"Showing {len(rows):,} of {total:,} filter-passing RFQs")
    display = [{"RFQ": r["rfq_id"], "Decision": r["status"],
                "Reason": r["reason_code"], "Our price": r["response_price"],
                "Size": r["size"], "Market price": r["market_price"],
                "Wait (ms)": r["wait_ms"], "Compute (ms)": r["compute_ms"]}
               for r in rows]
    selection = st.dataframe(pd.DataFrame(display), hide_index=True, width="stretch",
                             on_select="rerun", selection_mode="single-row",
                             key=f"live_pricing_table_{page}")
    if selection.selection.rows:
        st.session_state["live_selected_rfq"] = rows[selection.selection.rows[0]]["rfq_id"]
    ids = [r["rfq_id"] for r in rows]
    chosen = st.session_state.get("live_selected_rfq")
    if chosen not in ids:
        selected = vm.pricing(conn, limit=1, rfq_id=chosen) if chosen else []
        item = selected[0] if selected else rows[0]
        chosen = item["rfq_id"]
    else:
        item = next(r for r in rows if r["rfq_id"] == chosen)
    st.subheader(f"RFQ {chosen}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Decision", item["status"])
    c2.metric("Our quoted price", f"{item['response_price']:.4f}" if item["response_price"] is not None else "—")
    c3.metric(item["market_source"] if item["market_price"] is not None else "Market price",
              f"{item['market_price']:.4f}" if item["market_price"] is not None else "—")
    c4.metric("Edge vs market", f"{item['edge_vs_market']:+.4f}" if item["edge_vs_market"] is not None else "—")
    st.write(f"Reason: **{item['reason_code']}** {item['reason_detail'] or ''}")
    st.write(f"Quote size: **{item['size'] or '—'} {item['size_unit'] or ''}** · "
             f"Model fair **{item['fair'] if item['fair'] is not None else '—'}** · "
             f"Naive product **{item['naive'] if item['naive'] is not None else '—'}**")
    st.write(f"Wait: **{item['wait_ms']:.1f} ms**" if item["wait_ms"] is not None else "Wait: —")
    st.write(f"Compute: **{item['compute_ms']:.1f} ms**" if item["compute_ms"] is not None else "Compute: —")
    detail = item["detail"]
    with st.expander("Pricing adjustments and per-decision detail"):
        st.json({key: detail.get(key) for key in ("components", "explanations", "games", "legs",
                                                   "corr_adjustment_bps", "spread_bps_total")})


def _performance(conn) -> None:
    st.header("Paper performance")
    p = vm.performance(conn)
    st.caption("Shadow fills count only our quoted RFQs whose price would have beaten the market: "
               "the accepted RFQ trade when observed, otherwise the leg-implied (naive) price. "
               "Realized P&L requires settled legs.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Quotes", p["quoted"])
    c2.metric("Shadow fills", p["shadow_fills"])
    c3.metric("Win rate", f"{p['win_rate']:.1%}")
    c4.metric("Net notional", f"${p['net_notional']:,.2f}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Expected P&L", f"${p['expected_pnl']:,.2f}")
    c2.metric("Realized P&L", f"${p['realized_pnl']:,.2f}")
    c3.metric("Max downswing", f"${p['max_downswing']:,.2f}")
    c4.metric("Max upswing", f"${p['max_upswing']:,.2f}")
    if p["curve"]:
        st.line_chart(pd.DataFrame(p["curve"]).set_index("time")[["net_notional"]])
        st.line_chart(pd.DataFrame(p["curve"]).set_index("time")[["realized_pnl"]])
    for label, key in (("Combo family", "by_family"), ("Leg count", "by_legs"), ("Game", "by_game")):
        st.subheader(label)
        st.dataframe(pd.DataFrame(p[key]), hide_index=True, width="stretch")


def _engine(conn) -> None:
    st.header("Engine status")
    status = vm.engine_status(conn, PipelineConfig().quote_latency_budget_ms)
    health = status["health"]
    if health:
        started = datetime.fromisoformat(health["started_at"].replace("Z", "+00:00"))
        uptime = datetime.now(timezone.utc) - started
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Uptime", str(uptime).split(".")[0])
        c2.metric("Messages processed", health["messages_processed"])
        c3.metric("Errors", health["errors"])
        c4.metric("Gateway", "connected" if health["gateway_connected"] else "disconnected")
        st.caption(f"Last heartbeat: {health['heartbeat_at']} · Buffer drops: {health['buffer_drops']}")
    st.subheader("Quote latency · 400 ms budget")
    st.caption("Wait starts at the exchange posting timestamp when the gateway supplies it; "
               "otherwise the gateway receive timestamp is the observable lower bound.")
    wait, compute = status["wait"], status["compute"]
    if status["samples"]:
        st.dataframe(pd.DataFrame([
            {"Stage": "Posted → engine started (wait)", **wait},
            {"Stage": "Engine started → decision recorded (compute)", **compute},
        ]).rename(columns={"p50": "p50 (ms)", "p95": "p95 (ms)", "max": "max (ms)"}),
            hide_index=True, width="stretch")
        if wait["p50"] is not None and wait["p50"] > status["budget_ms"]:
            st.error(f"p50 wait {wait['p50']:.0f} ms exceeds the {status['budget_ms']} ms budget")
    else:
        st.caption("No live timing samples yet.")
    st.subheader("Decline and skip reasons")
    st.dataframe(pd.DataFrame(status["reasons"]), hide_index=True, width="stretch")
    st.subheader("Stored draft quotes")
    st.dataframe(pd.DataFrame(status["drafts"]), hide_index=True, width="stretch")

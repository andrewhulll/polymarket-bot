"""Streamlit dashboard: RFQs, pricing & quoting, and paper performance.

Demo/observability only -- not production. Run with::

    streamlit run dashboard/app.py

from the repo root. Press "Run simulation" to replay the scripted session
through the pipeline (simulated transport, no network) into a SQLite DB, then
browse the three views, which all read precomputed tables from that DB:

1. RFQs -- every RFQ request, expandable to full detail.
2. Pricing & quoting -- V1 fair price, quoted buy/sell, size, expected edge,
   and a human-readable explanation of each pricing adjustment.
3. Performance -- paper/shadow backtest metrics over the fixture dataset.
4. NFL correlation -- offline same-game combo backtest and correlation
   explorer over historical NFL closing lines (``dashboard/nfl_tab.py``);
   independent of the simulation button.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import streamlit as st

# ``streamlit run dashboard/app.py`` puts dashboard/ (not the repo root) on
# sys.path, so make the combo_mm package importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm import PipelineConfig, fixtures, paper_backtest  # noqa: E402
from dashboard import nfl_tab  # noqa: E402

st.set_page_config(page_title="combo_mm dashboard (paper)", layout="wide")

BANNER = (
    "PAPER / SHADOW -- no live quotes submitted. "
    "All quotes are simulated drafts recorded by the shadow quoter."
)
st.warning(BANNER)
st.title("Combo RFQ pipeline -- paper dashboard")

# ---------------------------------------------------------------------------
# Simulation run (cached in session state; DB file persists for the session)
# ---------------------------------------------------------------------------

def _run_simulation() -> dict:
    session, combos = fixtures.build_session()
    config = PipelineConfig(paper_mode=True, db_path=":memory:")
    db_file = tempfile.NamedTemporaryFile(
        prefix="combo_mm_dash_", suffix=".db", delete=False
    ).name
    result, store = paper_backtest.run_backtest(
        session, combos, fixtures.build_drop_copy_feed(), config,
        db_path=db_file,
    )
    store.close()
    return {"db_path": db_file, "result": result}


if st.button("Run simulation", type="primary"):
    with st.spinner("Replaying scripted session through the pipeline..."):
        st.session_state["sim"] = _run_simulation()
    st.success("Simulation complete.")

view = st.tabs(["RFQs", "Pricing & quoting", "Performance", "NFL correlation"])

# The NFL correlation view reads offline backtest files and does not need the
# RFQ simulation.
with view[3]:
    nfl_tab.render()

sim = st.session_state.get("sim")
if sim is None:
    for tab in view[:3]:
        with tab:
            st.info('Press "Run simulation" to replay the scripted session and populate this view.')

# The sim-dependent views are guarded: they render once the button above has
# populated ``sim`` (and stay inert in bare mode, ``python -c "import dashboard.app"``).
if sim is not None:
    db_path: str = sim["db_path"]
    result = sim["result"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ---------------------------------------------------------------------------
    # View 1: RFQs
    # ---------------------------------------------------------------------------
    with view[0]:
        st.header("RFQ requests")
        rfqs = conn.execute("SELECT * FROM rfq ORDER BY rowid").fetchall()
        st.caption(f"{len(rfqs)} RFQs in the simulated session.")
        for r in rfqs:
            legs = conn.execute(
                "SELECT symbol, side, settlement_price FROM rfq_legs "
                "WHERE rfq_id = ? ORDER BY rowid",
                (r["rfq_id"],),
            ).fetchall()
            size_mode = "qtyDecimal" if r["qty_decimal"] is not None else "cashOrderQty"
            size_val = r["qty_decimal"] if r["qty_decimal"] is not None else r["cash_order_qty"]
            with st.expander(
                f"{r['rfq_id']} -- {r['symbol']} -- {r['status']} "
                f"({size_mode}={size_val})"
            ):
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("**Combo**")
                    st.write(f"Symbol: `{r['symbol']}`")
                    st.write(f"Size mode: `{size_mode}` = `{size_val}`")
                    st.write(f"Requester: `{r['creator_user_id']}`")
                    st.write(f"Rest remainder: `{bool(r['rest_remainder'])}`")
                    st.write(f"Status: `{r['status']}`")
                with c2:
                    st.markdown("**Timestamps**")
                    st.write(f"Created (exchange): `{r['created_time']}`")
                    st.write(f"Updated (exchange): `{r['updated_time']}`")
                st.markdown("**Legs**")
                if legs:
                    st.table(
                        [
                            {
                                "market symbol": leg["symbol"],
                                "side": leg["side"],
                                "settlement (raw YES/LONG)": leg["settlement_price"],
                            }
                            for leg in legs
                        ]
                    )
                else:
                    st.write("(no inline legs -- reference fallback used for pricing)")
                st.markdown("**Lifecycle events**")
                events = conn.execute(
                    "SELECT event_type, source, client_derived, recorded_at "
                    "FROM raw_events WHERE rfq_id = ? ORDER BY id",
                    (r["rfq_id"],),
                ).fetchall()
                st.table(
                    [
                        {
                            "event": e["event_type"],
                            "source": e["source"],
                            "client-derived": bool(e["client_derived"]),
                            "recorded at": e["recorded_at"],
                        }
                        for e in events
                    ]
                )

    # ---------------------------------------------------------------------------
    # View 2: Pricing & quoting
    # ---------------------------------------------------------------------------
    with view[1]:
        st.header("Pricing & quoting (V1 shadow pricer)")
        decisions = conn.execute(
            "SELECT d.*, r.symbol AS combo_symbol, r.status AS rfq_status "
            "FROM shadow_decisions d JOIN rfq r ON r.rfq_id = d.rfq_id "
            "ORDER BY d.id"
        ).fetchall()
        st.caption(
            f"{len(decisions)} shadow decisions. Every quote and every decline "
            "carries a reason code and its spread components."
        )

        def _f(x, spec=".4f"):
            return ("{:" + spec + "}").format(x) if x is not None else "-"

        for d in decisions:
            components = json.loads(d["components_json"] or "{}")
            quoted = d["decision"] == "QUOTED_OK"
            icon = "🟢" if quoted else "🔴"
            with st.expander(
                f"{icon} {d['rfq_id']} -- {d['combo_symbol']} -- "
                f"{d['decision']} (fair={_f(d['fair_price'])})"
            ):
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.metric("Fair combo price", _f(d["fair_price"]))
                    st.metric("Buy price (our offer)", _f(d["buy_price"], ".3f"))
                    st.metric("Sell price (our bid)", _f(d["sell_price"], ".3f"))
                with c2:
                    st.metric("Buy qty", d["buy_qty"] or "-")
                    st.metric("Sell qty", d["sell_qty"] or "-")
                    st.metric(
                        "Expected edge",
                        _f(d["expected_edge_bps"], ".1f") + " bps"
                        if d["expected_edge_bps"] is not None else "-",
                    )
                with c3:
                    st.metric("Reason code", d["decision"])
                    st.metric("RFQ status", d["rfq_status"])
                    st.write(f"Model: `{d['reason']}`")
                    st.write(f"Decided at: `{d['ts']}`")
                st.markdown("**Pricing adjustments** (spread components, bps)")
                rows = [
                    ("base edge", components.get("base_edge_bps")),
                    ("model uncertainty", components.get("model_uncertainty_bps")),
                    ("depth impact", components.get("depth_impact_bps")),
                    ("event risk", components.get("event_risk_bps")),
                    ("operational buffer", components.get("operational_buffer_bps")),
                ]
                st.table(
                    [
                        {"adjustment": name, "bps": _f(bps, ".2f")}
                        for name, bps in rows
                    ]
                )
                st.write(
                    f"Total spread: `{_f(components.get('spread_bps_total'), '.2f')}` bps "
                    f"=> half-spread `{_f(components.get('half_spread'))}` "
                    f"around center `{_f(components.get('center'))}` "
                    f"(center = fair; no inventory skew -- risk engine parked)."
                )
                st.markdown("**Per-leg marks**")
                leg_marks = components.get("leg_marks", [])
                if leg_marks:
                    st.table(leg_marks)
                else:
                    st.write("(no leg marks recorded)")

    # ---------------------------------------------------------------------------
    # View 3: Performance (paper/shadow backtest)
    # ---------------------------------------------------------------------------
    with view[2]:
        st.header("Performance -- paper/shadow backtest")
        st.warning(BANNER)
        st.caption(
            "Computed by replaying the fixture dataset through the pipeline + "
            "V1 pricer + shadow quotes with the virtual clock. Pricing used only "
            "book snapshots with timestamps <= each event time (no future info)."
        )
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("RFQs received", result.rfqs_received)
            st.metric("RFQs quoted", result.rfqs_quoted)
        with c2:
            st.metric("RFQs rejected", result.rfqs_rejected)
            st.metric("RFQs expired", result.rfqs_expired)
        with c3:
            st.metric("RFQs executed", result.rfqs_executed)
            st.metric("Quote rate", f"{result.quote_rate:.1%}")
        with c4:
            st.metric("Execution rate", f"{result.execution_rate:.1%}")
            st.metric("Fills", result.n_fills)

        c5, c6, c7, c8 = st.columns(4)
        with c5:
            st.metric("Expected P&L", f"{result.expected_pnl:.2f}")
        with c6:
            st.metric("Realized P&L", f"{result.realized_pnl:.2f}")
        with c7:
            st.metric("Max downswing", f"{result.max_downswing:.2f}")
        with c8:
            st.metric("Max upswing", f"{result.max_upswing:.2f}")

        st.subheader("Cumulative realized P&L")
        if result.equity_curve:
            st.line_chart(
                {"cumulative P&L": [p for _, p in result.equity_curve]},
            )
        else:
            st.write("(no settled fills in this run)")

        st.subheader("Inventory / exposure over time (net notional)")
        if result.exposure_curve:
            st.line_chart(
                {"exposure": [p for _, p in result.exposure_curve]},
            )
        else:
            st.write("(no fills in this run)")

        st.subheader("Per-RFQ detail")
        st.table(result.per_rfq)

    conn.close()

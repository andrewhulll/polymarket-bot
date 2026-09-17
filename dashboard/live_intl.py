"""Issue #11: Streamlit helpers for the live international RFQ source.

Everything the "Go live (international)" button needs lives here so
``dashboard/app.py`` only grows a small delimited block. The live source is
strictly receive-only -- the adapter cannot submit quotes -- and it never
merges into the simulation views (those stay sim-only by design).
"""
from __future__ import annotations

import time

import streamlit as st

from combo_mm.intl_gateway import (
    GatewayCredentials,
    InternationalQuoterGatewayAdapter,
    MissingCredentialsError,
)

STATE_KEY = "live_intl"

MISSING_KEYS_MSG = (
    "Live international feed needs API keys. Set these environment variables "
    "(or put them in a gitignored `.env` file in the repo root) and press the "
    "button again: `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, "
    "`POLY_WALLET_ADDRESS`. Keys are read from your environment only -- they "
    "are never displayed, logged, or committed."
)


def start_live_source() -> None:
    """Start the quoter-gateway adapter; warn gracefully when keys are absent."""
    try:
        creds = GatewayCredentials.from_env()
    except MissingCredentialsError:
        st.warning(MISSING_KEYS_MSG)
        return
    old = st.session_state.get(STATE_KEY)
    if old is not None:
        old["adapter"].stop()
    adapter = InternationalQuoterGatewayAdapter(creds)
    try:
        adapter.start()
    except RuntimeError as exc:  # missing optional 'websockets' dependency
        st.error(str(exc))
        return
    st.session_state[STATE_KEY] = {"adapter": adapter, "started_at": time.time()}
    st.success("Live international feed starting -- waiting for the first RFQ.")


def stop_live_source() -> None:
    """Stop the adapter and clear live state."""
    live = st.session_state.pop(STATE_KEY, None)
    if live is not None:
        live["adapter"].stop()


def render_live_status() -> None:
    """Render the live-source status block (no-op when the feed is off)."""
    live = st.session_state.get(STATE_KEY)
    if live is None:
        return
    adapter: InternationalQuoterGatewayAdapter = live["adapter"]
    stats = adapter.stats()
    st.divider()
    st.subheader("Live international RFQ feed (receive-only)")
    st.caption(
        "Streaming `RFQ_REQUEST` / `RFQ_TRADE` from the polymarket.com quoter "
        "gateway. This adapter cannot submit quotes -- it only reads."
    )
    if adapter.connected:
        st.success("Connected to the quoter gateway.")
    else:
        last_err = stats.get("last_error")
        st.warning(
            "Not currently connected (reconnecting with backoff"
            + (f"; last error: `{last_err}`" if last_err else "")
            + ")."
        )
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("RFQs seen", stats["rfqs_seen"])
    with c2:
        st.metric("Trades seen", stats["trades_seen"])
    with c3:
        st.metric("Reconnects", stats["reconnects"])
    with c4:
        last = stats.get("last_frame_at")
        age = f"{time.time() - last:.0f}s ago" if last else "-"
        st.metric("Last frame", age)
    recent = adapter.recent(25)
    if recent:
        st.markdown("**Recent RFQs / trades**")
        st.table(
            [
                {
                    "received": r["received_at"],
                    "kind": r["kind"],
                    "rfq_id": (r["rfq_id"] or "")[:24],
                    "direction": r["direction"],
                    "legs": r["legs"],
                    "size": r["size"],
                }
                for r in recent
            ]
        )
    else:
        st.info("No RFQs received yet -- they appear here as they stream in.")
    b1, b2 = st.columns(2)
    with b1:
        if st.button("Refresh status"):
            pass  # rerun refreshes the metrics above
    with b2:
        if st.button("Stop live feed"):
            stop_live_source()
            st.rerun()

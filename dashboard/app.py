"""Streamlit dashboard: RFQs, pricing & quoting, and paper performance.

Demo/observability only -- not production. Run with::

    streamlit run dashboard/app.py

from the repo root. Two controls at the top pick the data behind the views:

- **Run backtest -- NFL 2026 Week 1**: replays every same-game combo from the
  week's games as RFQs through the pipeline, priced by the NFL joint model
  against a naive competitor and settled on the final scores
  (:mod:`combo_mm.nfl.week_backtest`). Needs a cached nflverse pull under
  ``data/raw`` (``python scripts/refresh_params.py --pull``).
- **Live monitor RFQ feed**: streams live RFQs through the same store and
  shadow engine (:class:`combo_mm.live_monitor.LiveMonitor`), refreshing the
  views every few seconds. Source: the polymarket.com quoter gateway
  (:class:`combo_mm.intl_gateway.InternationalQuoterGatewayAdapter`,
  receive-only) when ``POLYMARKET_API_KEY`` / ``POLYMARKET_SECRET`` /
  ``POLYMARKET_PASSPHRASE`` / ``POLYMARKET_ADDRESS`` are set (env or a
  gitignored ``.env``); otherwise the Polymarket US Retail API
  (:class:`combo_mm.retail.RetailPollingSource`) when ``POLYMARKET_US_KEY_ID``
  / ``POLYMARKET_US_SECRET_KEY`` are set. No simulated fallback: without
  access it says what is missing.

Views 1-4 read the active run's SQLite DB:

1. RFQs -- the week's historical RFQs or the live feed, filterable, with full
   detail (legs, settlement, lifecycle events) per RFQ. Live: legs resolved
   from the combo catalog, quotable NFL same-game RFQs first, a button to
   pick RFQs to quote, and the durable picked list with accepted quotes.
2. Pricing & quoting -- fair price (model vs naive), quoted buy/sell, size,
   expected edge, and each pricing adjustment.
3. Performance -- quote/fill counts, expected vs realized P&L, swings,
   exposure, and (backtest) results by combo family, size and game.
4. Engine status -- shadow quoting engine health and stored draft quotes.
5. NFL correlation -- offline correlation research (``dashboard/nfl_tab.py``);
   independent of the controls.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import streamlit as st

# ``streamlit run dashboard/app.py`` puts dashboard/ (not the repo root) on
# sys.path, so make the combo_mm package importable without installing it.
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from combo_mm import PipelineConfig  # noqa: E402
from combo_mm.auth import CredentialsNotConfigured  # noqa: E402
from combo_mm.combo_markets import ComboMarketCatalog, LegMarket  # noqa: E402
from combo_mm.intl_gateway import (  # noqa: E402
    GATEWAY_ENV_VARS,
    GatewayCredentials,
    InternationalQuoterGatewayAdapter,
    MissingCredentialsError,
)
from combo_mm.live_monitor import LiveMonitor  # noqa: E402
from combo_mm.nfl import week_backtest  # noqa: E402
from combo_mm.paper_backtest import BacktestResult  # noqa: E402
from combo_mm.quote_selections import QuoteSelectionStore  # noqa: E402
from combo_mm.retail import KEY_ID_ENV, SECRET_ENV, RetailPollingSource  # noqa: E402
from combo_mm.rfq_screen import QUOTABLE, UNRESOLVED  # noqa: E402
from combo_mm.store import EventStore  # noqa: E402
from dashboard import nfl_tab  # noqa: E402

# The live quoter gateway broadcasts ~200 RFQs/s, so live views show the newest
# rows only and take counts from SQL aggregates (the backtest shows everything).
LIVE_ROW_LIMIT = 500

# Durable live-mode state (data/ is gitignored): the combo leg catalog cache and
# the RFQs picked for quoting with their accepted quotes.
LIVE_DATA = REPO / "data" / "live"
MARKETS_QUOTABLE_FIRST = "Quotable NFL first"
MARKETS_QUOTABLE_ONLY = "Quotable NFL only"
MARKETS_ALL = "All, newest first"
MARKET_FILTERS = (MARKETS_QUOTABLE_FIRST, MARKETS_QUOTABLE_ONLY, MARKETS_ALL)

RAW_ROOT = REPO / "data" / "raw"
ESTIMATOR_PATH = REPO / "params" / "estimator.json"
SEASON, WEEK = week_backtest.BACKTEST_SEASON, week_backtest.BACKTEST_WEEK
BACKTEST_LABEL = f"NFL {SEASON} Week {WEEK}"

st.set_page_config(page_title="combo_mm dashboard (paper)", layout="wide")

BANNER = (
    "PAPER / SHADOW -- no live quotes submitted. "
    "All quotes are simulated drafts recorded by the shadow quoter."
)
st.warning(BANNER)
st.title("Combo RFQ pipeline -- paper dashboard")


def _new_db(prefix: str) -> str:
    return tempfile.NamedTemporaryFile(prefix=prefix, suffix=".db", delete=False).name


def _f(x: Any, spec: str = ".4f") -> str:
    return ("{:" + spec + "}").format(x) if x is not None else "-"


# ---------------------------------------------------------------------------
# Controls: backtest one-click vs live monitor (state lives in session_state)
# ---------------------------------------------------------------------------

def _run_backtest() -> Dict[str, Any]:
    out = week_backtest.run_from_pull(raw_root=RAW_ROOT, estimator_path=ESTIMATOR_PATH,
                                      db_path=_new_db("combo_mm_backtest_"),
                                      season=SEASON, week=WEEK)
    return {"mode": "backtest", "db_path": out.db_path, "result": out.result,
            "trades": out.trades, "meta": out.meta}


MISSING_LIVE_KEYS_MSG = (
    "Live monitoring needs API keys in the shell that starts Streamlit (or a gitignored `.env` "
    "in the repo root for the gateway keys): "
    + ", ".join(f"`{v}`" for v in GATEWAY_ENV_VARS)
    + f" for the polymarket.com quoter gateway, or `{KEY_ID_ENV}` + `{SECRET_ENV}` "
    "(plus `pip install polymarket-us`) for the US Retail API. Key values are never displayed. "
    "No simulated RFQs are shown in live mode."
)


def _live_source(config: PipelineConfig):
    """(source, label): the quoter gateway if its keys exist, else Retail if its keys exist."""
    try:
        creds = GatewayCredentials.from_env()
    except MissingCredentialsError:
        creds = None
    if creds is not None:
        return InternationalQuoterGatewayAdapter(creds).start(), "quoter gateway"
    if os.environ.get(KEY_ID_ENV) and os.environ.get(SECRET_ENV):
        return RetailPollingSource(poll_interval_s=config.poll_interval_s,
                                   max_requests_per_poll=config.max_requests_per_poll), "retail"
    return None, None


@st.cache_resource
def _combo_catalog() -> ComboMarketCatalog:
    """One process-wide leg catalog: loads the disk cache, then crawls in the background."""
    return ComboMarketCatalog(LIVE_DATA / "combo_markets.json").start()


@st.cache_resource
def _quote_selections() -> QuoteSelectionStore:
    return QuoteSelectionStore(LIVE_DATA / "quote_selections.db")


def _start_live() -> Dict[str, Any]:
    config = PipelineConfig(paper_mode=True)
    run: Dict[str, Any] = {"mode": "live", "db_path": None, "monitor": None,
                           "polling": False, "error": None, "source": None,
                           "poll_interval_s": config.poll_interval_s}
    try:
        source, label = _live_source(config)
    except (CredentialsNotConfigured, RuntimeError) as exc:
        run["error"] = str(exc)  # names env vars / missing packages only, never values
        return run
    except Exception as exc:
        run["error"] = f"Live feed failed to start ({type(exc).__name__})."
        return run
    if source is None:
        run["error"] = "No live feed credentials found."
        return run
    db_path = _new_db("combo_mm_live_")
    run.update(db_path=db_path, polling=True, source=label,
               monitor=LiveMonitor(source, EventStore(db_path, synchronous="NORMAL"), config,
                                   source_label=label, catalog=_combo_catalog(),
                                   selections=_quote_selections()))
    return run


def _stop_live(run: Optional[Dict[str, Any]]) -> None:
    """Stop polling and any background connection (the gateway owns a websocket thread)."""
    if not run or run.get("mode") != "live":
        return
    run["polling"] = False
    monitor = run.get("monitor")
    stop = getattr(monitor.source, "stop", None) if monitor is not None else None
    if stop is not None:
        stop()


run: Optional[Dict[str, Any]] = st.session_state.get("run")
live_polling = bool(run and run["mode"] == "live" and run.get("polling"))

c_bt, c_live, c_state = st.columns([1.2, 1.2, 2.6])
with c_bt:
    if st.button(f"Run backtest -- {BACKTEST_LABEL}", type="primary", width="stretch"):
        _stop_live(run)
        with st.spinner(f"Replaying {BACKTEST_LABEL} same-game combos through the pipeline..."):
            try:
                run = _run_backtest()
            except Exception as exc:  # missing pull, no games for the week, ...
                run = {"mode": "backtest", "db_path": None, "error": f"{type(exc).__name__}: {exc}"}
        st.session_state["run"] = run
        live_polling = False
with c_live:
    if live_polling:
        if st.button("Stop live monitor", width="stretch"):
            _stop_live(run)
            st.rerun()  # redraw the controls and drop the refresh timers
    elif st.button("Live monitor RFQ feed", width="stretch"):
        _stop_live(run)
        with st.spinner("Connecting to the live RFQ feed..."):
            st.session_state["run"] = _start_live()
        st.rerun()  # redraw the controls with the refresh timers on
with c_state:
    if run is None:
        st.caption(f"Choose a data source: the {BACKTEST_LABEL} backtest (historical) "
                   "or the live RFQ feed.")
    elif run["mode"] == "backtest":
        st.caption(f"Showing: **backtest -- {BACKTEST_LABEL}** (historical RFQ replay).")
    else:
        st.caption(f"Showing: **live RFQ feed ({run.get('source') or 'not connected'})** -- "
                   + ("polling." if live_polling else "stopped (last data kept)."))

if run is not None and run.get("error"):
    st.error(run["error"])
    if run["mode"] == "live":
        st.info(MISSING_LIVE_KEYS_MSG)

every = run["poll_interval_s"] if live_polling else None


@st.fragment(run_every=every)
def _live_status() -> None:
    monitor: LiveMonitor = run["monitor"]
    if run.get("polling"):
        monitor.poll_once()
    source = monitor.source
    c1, c2, c3, c4, c5 = st.columns(5)
    if isinstance(source, InternationalQuoterGatewayAdapter):
        stats = source.stats()
        c1.metric("Gateway", "connected" if source.connected else "reconnecting")
        c2.metric("RFQs seen", f"{stats['rfqs_seen']:,}")
        c3.metric("Trades seen", f"{stats['trades_seen']:,}")
        c4.metric("Key auth", stats["auth"])
        c5.metric("Reconnects", stats["reconnects"])
        if stats["auth"] == "rejected":
            st.warning(f"The gateway rejected these API keys (`{stats['auth_error']}`). The RFQ "
                       "broadcast is public, so the feed keeps streaming; key auth only matters for "
                       "quoting, which this receive-only monitor never does.")
        if stats.get("buffer_drops"):
            st.caption(f"{stats['buffer_drops']:,} RFQs dropped by the adapter buffer (feed outpaced polling).")
        if stats.get("last_error"):
            st.caption(f"Last gateway error: `{stats['last_error']}`")
        pricing_note = ("gateway legs are on-chain position ids with no leg books, so the shadow "
                        "engine records them as MISSING_LEG declines until leg pricing is mapped")
    else:
        c1.metric("Polls", monitor.polls)
        c2.metric("RFQ events applied", monitor.events_applied)
        c3.metric("Leg book updates", monitor.books_seen)
        beta = monitor.rfq_beta_enabled
        c4.metric("RFQ beta access", "-" if beta is None else ("enabled" if beta else "NOT enabled"))
        c5.metric("Poll errors", monitor.poll_errors)
        if beta is False:
            st.warning("This API key is not enabled for the Retail RFQ beta (403). No RFQs will "
                       "appear until access is granted; leg books still refresh.")
        if monitor.last_error:
            st.caption(f"Last source error: `{monitor.last_error}`")
        pricing_note = "V1 independent-leg pricer (live symbols are not mapped to the NFL model yet)"
    st.caption(f"Source: {run['source']} (receive-only) · last poll: {monitor.last_poll_at} · "
               f"every {run['poll_interval_s']:.0f}s · {pricing_note}.")

    budget_ms = monitor.config.quote_latency_budget_ms
    lat = monitor.store.get_latency_stats()
    if lat["count"]:
        lc1, lc2, lc3, lc4 = st.columns(4)
        lc1.metric("Quote latency (last)", f"{lat['last_ms']:.0f} ms")
        lc2.metric("Quote latency (p95)", f"{lat['p95_ms']:.0f} ms")
        lc3.metric("Quote latency (max)", f"{lat['max_ms']:.0f} ms")
        lc4.metric(f"Over {budget_ms}ms budget", f"{lat['breaches']}/{lat['count']}",
                  delta=f"{lat['breach_rate']:.0%}", delta_color="inverse")
        if lat["breach_rate"]:
            st.warning(f"{lat['breaches']} of the last {lat['count']} RFQs took longer than "
                       f"{budget_ms}ms from posting to our decision -- those are too slow to "
                       "win the contract.")
    catalog = monitor.catalog
    if catalog is not None:
        state = ("crawling" if catalog.refreshing else
                 f"refreshed {catalog.last_refresh_at}" if catalog.last_refresh_at else "idle")
        err = f" · last error `{catalog.last_error}`" if catalog.last_error else ""
        st.caption(f"Leg catalog (combos-rfq-api combo-markets): {len(catalog):,} positions · "
                   f"{state}{err} · accepted quotes stored this run: {monitor.accepted_recorded}.")


if run is not None and run["mode"] == "live" and run.get("monitor") is not None:
    _live_status()

view = st.tabs(["RFQs", "Pricing & quoting", "Performance", "Engine status", "NFL correlation"])

# The NFL correlation view reads offline backtest files and does not need a run.
with view[4]:
    nfl_tab.render()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _trades_by_rfq(r: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {t["rfq_id"]: t for t in r.get("trades") or []}


# ---------------------------------------------------------------------------
# View 1: RFQs
# ---------------------------------------------------------------------------

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _live_rfqs(conn: sqlite3.Connection, market_filter: str) -> list:
    """Newest live RFQs for the chosen market filter, screened ones via the (rank, seq) index."""
    screened = _table_exists(conn, "rfq_screen")
    cols = ("r.*, s.direction, s.side AS combo_side, s.submission_deadline, s.screen, "
            "s.n_nfl_legs, s.n_resolved")
    if screened and market_filter != MARKETS_ALL:
        ranks = (0,) if market_filter == MARKETS_QUOTABLE_ONLY else (0, 1, 2)
        out: list = []
        for rank in ranks:
            out += [dict(x) for x in conn.execute(
                f"SELECT {cols} FROM rfq_screen s JOIN rfq r ON r.rfq_id = s.rfq_id "
                "WHERE s.rank = ? ORDER BY s.seq DESC LIMIT ?", (rank, LIVE_ROW_LIMIT - len(out)))]
            if len(out) >= LIVE_ROW_LIMIT:
                break
        if out or market_filter == MARKETS_QUOTABLE_ONLY:
            return out
    join = "LEFT JOIN rfq_screen s ON s.rfq_id = r.rfq_id" if screened else ""
    select = cols if screened else "r.*"
    return [dict(x) for x in conn.execute(
        f"SELECT {select} FROM rfq r {join} ORDER BY r.rowid DESC LIMIT ?", (LIVE_ROW_LIMIT,))]


def _leg_label(pid: str, market: Optional[LegMarket]) -> str:
    if market is None:
        return f"? {pid[:12]}…"
    price = f" @{market.price:.3f}" if market.price is not None else ""
    return f"{market.title} → {market.outcome}{price}"


def _ms_to_iso(value: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _selected_view(selections: QuoteSelectionStore) -> None:
    st.subheader("Selected to quote")
    chosen = selections.list_selected()
    st.caption("RFQs picked on this page (kept across restarts in `data/live/quote_selections.db`). "
               "The accepted quote is the gateway's confirmed trade broadcast for that RFQ: accepted "
               "blended price and matched size. Trades for RFQs not picked are never stored.")
    if not chosen:
        st.write("(none selected yet)")
        return
    st.dataframe(pd.DataFrame([{
        "rfq_id": s["rfq_id"], "selected at": s["selected_at"], "screen": s["screen"],
        "legs": " | ".join(leg.get("label", "") for leg in s["legs"]),
        "direction": s["direction"], "side": s["side"],
        "size": s["size"], "unit": s["size_unit"],
        "accepted price": s["accepted_price"], "accepted size": s["accepted_size"],
        "executed at": s["accepted_executed_at"],
    } for s in chosen]), width="stretch", hide_index=True,
        column_config={"accepted price": st.column_config.NumberColumn(format="%.4f")})


def _live_rfq_detail(x: Dict[str, Any], legs: list, markets: list,
                     monitor: Optional[LiveMonitor], conn: sqlite3.Connection) -> None:
    rfq_id = x["rfq_id"]
    size_val = x["qty_decimal"] if x["qty_decimal"] is not None else x["cash_order_qty"]
    unit = "shares" if x["qty_decimal"] is not None else "notional"
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Request**")
        st.write(f"Screen: `{x.get('screen') or '-'}`")
        st.write(f"Requester `{x.get('direction') or '?'}` · combo side `{x.get('combo_side') or '?'}` "
                 f"· size `{size_val}` {unit}")
        st.write(f"Combo condition: `{x['symbol']}`")
        st.write(f"Status: `{x['status']}`")
    with c2:
        st.markdown("**Timestamps**")
        st.write(f"Received: `{x['created_time']}`")
        st.write(f"Quote submission deadline: `{_ms_to_iso(x.get('submission_deadline')) or '-'}`")
        trade = monitor.recent_trades.get(rfq_id) if monitor is not None else None
        if trade:
            st.write(f"Traded: `{trade.get('size')}` shares @ `{trade.get('price')}` "
                     f"(executed `{trade.get('executed_at')}`)")

    leg_rows = []
    for leg, m in zip(legs, markets):
        leg_rows.append({
            "market": m.title if m else "(not in combo catalog yet)",
            "outcome": m.outcome if m else leg["side"],
            "price": m.price if m else None,
            "league": m.league if m else None,
            "game": m.game if m else None,
            "NFL": bool(m and m.is_nfl),
            "position id": leg["symbol"],
        })
    st.markdown("**Legs**")
    st.dataframe(pd.DataFrame(leg_rows), width="stretch", hide_index=True,
                 column_config={"price": st.column_config.NumberColumn(format="%.3f")})

    selections = monitor.selections if monitor is not None else None
    if selections is not None:
        if selections.is_selected(rfq_id):
            st.success("On the quoting list. Its accepted quote is stored when the RFQ trades.")
            if st.button("Remove from quoting list", key=f"unselect_{rfq_id}"):
                selections.unselect(rfq_id)
                st.rerun()
        elif st.button("Quote this RFQ", type="primary", key=f"select_{rfq_id}"):
            monitor.select(rfq_id, {
                "direction": x.get("direction"), "side": x.get("combo_side"), "size": size_val,
                "size_unit": unit, "submission_deadline": _ms_to_iso(x.get("submission_deadline")),
                "condition_id": x["symbol"], "created_time": x["created_time"],
                "screen": x.get("screen"),
                "legs": [{**row, "label": _leg_label(row["position id"], m)}
                         for row, m in zip(leg_rows, markets)],
            })
            st.rerun()

    st.markdown("**Lifecycle events**")
    events = conn.execute(
        "SELECT event_type, source, client_derived, recorded_at FROM raw_events "
        "WHERE rfq_id = ? ORDER BY id", (rfq_id,)).fetchall()
    st.table([{"event": e["event_type"], "source": e["source"],
               "client-derived": bool(e["client_derived"]), "recorded at": e["recorded_at"]}
              for e in events])


@st.fragment(run_every=every)
def _rfq_view(r: Dict[str, Any]) -> None:
    conn = _connect(r["db_path"])
    try:
        backtest = r["mode"] == "backtest"
        monitor: Optional[LiveMonitor] = r.get("monitor")
        catalog = monitor.catalog if monitor is not None else None
        market_filter = MARKETS_QUOTABLE_FIRST
        if backtest:
            rfqs = [dict(x) for x in conn.execute("SELECT * FROM rfq ORDER BY created_time, rowid")]
            leg_rows = conn.execute("SELECT rfq_id, symbol, side, settlement_price FROM rfq_legs "
                                    "ORDER BY rowid")
        else:
            market_filter = st.radio(
                "Markets", MARKET_FILTERS, horizontal=True, key="rfq_market_filter",
                help="Quotable = at least two legs from the same NFL game and no other game "
                     "(any sport) contributing two or more legs. Other legs are independent.")
            rfqs = _live_rfqs(conn, market_filter)
            ids = [x["rfq_id"] for x in rfqs]
            leg_rows = conn.execute(
                f"SELECT rfq_id, symbol, side, settlement_price FROM rfq_legs WHERE rfq_id IN "
                f"({','.join('?' * len(ids))}) ORDER BY rowid", ids) if ids else []
        legs_by_rfq: Dict[str, list] = {}
        for leg in leg_rows:
            legs_by_rfq.setdefault(leg["rfq_id"], []).append(dict(leg))
        status_counts = {x["status"]: x["n"] for x in conn.execute(
            "SELECT status, COUNT(*) AS n FROM rfq GROUP BY status")}
        trades = _trades_by_rfq(r)

        if backtest:
            st.header(f"Historical RFQs -- {BACKTEST_LABEL}")
            meta = r["meta"]
            st.caption(
                f"{meta['n_rfqs']} RFQs: every same-game combo (2-3 legs of ML / spread / total) for "
                f"{meta['n_games']} games, reconstructed from nflverse closing lines "
                f"(pull {meta.get('data_vintage', {}).get('pull_date', '?')}) and arriving in the "
                "3 hours before kickoff. Each is priced by the joint model, competes with a naive "
                "independent-leg maker, and settles on the final score.")
        else:
            st.header("Live RFQ feed")
            st.caption("RFQs observed on the live feed since the monitor started "
                       "(auto-refreshing while polling). Legs are resolved from the public combo "
                       "catalog; pick an RFQ below to add it to the quoting list.")

        selections = monitor.selections if monitor is not None else None
        if not rfqs:
            st.info("The backtest produced no RFQs." if backtest else
                    "No quotable NFL RFQs yet." if market_filter == MARKETS_QUOTABLE_ONLY else
                    "No RFQs yet.")
            if selections is not None:
                _selected_view(selections)
            return

        total = sum(status_counts.values())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("RFQs", f"{total:,}")
        if backtest:
            c2.metric("Executed (we traded)", f"{status_counts.get('EXECUTED', 0):,}")
            c3.metric("Closed (no trade)", f"{status_counts.get('CLOSED', 0):,}")
            c4.metric("Open / quoted", f"{sum(n for s, n in status_counts.items() if s not in ('EXECUTED', 'CLOSED', 'CANCELLED', 'EXPIRED')):,}")
        else:
            screens = ({x["screen"]: x["n"] for x in conn.execute(
                "SELECT screen, COUNT(*) AS n FROM rfq_screen GROUP BY screen")}
                if _table_exists(conn, "rfq_screen") else {})
            c2.metric("Quotable (NFL same game)", f"{screens.get(QUOTABLE, 0):,}")
            c3.metric("Traded (closed)", f"{status_counts.get('CLOSED', 0):,}")
            c4.metric("Legs not yet resolved", f"{screens.get(UNRESOLVED, 0):,} RFQs")
            if total > len(rfqs):
                st.caption(f"Table shows {len(rfqs):,} of {total:,} RFQs ({market_filter.lower()}).")

        chosen_ids = selections.selected_ids() if selections is not None else set()
        markets_by_rfq: Dict[str, list] = {}
        rows = []
        for x in rfqs:
            t = trades.get(x["rfq_id"], {})
            size = x["qty_decimal"] if x["qty_decimal"] is not None else x["cash_order_qty"]
            leg_ids = [leg["symbol"] for leg in legs_by_rfq.get(x["rfq_id"], [])]
            row = {
                "rfq_id": x["rfq_id"],
                "combo": t.get("combo_label") or x["symbol"],
                "status": x["status"],
                "size": size,
                "created": x["created_time"],
                "requester": x["creator_user_id"],
                "legs": len(leg_ids),
            }
            if not backtest:
                markets = catalog.resolve(leg_ids) if catalog is not None else [None] * len(leg_ids)
                markets_by_rfq[x["rfq_id"]] = markets
                nfl_games = sorted({m.game for m in markets if m is not None and m.is_nfl and m.game})
                row = {
                    "quoting": x["rfq_id"] in chosen_ids,
                    "screen": x.get("screen") or "-",
                    "legs": " | ".join(_leg_label(pid, m) for pid, m in zip(leg_ids, markets)),
                    "n legs": len(leg_ids),
                    "NFL games": ", ".join(nfl_games),
                    "direction": x.get("direction"),
                    "side": x.get("combo_side"),
                    "size": size,
                    "unit": "shares" if x["qty_decimal"] is not None else "notional",
                    "status": x["status"],
                    "received": x["created_time"],
                    "quote by": _ms_to_iso(x.get("submission_deadline")),
                    "rfq_id": x["rfq_id"],
                }
            if backtest:
                row.update({
                    "game": t.get("game"),
                    "family": t.get("family"),
                    "requester side": t.get("requester_side"),
                    "naive fair": t.get("naive_fair"),
                    "model fair": t.get("model_fair"),
                    "our price": t.get("fill_price"),
                    "result": t.get("outcome"),
                    "P&L": t.get("pnl"),
                })
            rows.append(row)
        df = pd.DataFrame(rows)

        f1, f2 = st.columns(2)
        with f1:
            if backtest:
                games = sorted(df["game"].dropna().unique())
                pick = st.multiselect("Game", games, key="rfq_filter_game")
                if pick:
                    df = df[df["game"].isin(pick)]
        with f2:
            status_pick = st.multiselect("Status", sorted(status_counts), key="rfq_filter_status")
            if status_pick:
                df = df[df["status"].isin(status_pick)]

        st.dataframe(df, width="stretch", hide_index=True,
                     column_config={
                         "naive fair": st.column_config.NumberColumn(format="%.3f"),
                         "model fair": st.column_config.NumberColumn(format="%.3f"),
                         "our price": st.column_config.NumberColumn(format="%.3f"),
                         "P&L": st.column_config.NumberColumn(format="$%.2f"),
                     })

        st.subheader("RFQ detail")
        labels = {x["rfq_id"]: x.get("screen") for x in rfqs}
        choice = st.selectbox(
            "RFQ", list(df["rfq_id"]), key="rfq_detail_pick",
            format_func=(lambda i: i) if backtest else
            (lambda i: f"{'★ ' if i in chosen_ids else ''}{i} · {labels.get(i) or '-'} · "
                       + " | ".join(_leg_label("", m) for m in markets_by_rfq.get(i, []))[:140]))
        if choice is None:
            if selections is not None:
                _selected_view(selections)
            return
        x = next(r_ for r_ in rfqs if r_["rfq_id"] == choice)
        if not backtest:
            _live_rfq_detail(x, legs_by_rfq.get(choice, []), markets_by_rfq.get(choice, []),
                             monitor, conn)
            if selections is not None:
                _selected_view(selections)
            return
        size_mode = "qtyDecimal" if x["qty_decimal"] is not None else "cashOrderQty"
        size_val = x["qty_decimal"] if x["qty_decimal"] is not None else x["cash_order_qty"]
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Combo**")
            st.write(f"Symbol: `{x['symbol']}`")
            st.write(f"Size mode: `{size_mode}` = `{size_val}`")
            st.write(f"Requester: `{x['creator_user_id']}`")
            st.write(f"Status: `{x['status']}`")
            t = trades.get(choice)
            if t:
                st.write(f"Requester side: `{t['requester_side']}` · combo result: `{t['outcome']}`")
        with c2:
            st.markdown("**Timestamps**")
            st.write(f"Created (exchange): `{x['created_time']}`")
            st.write(f"Updated (exchange): `{x['updated_time']}`")
            if t:
                st.write(f"Our quote: bid `{_f(t['our_sell'], '.3f')}` / offer `{_f(t['our_buy'], '.3f')}` · "
                         f"naive maker: bid `{_f(t['comp_sell'], '.3f')}` / offer `{_f(t['comp_buy'], '.3f')}`")
                if t["won"]:
                    st.write(f"Traded: we `{t['fill_side']}` {t['fill_qty']:g} @ `{t['fill_price']:.3f}` "
                             f"→ P&L `{_f(t['pnl'], '.2f')}`")
                else:
                    st.write("Not traded: the naive maker showed the better price.")
        st.markdown("**Legs**")
        legs = legs_by_rfq.get(choice, [])
        if legs:
            st.table([{"market symbol": leg["symbol"], "side": leg["side"],
                       "settlement (raw YES/LONG)": leg["settlement_price"]} for leg in legs])
        else:
            st.write("(no inline legs)")
        st.markdown("**Lifecycle events**")
        events = conn.execute(
            "SELECT event_type, source, client_derived, recorded_at FROM raw_events "
            "WHERE rfq_id = ? ORDER BY id", (choice,)).fetchall()
        st.table([{"event": e["event_type"], "source": e["source"],
                   "client-derived": bool(e["client_derived"]), "recorded at": e["recorded_at"]}
                  for e in events])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# View 2: Pricing & quoting
# ---------------------------------------------------------------------------

@st.fragment(run_every=every)
def _pricing_view(r: Dict[str, Any]) -> None:
    conn = _connect(r["db_path"])
    try:
        st.header("Pricing & quoting (shadow pricer)")
        backtest = r["mode"] == "backtest"
        query = ("SELECT d.*, r.symbol AS combo_symbol, r.status AS rfq_status "
                 "FROM shadow_decisions d JOIN rfq r ON r.rfq_id = d.rfq_id ")
        decisions = [dict(d) for d in (
            conn.execute(query + "ORDER BY d.id") if backtest
            else conn.execute(query + "ORDER BY d.id DESC LIMIT ?", (LIVE_ROW_LIMIT,)))]
        total = conn.execute("SELECT COUNT(*) AS n FROM shadow_decisions").fetchone()["n"]
        model = ("NFL joint model (bivariate-normal scores, walk-forward params)"
                 if backtest else "V1 independent-leg product")
        shown = "" if total == len(decisions) else f" (newest {len(decisions):,} shown)"
        st.caption(f"{total:,} shadow decisions{shown} · pricer: {model}. Every quote and decline "
                   "carries a reason code and its spread components.")
        if not decisions:
            st.info("No pricing decisions yet.")
            return
        rows = []
        for d in decisions:
            comp = json.loads(d["components_json"] or "{}")
            rows.append({
                "rfq_id": d["rfq_id"], "decision": d["decision"],
                "naive fair": comp.get("naive_fair"), "fair": d["fair_price"],
                "corr adj (bps)": comp.get("correlation_adjustment_bps"),
                "bid (sell)": d["sell_price"], "offer (buy)": d["buy_price"],
                "size": d["buy_qty"], "edge (bps)": d["expected_edge_bps"],
                "spread (bps)": d["spread_bps"], "decided at": d["ts"],
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                     column_config={c: st.column_config.NumberColumn(format="%.3f")
                                    for c in ("naive fair", "fair", "bid (sell)", "offer (buy)")}
                     | {c: st.column_config.NumberColumn(format="%.0f")
                        for c in ("corr adj (bps)", "edge (bps)", "spread (bps)")})

        st.subheader("Decision detail")
        idx = st.selectbox("Decision", range(len(decisions)), key="pricing_detail_pick",
                           format_func=lambda i: f"{decisions[i]['rfq_id']} -- {decisions[i]['decision']}")
        if idx is None:
            return
        d = decisions[idx]
        components = json.loads(d["components_json"] or "{}")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Fair combo price", _f(d["fair_price"]))
            st.metric("Buy price (our offer)", _f(d["buy_price"], ".3f"))
            st.metric("Sell price (our bid)", _f(d["sell_price"], ".3f"))
        with c2:
            st.metric("Naive product", _f(components.get("naive_fair")))
            st.metric("Correlation adjustment",
                      _f(components.get("correlation_adjustment_bps"), ".0f") + " bps"
                      if components.get("correlation_adjustment_bps") is not None else "-")
            st.metric("Expected edge", _f(d["expected_edge_bps"], ".1f") + " bps"
                      if d["expected_edge_bps"] is not None else "-")
        with c3:
            st.metric("Reason code", d["decision"])
            st.metric("RFQ status", d["rfq_status"])
            st.write(f"Model: `{d['reason']}`")
            st.write(f"Size: buy `{d['buy_qty'] or '-'}` / sell `{d['sell_qty'] or '-'}`")
        st.markdown("**Pricing adjustments** (spread components, bps)")
        st.table([{"adjustment": name, "bps": _f(components.get(key), ".2f")} for name, key in (
            ("base edge", "base_edge_bps"), ("model uncertainty", "model_uncertainty_bps"),
            ("depth impact", "depth_impact_bps"), ("event risk", "event_risk_bps"),
            ("operational buffer", "operational_buffer_bps"))])
        st.write(f"Total spread: `{_f(components.get('spread_bps_total'), '.2f')}` bps "
                 f"=> half-spread `{_f(components.get('half_spread'))}` "
                 f"around center `{_f(components.get('center'))}` (center = model fair; "
                 "no inventory skew -- risk engine parked).")
        st.markdown("**Per-leg marks**")
        leg_marks = components.get("leg_marks", [])
        if leg_marks:
            st.table(leg_marks)
        else:
            st.write("(no leg marks recorded)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# View 3: Performance
# ---------------------------------------------------------------------------

def _group(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    g = trades.groupby(by)
    out = pd.DataFrame({
        "RFQs": g.size(),
        "traded": g["won"].sum(),
        "expected P&L": g["expected_pnl"].sum(),
        "realized P&L": g["pnl"].sum(),
    }).reset_index()
    out["traded"] = out["traded"].astype(int)
    return out


def _live_metrics(db_path: str) -> BacktestResult:
    """Live counts from SQL aggregates: compute_metrics does per-RFQ reads, too slow at ~200 RFQs/s."""
    conn = _connect(db_path)
    try:
        def one(sql: str) -> int:
            return conn.execute(sql).fetchone()["n"]

        res = BacktestResult(
            rfqs_received=one("SELECT COUNT(*) AS n FROM rfq"),
            rfqs_quoted=one("SELECT COUNT(DISTINCT rfq_id) AS n FROM shadow_decisions "
                            "WHERE decision = 'QUOTED_OK'"),
            rfqs_expired=one("SELECT COUNT(*) AS n FROM rfq WHERE status = 'EXPIRED'"),
            rfqs_executed=one("SELECT COUNT(DISTINCT rfq_id) AS n FROM quotes WHERE status = 'EXECUTED'"),
            n_fills=one("SELECT COUNT(*) AS n FROM fills"),
        )
        decided = one("SELECT COUNT(DISTINCT rfq_id) AS n FROM shadow_decisions")
        res.rfqs_rejected = decided - res.rfqs_quoted
        res.quote_rate = res.rfqs_quoted / res.rfqs_received if res.rfqs_received else 0.0
        res.execution_rate = res.rfqs_executed / res.rfqs_quoted if res.rfqs_quoted else 0.0
        return res
    finally:
        conn.close()


@st.fragment(run_every=every)
def _performance_view(r: Dict[str, Any]) -> None:
    backtest = r["mode"] == "backtest"
    result = r["result"] if backtest else _live_metrics(r["db_path"])
    st.header(f"Performance -- {'backtest, ' + BACKTEST_LABEL if backtest else 'live feed (paper)'}")
    if backtest:
        st.caption(
            "Replay of the week's RFQs with exchange-time ordering: params use only games before "
            "Week 1, books only closing lines. Expected P&L = model edge on what traded; realized "
            "P&L = fills settled on final scores. Combos with a pushed leg are void (no P&L).")
    else:
        st.caption("Live counts from the shadow engine. Fills and P&L need our own executions "
                   "(Exchange Drop Copy); the receive-only live feeds provide none, so expect "
                   "zero fills here.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("RFQs received", result.rfqs_received)
    c1.metric("RFQs quoted", result.rfqs_quoted)
    c2.metric("RFQs declined", result.rfqs_rejected)
    c2.metric("RFQs executed", result.rfqs_executed)
    c3.metric("Quote rate", f"{result.quote_rate:.1%}")
    c3.metric("Win rate (executed / quoted)", f"{result.execution_rate:.1%}")
    c4.metric("Fills", result.n_fills)
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Expected P&L", f"${result.expected_pnl:,.2f}")
    c6.metric("Realized P&L", f"${result.realized_pnl:,.2f}")
    c7.metric("Max downswing", f"${result.max_downswing:,.2f}")
    c8.metric("Max upswing", f"${result.max_upswing:,.2f}")

    st.subheader("Cumulative realized P&L (by fill time)")
    if result.equity_curve:
        st.line_chart(pd.DataFrame(result.equity_curve, columns=["time", "cumulative P&L"])
                      .set_index("time"))
    else:
        st.write("(no settled fills)")
    st.subheader("Inventory / exposure over time (net notional)")
    if result.exposure_curve:
        st.line_chart(pd.DataFrame(result.exposure_curve, columns=["time", "exposure"])
                      .set_index("time"))
    else:
        st.write("(no fills)")

    if not backtest:
        return
    trades = pd.DataFrame(r["trades"])
    trades["pnl"] = trades["pnl"].fillna(0.0)
    trades["expected_pnl"] = trades["expected_pnl"].fillna(0.0)
    money = {c: st.column_config.NumberColumn(format="$%.2f") for c in ("expected P&L", "realized P&L")}
    st.subheader("Results by combo family")
    st.dataframe(_group(trades, "family"), hide_index=True, width="stretch", column_config=money)
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("By combo size (legs)")
        st.dataframe(_group(trades, "n_legs"), hide_index=True, width="stretch",
                     column_config=money)
    with c2:
        st.subheader("By requester side")
        st.dataframe(_group(trades, "requester_side"), hide_index=True, width="stretch",
                     column_config=money)
    st.subheader("By game")
    st.dataframe(_group(trades, "game"), hide_index=True, width="stretch", column_config=money)
    meta = r["meta"]
    lg = meta["league_params"]
    st.caption(
        f"Params `{meta['params_version']}` (estimator {'tuned on train seasons' if meta.get('estimator_tuned') else 'defaults'}): "
        f"sigma at mean points {lg['sigma_at_mean_points']:.2f}, rho {lg['rho']:.3f}, "
        f"{lg['n_games']} games of history. Requester side: {meta['buy_share']:.0%} buy. "
        f"{meta['n_pushed_combos']} combos voided by a push.")


# ---------------------------------------------------------------------------
# View 4: Engine status
# ---------------------------------------------------------------------------

@st.fragment(run_every=every)
def _engine_view(r: Dict[str, Any]) -> None:
    conn = _connect(r["db_path"])
    try:
        st.header("Engine status -- shadow quoting engine")
        st.caption("Eligibility -> pricer -> risk -> draft. Drafts are stored with "
                   "status='shadow' / origin='shadow' and are never submitted.")

        def one(sql: str) -> int:
            return conn.execute(sql).fetchone()["n"]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("RFQs seen", one("SELECT COUNT(DISTINCT rfq_id) AS n FROM shadow_decisions"))
        c2.metric("Quoted (drafts)", one("SELECT COUNT(DISTINCT rfq_id) AS n FROM shadow_decisions "
                                         "WHERE decision = 'QUOTED_OK'"))
        c3.metric("Skipped", one("SELECT COUNT(*) AS n FROM shadow_decisions "
                                 "WHERE substr(decision, 1, 5) = 'SKIP_'"))
        c4.metric("Declined (pricer/risk)", one(
            "SELECT COUNT(*) AS n FROM shadow_decisions "
            "WHERE decision <> 'QUOTED_OK' AND substr(decision, 1, 5) <> 'SKIP_'"))

        st.subheader("Skip / decline reason breakdown")
        reasons = conn.execute("SELECT decision, COUNT(*) AS n FROM shadow_decisions "
                               "GROUP BY decision ORDER BY n DESC").fetchall()
        st.table([{"reason": x["decision"], "count": x["n"]} for x in reasons])

        st.subheader("Quote latency: RFQ posted -> we decided (live feed only)")
        lat_rows = conn.execute(
            "SELECT latency_ms, over_budget FROM quote_latency "
            "ORDER BY id DESC LIMIT 5000").fetchall()
        if lat_rows:
            latencies = sorted(x["latency_ms"] for x in lat_rows)
            n = len(latencies)

            def pct(p: float) -> float:
                return latencies[min(n - 1, int(p * n))]

            breaches = sum(1 for x in lat_rows if x["over_budget"])
            budget_ms = r.get("monitor").config.quote_latency_budget_ms if r.get("monitor") else 400
            lc1, lc2, lc3, lc4 = st.columns(4)
            lc1.metric("Samples", n)
            lc2.metric("p50", f"{pct(0.50):.0f} ms")
            lc3.metric("p95", f"{pct(0.95):.0f} ms")
            lc4.metric(f"Over {budget_ms}ms", f"{breaches} ({breaches / n:.0%})")
        else:
            st.caption("No live latency samples yet -- this is only measured on the live "
                       "feed (backtest replay uses virtual time, not the real clock).")

        st.subheader("Stored draft quotes (status='shadow')")
        drafts = conn.execute(
            "SELECT quote_id, rfq_id, buy_price, sell_price, buy_qty_decimal, sell_qty_decimal, "
            "model_version, params_version, decided_by, created_time, input_snapshot_json "
            "FROM quotes WHERE status = 'shadow' ORDER BY rowid DESC LIMIT ?",
            (LIVE_ROW_LIMIT if r["mode"] == "live" else -1,)).fetchall()

        def fair(q) -> Optional[float]:
            try:
                return json.loads(q["input_snapshot_json"] or "{}").get("fair_value")
            except (ValueError, TypeError):
                return None

        st.dataframe(pd.DataFrame([{
            "quote_id": q["quote_id"], "rfq_id": q["rfq_id"], "fair": fair(q),
            "buy (offer)": q["buy_price"], "sell (bid)": q["sell_price"],
            "buy qty": q["buy_qty_decimal"], "sell qty": q["sell_qty_decimal"],
            "model": q["model_version"], "params": q["params_version"],
            "decided at": q["created_time"], "decided by": q["decided_by"],
        } for q in drafts]), width="stretch", hide_index=True)
    finally:
        conn.close()


# The run-dependent views render once a control above has produced a DB (and
# stay inert in bare mode, ``python -c "import dashboard.app"``).
if run is None or not run.get("db_path"):
    for tab in view[:4]:
        with tab:
            st.info(f'Press "Run backtest -- {BACKTEST_LABEL}" for the historical RFQs, or '
                    '"Live monitor RFQ feed" to watch the live feed.')
else:
    with view[0]:
        _rfq_view(run)
    with view[1]:
        _pricing_view(run)
    with view[2]:
        _performance_view(run)
    with view[3]:
        _engine_view(run)

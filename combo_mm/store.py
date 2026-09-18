"""Event store: durable state for the Polymarket US RFQ pipeline.

Every stream record (``raw_events``) is stored first; projections into the
read model (RFQ state, quote state, leg cache, fills ledger) are built by
replaying rows in order. Idempotency is load-bearing because the RFQ stream
offers no replay and no ordering guarantees (see :mod:`combo_mm.stream`):

1. Event-level: ``event_key`` dedups (event ID when present, else a stable
   hash of the payload).
2. Entity-level: only strictly newer ``updatedTime`` updates project onto an
   RFQ or quote. Events carrying older timestamps are recorded but do not
   move entity state -- this is also what makes recovery and duplicate
   redelivery safe.
3. Fill-level: ``record_fill`` is ``INSERT OR IGNORE`` on ``fillId`` (falling
   back to ``dropCopySeq``), so Drop Copy redeliveries are no-ops. The
   Drop Copy resume token is persisted in ``dropcopy_state``.

Wire shapes: payloads are stored verbatim (exact camelCase wire names, raw
``settlementPrice`` strings). Projections read the wire fields.

``rfq_expired`` is client-derived ONLY: it never arrives on the real public
stream; the only producer is :meth:`sweep_confirmation_deadlines`, and every
such row is flagged ``client_derived=1`` so audit can tell our inferences
from exchange truth. Durable-recovery inserts are likewise flagged with
``source='durable'``.

SQLite only (``sqlite3`` stdlib). WAL mode for concurrent readers.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from combo_mm.events import (
    RFQ_TERMINAL_STATUSES,
    QUOTE_TERMINAL_STATUSES,
    RFQ_EVENT_STATUS,
    NormalizedEvent,
    quote_allows,
    quote_target_status,
    rfq_allows,
)
from combo_mm.normalize import NormalizeError  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

__all__ = ["EventStore"]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp to an aware datetime, or None.

    Mixed wire formats are the norm (``…:01Z`` vs ``…:01.100000Z``), and
    lexicographic comparison mis-orders those (``'.' < 'Z'``), so parse
    instead of comparing strings. Naive timestamps are assumed UTC.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_newer(candidate: Optional[str], current: Optional[str], *,
              allow_equal: bool = False) -> bool:
    """True if ``candidate`` updatedTime is strictly newer than ``current``.

    NULL current always loses to a non-NULL candidate; NULL candidate never
    wins; equal timestamps are not newer (idempotent replays stay inert)
    unless ``allow_equal`` is set.
    """
    if candidate is None:
        return False
    pc = _parse_ts(candidate)
    if pc is None:
        return False
    if current is None:
        return True
    pu = _parse_ts(current)
    if pu is None:
        return True
    return pc >= pu if allow_equal else pc > pu


class EventStore:
    """SQLite-backed event store and read-model projections."""

    def __init__(self, path: str = ":memory:", *, synchronous: str = "FULL") -> None:
        """``synchronous="NORMAL"`` skips the per-commit fsync (WAL mode stays
        corruption-safe; an OS crash can lose only the last commits). It is
        ~15x faster on Windows and meant for high-rate paper monitoring, e.g.
        the live quoter-gateway feed at ~200 RFQs/s; the default stays FULL.
        """
        if synchronous not in ("FULL", "NORMAL"):
            raise ValueError("synchronous must be 'FULL' or 'NORMAL'")
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()
        self._conn.execute(f"PRAGMA synchronous={synchronous}")

    # -- schema -------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock, self._conn:
            cur = self._conn.cursor()
            cur.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS raw_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
                    event_type TEXT NOT NULL,
                    rfq_id TEXT,
                    quote_id TEXT,
                    symbol TEXT,
                    event_key TEXT NOT NULL UNIQUE,
                    client_derived INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'stream',
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_raw_rfq ON raw_events(rfq_id);
                CREATE INDEX IF NOT EXISTS idx_raw_quote ON raw_events(quote_id);

                CREATE TABLE IF NOT EXISTS rfq (
                    rfq_id TEXT PRIMARY KEY,
                    symbol TEXT,
                    creator_user_id TEXT,
                    qty_decimal REAL,
                    cash_order_qty REAL,
                    created_time TEXT,
                    updated_time TEXT,
                    rest_remainder INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    last_event_id TEXT
                );

                CREATE TABLE IF NOT EXISTS rfq_legs (
                    rfq_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT,
                    settlement_price REAL,
                    PRIMARY KEY (rfq_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS quotes (
                    quote_id TEXT PRIMARY KEY,
                    rfq_id TEXT,
                    symbol TEXT,
                    maker_user_id TEXT,
                    status TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'shadow'
                        CHECK (origin IN ('shadow', 'live')),
                    buy_price REAL,
                    sell_price REAL,
                    buy_qty_decimal REAL,
                    sell_qty_decimal REAL,
                    accepted_side TEXT,
                    confirmation_deadline TEXT,
                    execution_deadline TEXT,
                    order_id TEXT,
                    client_order_id TEXT,
                    created_time TEXT,
                    updated_time TEXT,
                    last_event_id TEXT,
                    model_version TEXT,
                    params_version TEXT,
                    input_snapshot_json TEXT,
                    decided_by TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_quotes_rfq ON quotes(rfq_id);

                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    rfq_id TEXT,
                    quote_id TEXT,
                    symbol TEXT,
                    side TEXT,
                    price REAL,
                    qty REAL,
                    executed_time TEXT,
                    source TEXT,
                    drop_copy_seq TEXT,
                    event_id TEXT
                );

                CREATE VIEW IF NOT EXISTS positions AS
                    SELECT rfq_id, symbol, side, SUM(qty) AS qty,
                           SUM(qty * price) / NULLIF(SUM(qty), 0) AS avg_price,
                           MAX(executed_time) AS last_fill
                    FROM fills GROUP BY rfq_id, symbol, side;
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, rfq_id TEXT, quote_id TEXT, game_id TEXT,
                    action TEXT NOT NULL, reason TEXT NOT NULL,
                    detail_json TEXT NOT NULL, policy_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exposure_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL, ts TEXT NOT NULL,
                    level TEXT NOT NULL, key TEXT NOT NULL,
                    pending_wcl REAL NOT NULL, executed_wcl REAL NOT NULL,
                    total_wcl REAL NOT NULL, equity REAL NOT NULL,
                    buying_power REAL NOT NULL,
                    UNIQUE(source_id, level, key)
                );
                CREATE TABLE IF NOT EXISTS kill_switch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, state TEXT NOT NULL
                        CHECK (state IN ('tripped', 'reset')),
                    trigger TEXT NOT NULL, detail_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dropcopy_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    resume_token TEXT
                );

                CREATE TABLE IF NOT EXISTS books (
                    symbol TEXT PRIMARY KEY,
                    bid REAL,
                    ask REAL,
                    bid_size REAL,
                    ask_size REAL,
                    seq INTEGER,
                    ts TEXT
                );

                CREATE TABLE IF NOT EXISTS shadow_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rfq_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT,
                    fair_price REAL,
                    buy_price REAL,
                    sell_price REAL,
                    spread_bps REAL,
                    expected_edge_bps REAL,
                    buy_qty TEXT,
                    sell_qty TEXT,
                    components_json TEXT,
                    ts TEXT NOT NULL
                );

                -- Live-feed screen per RFQ (combo_mm.rfq_screen): gateway
                -- extras normalize() drops, plus the NFL same-game screen.
                -- seq is the rfq row's rowid (arrival order); (rank, seq)
                -- serves "quotable first, newest first" without a sort.
                CREATE TABLE IF NOT EXISTS rfq_screen (
                    rfq_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    direction TEXT,
                    side TEXT,
                    condition_id TEXT,
                    submission_deadline TEXT,
                    n_legs INTEGER NOT NULL,
                    n_resolved INTEGER NOT NULL,
                    n_nfl_legs INTEGER NOT NULL,
                    screen TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    catalog_version INTEGER NOT NULL,
                    checks_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_screen_rank_seq ON rfq_screen(rank, seq);
                CREATE INDEX IF NOT EXISTS idx_screen_unresolved
                    ON rfq_screen(catalog_version) WHERE n_resolved < n_legs;

                -- Live-only wall-clock latency samples: how long from an RFQ
                -- posting to us deciding whether to quote it
                -- (combo_mm.live_monitor). Never written by replay/backtest
                -- (virtual time has no real elapsed wall clock), so this is
                -- deliberately excluded from state_digest().
                CREATE TABLE IF NOT EXISTS quote_latency (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rfq_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    posted_at TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    quoted INTEGER NOT NULL,
                    over_budget INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'live'
                );
                CREATE INDEX IF NOT EXISTS idx_latency_over_budget
                    ON quote_latency(over_budget);
                CREATE TABLE IF NOT EXISTS live_trades (
                    rfq_id TEXT PRIMARY KEY, price REAL, size REAL,
                    executed_at TEXT, recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_engine_health (
                    id INTEGER PRIMARY KEY CHECK (id = 1), started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL, messages_processed INTEGER NOT NULL,
                    errors INTEGER NOT NULL, gateway_connected INTEGER NOT NULL,
                    buffer_drops INTEGER NOT NULL
                );
                """
            )
            latency_columns = {r[1] for r in cur.execute(
                "PRAGMA table_info(quote_latency)").fetchall()}
            for column in ("started_at", "wait_ms", "compute_ms", "fetch_ms",
                           "solve_ms", "delivery_ms", "queue_ms"):
                if column not in latency_columns:
                    cur.execute(f"ALTER TABLE quote_latency ADD COLUMN {column} "
                                + ("TEXT" if column == "started_at" else "REAL"))
            screen_columns = {r[1] for r in cur.execute("PRAGMA table_info(rfq_screen)")}
            if "checks_json" not in screen_columns:
                cur.execute("ALTER TABLE rfq_screen ADD COLUMN checks_json TEXT")
            # Migration for DBs created before the shadow-engine columns
            # existed: origin / model_version / params_version /
            # input_snapshot_json / decided_by on quotes. (The CHECK
            # constraint on origin only applies to fresh tables; ALTER
            # TABLE cannot add one.)
            #
            # origin semantics: 'shadow' = drafted by the paper engine
            # (record_shadow_draft); 'live' = exchange-observed quote
            # lifecycle rows (_upsert_quote). Nothing in this repo can
            # place a live order, so 'live' here means "wire-observed",
            # kept unmistakable from engine drafts for the day a live
            # path might exist.
            existing = {r[1] for r in
                        cur.execute("PRAGMA table_info(quotes)").fetchall()}
            for col, ddl in (
                ("origin", "TEXT NOT NULL DEFAULT 'shadow'"),
                ("model_version", "TEXT"),
                ("params_version", "TEXT"),
                ("input_snapshot_json", "TEXT"),
                ("decided_by", "TEXT"),
            ):
                if col not in existing:
                    cur.execute(f"ALTER TABLE quotes ADD COLUMN {col} {ddl}")
            cur.execute(
                "UPDATE quotes SET origin='live' "
                "WHERE origin='shadow' AND status <> 'shadow'"
            )

    # -- raw event log ---------------------------------------------------------
    def apply(self, event: NormalizedEvent, *, source: str = "stream",
              record_inventory: bool = True) -> bool:
        """Append a normalized event and project it. Returns False on dupes."""
        with self._lock, self._conn:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO raw_events
                        (event_id, event_type, rfq_id, quote_id, symbol,
                         event_key, client_derived, payload_json, source,
                         recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        None,  # stream messages carry no event_id of their own
                        event.event_type,
                        event.rfq_id,
                        event.quote_id,
                        event.symbol,
                        event.event_key,
                        1 if event.client_derived else 0,
                        json.dumps(event.payload),
                        source,
                        event.received_at,
                    ),
                )
            except sqlite3.IntegrityError:
                log.debug("duplicate event ignored: %s", event.event_key)
                return False
            self._project(cur, event)
        if record_inventory and event.event_type in (
                "rfq_closed", "rfq_cancelled", "rfq_expired", "quote_deleted"):
            from combo_mm.inventory import InventoryProvider
            InventoryProvider(self).record(event.event_at, f"event:{event.event_key}")
        return True

    def _project(self, cur: sqlite3.Cursor, event: NormalizedEvent) -> None:
        p = event.payload
        etype = event.event_type
        handler = {
            "rfq_created": self._p_rfq_created,
            "rfq_updated": self._p_rfq_updated,
            "rfq_cancelled": self._p_rfq_terminal,
            "rfq_closed": self._p_rfq_terminal,
            "rfq_expired": self._p_rfq_terminal,
            "quote_draft_revised": self._p_quote_draft,
            "quote_created": self._p_quote_created,
            "quote_accepted": self._p_quote_accepted,
            "quote_confirmed": self._p_quote_confirmed,
            "quote_executed": self._p_quote_executed,
            "quote_deleted": self._p_quote_deleted,
            "drop_copy_fill": self._p_drop_copy_fill,
        }.get(etype)
        if handler is None:
            log.warning("no projection for event type %r", etype)
            return
        handler(cur, event, p)

    # -- RFQ projections ----------------------------------------------------------
    @staticmethod
    def _entity_time(p: Dict[str, Any], event: NormalizedEvent) -> Optional[str]:
        """Entity updated_time: wire updatedTime, else the exchange timestamp."""
        return p.get("updatedTime") or event.event_at

    @staticmethod
    def _rfq_wire(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Canonical RFQ entity fields from a wire-shaped payload."""
        return {
            "rfq_id": payload.get("id") or payload.get("rfq_id"),
            "symbol": payload.get("symbol"),
            "creator_user_id": payload.get("rfqCreatorUserId"),
            "qty_decimal": _to_float(payload.get("qtyDecimal")),
            "cash_order_qty": _to_float(payload.get("cashOrderQty")),
            "created_time": payload.get("createdTime"),
            "rest_remainder": 1 if payload.get("restRemainder") else 0,
            "status": payload.get("status"),
        }

    def _get_rfq_row(self, cur: sqlite3.Cursor, rfq_id: str) -> Optional[sqlite3.Row]:
        return cur.execute("SELECT * FROM rfq WHERE rfq_id = ?", (rfq_id,)).fetchone()

    def _p_rfq_created(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                       p: Dict[str, Any]) -> None:
        fields = self._rfq_wire(p)
        # The canonical RFQ id lives on the event (normalize() resolves it
        # from rfq_id / rfqId / id); the payload copy is only a fallback.
        rfq_id = fields.pop("rfq_id") or event.rfq_id
        if not rfq_id:
            return
        updated_time = self._entity_time(p, event)
        existing = self._get_rfq_row(cur, rfq_id)
        if existing is not None:
            if not _is_newer(updated_time, existing["updated_time"]):
                return  # stale or duplicate create; entity keeps newer state
            if not rfq_allows(existing["status"], "OPEN"):
                return  # must not regress a live/quoted RFQ to OPEN
        cur.execute(
            """
            INSERT INTO rfq (rfq_id, symbol, creator_user_id, qty_decimal,
                             cash_order_qty, created_time, updated_time,
                             rest_remainder, status, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rfq_id) DO UPDATE SET
                symbol=excluded.symbol,
                creator_user_id=excluded.creator_user_id,
                qty_decimal=excluded.qty_decimal,
                cash_order_qty=excluded.cash_order_qty,
                created_time=excluded.created_time,
                updated_time=excluded.updated_time,
                rest_remainder=excluded.rest_remainder,
                status=excluded.status,
                last_event_id=excluded.last_event_id
            """,
            (rfq_id, fields["symbol"], fields["creator_user_id"],
             fields["qty_decimal"], fields["cash_order_qty"],
             fields["created_time"], updated_time,
             fields["rest_remainder"], fields["status"] or "OPEN",
             event.event_key),
        )
        self._replace_legs(cur, rfq_id, p.get("comboLegs") or [])

    def _p_rfq_updated(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                       p: Dict[str, Any]) -> None:
        rfq_id = event.rfq_id or p.get("id")
        if not rfq_id:
            return
        row = self._get_rfq_row(cur, rfq_id)
        updated_time = self._entity_time(p, event)
        if row is None:
            # Update for an RFQ we never saw: treat as a late create.
            created = dict(p)
            created.setdefault("status", "OPEN")
            created.setdefault("id", rfq_id)
            self._p_rfq_created(cur, event, created)
            return
        if not _is_newer(updated_time, row["updated_time"]):
            return
        legs = p.get("comboLegs")
        if legs is not None:
            self._replace_legs(cur, rfq_id, legs)
        cur.execute(
            "UPDATE rfq SET updated_time = ?, last_event_id = ? WHERE rfq_id = ?",
            (updated_time, event.event_key, rfq_id),
        )

    def _p_rfq_terminal(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                        p: Dict[str, Any]) -> None:
        target = RFQ_EVENT_STATUS[event.event_type]
        rfq_id = event.rfq_id or p.get("id")
        if not rfq_id:
            return
        row = self._get_rfq_row(cur, rfq_id)
        updated_time = self._entity_time(p, event)
        if row is None:
            # Terminal event for an RFQ we never saw (created while we were
            # away and missed by recovery): record a terminal stub row so
            # the close stays auditable. Mirrors _p_rfq_updated's late-create.
            cur.execute(
                """
                INSERT INTO rfq (rfq_id, symbol, creator_user_id, qty_decimal,
                                 cash_order_qty, created_time, updated_time,
                                 rest_remainder, status, last_event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rfq_id, event.symbol, None, None, None, None, updated_time,
                 0, target, event.event_key),
            )
            return
        # Terminal moves accept an EQUAL updatedTime: RFQs never reopen, so a
        # close stamped at the same instant as the last update (e.g. a
        # poll-derived close) is still true. Strictly older ones are ignored.
        if not _is_newer(updated_time, row["updated_time"], allow_equal=True):
            return
        if rfq_allows(row["status"], target):
            cur.execute(
                "UPDATE rfq SET status = ?, updated_time = ?, last_event_id = ? "
                "WHERE rfq_id = ?",
                (target, updated_time, event.event_key, rfq_id),
            )
        else:
            log.debug("illegal RFQ transition %s -> %s ignored",
                      row["status"], target)

    def _replace_legs(self, cur: sqlite3.Cursor, rfq_id: str,
                      legs: List[Dict[str, Any]]) -> None:
        cur.execute("DELETE FROM rfq_legs WHERE rfq_id = ?", (rfq_id,))
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            cur.execute(
                "INSERT INTO rfq_legs (rfq_id, symbol, side, settlement_price) "
                "VALUES (?, ?, ?, ?)",
                (rfq_id, leg.get("symbol"), leg.get("side"),
                 _to_float(leg.get("settlementPrice"))),
            )

    # -- quote projections ---------------------------------------------------------
    @staticmethod
    def _quote_wire(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "quote_id": payload.get("id") or payload.get("quote_id"),
            "rfq_id": payload.get("rfqId"),
            "symbol": payload.get("symbol"),
            "maker_user_id": payload.get("creatorRfqUserId"),
            "status": payload.get("status"),
            "buy_price": _to_float(payload.get("buyPrice")),
            "sell_price": _to_float(payload.get("sellPrice")),
            "buy_qty_decimal": _to_float(payload.get("buyQtyDecimal")),
            "sell_qty_decimal": _to_float(payload.get("sellQtyDecimal")),
            "accepted_side": payload.get("acceptedSide"),
            "confirmation_deadline": payload.get("confirmationDeadline"),
            "execution_deadline": payload.get("executionDeadline"),
            "order_id": payload.get("orderId"),
            "client_order_id": payload.get("clientOrderId"),
            "created_time": payload.get("createdTime"),
        }

    def _get_quote_row(self, cur: sqlite3.Cursor,
                       quote_id: str) -> Optional[sqlite3.Row]:
        return cur.execute(
            "SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)).fetchone()

    def _quote_id_for(self, event: NormalizedEvent, p: Dict[str, Any]) -> Optional[str]:
        return event.quote_id or p.get("id") or p.get("quote_id")

    def _upsert_quote(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                      fields: Dict[str, Any],
                      target_status: Optional[str]) -> None:
        quote_id = fields["quote_id"]
        if not quote_id:
            return
        updated_time = self._entity_time(event.payload, event)
        row = self._get_quote_row(cur, quote_id)
        if row is not None and not _is_newer(updated_time, row["updated_time"]):
            return  # stale event; entity keeps newer state
        if row is None:
            status = target_status or fields["status"] or "ACTIVE"
        elif target_status is None:
            status = row["status"]
        elif quote_allows(row["status"], target_status):
            status = target_status
        else:
            status = row["status"]  # would regress: keep current state
        cur.execute(
            """
            INSERT INTO quotes
                (quote_id, rfq_id, symbol, maker_user_id, status, origin, buy_price,
                 sell_price, buy_qty_decimal, sell_qty_decimal, accepted_side,
                 confirmation_deadline, execution_deadline, order_id,
                 client_order_id, created_time, updated_time, last_event_id)
            VALUES (?, ?, ?, ?, ?, 'live', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(quote_id) DO UPDATE SET
                -- Lifecycle events carry partial payloads (e.g. a confirm
                -- without acceptedSide): absent fields keep the stored value.
                rfq_id=COALESCE(excluded.rfq_id, quotes.rfq_id),
                symbol=COALESCE(excluded.symbol, quotes.symbol),
                maker_user_id=COALESCE(excluded.maker_user_id, quotes.maker_user_id),
                status=excluded.status,
                buy_price=COALESCE(excluded.buy_price, quotes.buy_price),
                sell_price=COALESCE(excluded.sell_price, quotes.sell_price),
                buy_qty_decimal=COALESCE(excluded.buy_qty_decimal, quotes.buy_qty_decimal),
                sell_qty_decimal=COALESCE(excluded.sell_qty_decimal, quotes.sell_qty_decimal),
                accepted_side=COALESCE(excluded.accepted_side, quotes.accepted_side),
                confirmation_deadline=COALESCE(excluded.confirmation_deadline,
                                               quotes.confirmation_deadline),
                execution_deadline=COALESCE(excluded.execution_deadline,
                                            quotes.execution_deadline),
                order_id=COALESCE(excluded.order_id, quotes.order_id),
                client_order_id=COALESCE(excluded.client_order_id, quotes.client_order_id),
                created_time=COALESCE(excluded.created_time, quotes.created_time),
                updated_time=excluded.updated_time,
                last_event_id=excluded.last_event_id
            """,
            (quote_id, fields["rfq_id"], fields["symbol"],
             fields["maker_user_id"], status, fields["buy_price"],
             fields["sell_price"], fields["buy_qty_decimal"],
             fields["sell_qty_decimal"], fields["accepted_side"],
             fields["confirmation_deadline"], fields["execution_deadline"],
             fields["order_id"], fields["client_order_id"],
             fields["created_time"], updated_time, event.event_key),
        )

    def _p_quote_draft(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                       p: Dict[str, Any]) -> None:
        fields = self._quote_wire(p)
        fields["quote_id"] = self._quote_id_for(event, p)
        # normalize() keeps only a coerced subset in the payload; the
        # canonical rfq/quote ids live on the event itself.
        fields["rfq_id"] = event.rfq_id or fields["rfq_id"]
        if self._quote_blocked_for_terminal_rfq(cur, fields):
            return
        current = self._current_quote_status(cur, fields["quote_id"])
        # quote_target_status encodes the documented replacement exception:
        # ACTIVE -> REPLACED on a revised draft; REPLACED -> ACTIVE when the
        # replacement goes live; never regresses anything further along.
        target = quote_target_status("quote_draft_revised", current)
        self._upsert_quote(cur, event, fields, target_status=target)

    def _p_quote_created(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                         p: Dict[str, Any]) -> None:
        fields = self._quote_wire(p)
        fields["quote_id"] = self._quote_id_for(event, p)
        # normalize() keeps only a coerced subset in the payload; the
        # canonical rfq/quote ids live on the event itself.
        fields["rfq_id"] = event.rfq_id or fields["rfq_id"]
        if self._quote_blocked_for_terminal_rfq(cur, fields):
            return
        # Fired on create OR replace: a re-quote revives a REPLACED quote
        # (quote_allows encodes that as the allowed same-rank exception).
        self._upsert_quote(cur, event, fields, target_status="ACTIVE")
        self._advance_rfq_from_quote(cur, event, fields["rfq_id"], "quote_created")

    def _p_quote_accepted(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                          p: Dict[str, Any]) -> None:
        fields = self._quote_wire(p)
        fields["quote_id"] = self._quote_id_for(event, p)
        # normalize() keeps only a coerced subset in the payload; the
        # canonical rfq/quote ids live on the event itself.
        fields["rfq_id"] = event.rfq_id or fields["rfq_id"]
        # Race rule: the public rfq_closed may precede the private accept.
        # The RFQ row stays terminal, but the quote row still advances --
        # never create/advance quote state is wrong here; the accept is
        # authoritative for OUR quote.
        current = self._current_quote_status(cur, fields["quote_id"])
        if current is not None:
            # Lifecycle events advance existing quote rows but never invent
            # one: an accept for an unknown quote is ignored at the quote
            # level (GetQuotes reconciliation fills it in); the RFQ row may
            # still advance monotonically below.
            target = quote_target_status("quote_accepted", current)
            self._upsert_quote(cur, event, fields, target_status=target)
        else:
            log.debug("quote_accepted for unknown quote %s: no row created",
                      fields["quote_id"])
        self._advance_rfq_from_quote(cur, event, fields["rfq_id"], "quote_accepted")

    def _p_quote_confirmed(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                           p: Dict[str, Any]) -> None:
        fields = self._quote_wire(p)
        fields["quote_id"] = self._quote_id_for(event, p)
        # normalize() keeps only a coerced subset in the payload; the
        # canonical rfq/quote ids live on the event itself.
        fields["rfq_id"] = event.rfq_id or fields["rfq_id"]
        current = self._current_quote_status(cur, fields["quote_id"])
        if current is not None:
            target = quote_target_status("quote_confirmed", current)
            self._upsert_quote(cur, event, fields, target_status=target)
        else:
            log.debug("quote_confirmed for unknown quote %s: no row created",
                      fields["quote_id"])
        self._advance_rfq_from_quote(cur, event, fields["rfq_id"], "quote_confirmed")

    def _p_quote_executed(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                           p: Dict[str, Any]) -> None:
        # EXECUTED = paired orders accepted for submission, NOT a fill.
        # Fills reconcile exclusively through Drop Copy.
        fields = self._quote_wire(p)
        fields["quote_id"] = self._quote_id_for(event, p)
        # normalize() keeps only a coerced subset in the payload; the
        # canonical rfq/quote ids live on the event itself.
        fields["rfq_id"] = event.rfq_id or fields["rfq_id"]
        current = self._current_quote_status(cur, fields["quote_id"])
        if current is not None:
            target = quote_target_status("quote_executed", current)
            self._upsert_quote(cur, event, fields, target_status=target)
        else:
            log.debug("quote_executed for unknown quote %s: no row created",
                      fields["quote_id"])
        self._advance_rfq_from_quote(cur, event, fields["rfq_id"], "quote_executed")

    def _p_quote_deleted(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                         p: Dict[str, Any]) -> None:
        # Terminal for the quote only: the RFQ may still be quoted by others
        # (or re-quoted by us), so the RFQ row is left alone.
        fields = self._quote_wire(p)
        fields["quote_id"] = self._quote_id_for(event, p)
        fields["rfq_id"] = event.rfq_id or fields["rfq_id"]
        current = self._current_quote_status(cur, fields["quote_id"])
        if current is not None:
            target = quote_target_status("quote_deleted", current)
            self._upsert_quote(cur, event, fields, target_status=target)
        else:
            log.debug("quote_deleted for unknown quote %s: no row created",
                      fields["quote_id"])

    def _quote_blocked_for_terminal_rfq(self, cur: sqlite3.Cursor,
                                        fields: Dict[str, Any]) -> bool:
        """True when a create/draft must not open a NEW quote row.

        "Stop quoting" for a terminal RFQ: the raw event is still recorded
        for audit, but no new quote row is created. Existing rows still
        advance via the accept/confirm/execute path (the documented race).
        """
        quote_id = fields["quote_id"]
        if not quote_id or self._get_quote_row(cur, quote_id) is not None:
            return False
        rfq_id = fields["rfq_id"]
        if not rfq_id:
            return False
        rfq_row = self._get_rfq_row(cur, rfq_id)
        if rfq_row is not None and rfq_row["status"] in RFQ_TERMINAL_STATUSES:
            log.debug("quote create/draft for terminal RFQ %s: no quote row",
                      rfq_id)
            return True
        return False

    def _advance_rfq_from_quote(self, cur: sqlite3.Cursor,
                                event: NormalizedEvent,
                                rfq_id: Optional[str], event_type: str) -> None:
        """Advance the RFQ row along the quote lifecycle (monotonic only).

        A terminal RFQ never regresses: ``rfq_allows`` rejects e.g.
        CLOSED -> ACCEPTED, which is exactly the documented close/accept race.
        """
        target = RFQ_EVENT_STATUS.get(event_type)
        if not rfq_id or target is None:
            return
        row = self._get_rfq_row(cur, rfq_id)
        if row is None:
            return
        updated_time = self._entity_time(event.payload, event)
        if not _is_newer(updated_time, row["updated_time"]):
            return
        if rfq_allows(row["status"], target):
            cur.execute(
                "UPDATE rfq SET status = ?, updated_time = ?, last_event_id = ? "
                "WHERE rfq_id = ?",
                (target, updated_time, event.event_key, rfq_id),
            )

    def _current_quote_status(self, cur: sqlite3.Cursor,
                              quote_id: Optional[str]) -> Optional[str]:
        if not quote_id:
            return None
        row = self._get_quote_row(cur, quote_id)
        return row["status"] if row else None

    # -- fills (Drop Copy only) ----------------------------------------------------------
    def _p_drop_copy_fill(self, cur: sqlite3.Cursor, event: NormalizedEvent,
                          p: Dict[str, Any]) -> None:
        self.record_fill(
            fill_id=p.get("fillId") or p.get("dropCopySeq") or event.event_key,
            rfq_id=event.rfq_id,
            quote_id=event.quote_id,
            symbol=event.symbol or p.get("symbol"),
            side=p.get("side"),
            price=_to_float(p.get("price")),
            qty=_to_float(p.get("qty")),
            executed_time=p.get("executedTime") or event.event_at,
            source="drop_copy",
            drop_copy_seq=p.get("dropCopySeq"),
            event_id=event.event_key,
        )

    def record_fill(self, *, fill_id: str, rfq_id: Optional[str],
                    quote_id: Optional[str], symbol: Optional[str],
                    side: Optional[str], price: Optional[float],
                    qty: Optional[float], executed_time: Optional[str],
                    source: str = "drop_copy",
                    drop_copy_seq: Optional[str] = None,
                    event_id: Optional[str] = None) -> bool:
        """Exactly-once fill write. Returns False when already recorded."""
        with self._lock, self._conn:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO fills
                    (fill_id, rfq_id, quote_id, symbol, side, price, qty,
                     executed_time, source, drop_copy_seq, event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fill_id, rfq_id, quote_id, symbol, side, price, qty,
                 executed_time, source, drop_copy_seq, event_id),
            )
            inserted = cur.rowcount > 0
        if inserted:
            from combo_mm.inventory import InventoryProvider
            InventoryProvider(self).record(executed_time or "", f"fill:{fill_id}")
        return inserted

    # -- Drop Copy resume token ----------------------------------------------------------
    def get_drop_copy_token(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT resume_token FROM dropcopy_state WHERE id = 1"
            ).fetchone()
            return row["resume_token"] if row else None

    def set_drop_copy_token(self, token: Optional[str]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO dropcopy_state (id, resume_token) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET resume_token = excluded.resume_token",
                (token,),
            )

    # -- client-derived expiry sweeper ----------------------------------------------------------
    def sweep_confirmation_deadlines(self, now: datetime,
                                     maker_user_id: str) -> List[Dict[str, Any]]:
        """Expire ACCEPTED quotes whose confirmationDeadline has passed.

        Emits explicitly client-derived ``rfq_expired`` rows (the ONLY
        producer of that event type) and moves the quote/RFQ to EXPIRED
        locally. Returns the expired ``[{rfq_id, quote_id}]`` pairs.
        """
        now_utc = now.astimezone(timezone.utc)
        now_iso = now_utc.isoformat().replace("+00:00", "Z")
        expired: List[Dict[str, Any]] = []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT quote_id, rfq_id, symbol, confirmation_deadline
                FROM quotes
                WHERE status = 'ACCEPTED'
                  AND maker_user_id = ?
                  AND confirmation_deadline IS NOT NULL
                """,
                (maker_user_id,),
            ).fetchall()
            for row in rows:
                # Compare parsed datetimes: mixed wire precisions/offsets do
                # not order correctly as strings (see _parse_ts).
                deadline = _parse_ts(row["confirmation_deadline"])
                if deadline is None or not deadline < now_utc:
                    continue
                payload = {
                    "id": row["rfq_id"],
                    "status": "EXPIRED",
                    "updatedTime": now_iso,
                    "reason": "confirmationDeadline passed without confirmation",
                }
                event = NormalizedEvent(
                    event_key=f"sweeper:{row['quote_id']}:{row['confirmation_deadline']}",
                    event_type="rfq_expired",
                    rfq_id=row["rfq_id"],
                    quote_id=row["quote_id"],
                    symbol=row["symbol"],
                    event_at=now_iso,
                    received_at=now_iso,
                    payload=payload,
                    client_derived=True,
                )
                if self.apply(event, source="sweeper"):
                    with self._conn:
                        self._conn.execute(
                            "UPDATE quotes SET status = 'EXPIRED', updated_time = ? "
                            "WHERE quote_id = ? AND status = 'ACCEPTED'",
                            (now_iso, row["quote_id"]),
                        )
                    expired.append(
                        {"rfq_id": row["rfq_id"], "quote_id": row["quote_id"]}
                    )
        if expired:
            log.info("sweeper expired %d quotes past confirmationDeadline",
                     len(expired))
        return expired

    # -- durable recovery ----------------------------------------------------------
    def apply_durable_rfq(self, payload: Dict[str, Any]) -> str:
        """Insert or refresh an RFQ from a durable read (``GetRFQs``).

        Returns ``"inserted"`` / ``"refreshed"`` / ``"ignored"``. Monotonic:
        refreshes only project when the durable ``updatedTime`` is strictly
        newer; terminal durable states always win over non-terminal local
        ones, and terminal local states never regress.
        """
        merged = {**payload, "id": payload.get("id") or payload.get("rfq_id")}
        wire = self._rfq_wire(merged)
        rfq_id = wire["rfq_id"]
        if not rfq_id:
            raise ValueError("durable RFQ payload has no id")
        updated_time = merged.get("updatedTime") or merged.get("createdTime")
        with self._lock, self._conn:
            cur = self._conn.cursor()
            row = self._get_rfq_row(cur, rfq_id)
            if row is None:
                self._record_durable_event(
                    cur, f"durable:{rfq_id}", "rfq_created", rfq_id, None,
                    wire["symbol"], merged)
                event = NormalizedEvent(
                    event_key=f"durable:{rfq_id}", event_type="rfq_created",
                    rfq_id=rfq_id, quote_id=None, symbol=wire["symbol"],
                    event_at=updated_time or _utcnow_iso(),
                    received_at=_utcnow_iso(), payload=merged)
                self._p_rfq_created(cur, event, merged)
                return "inserted"
            # Backfill: the durable read is authoritative full state. A
            # stream-bootstrapped partial row (its create was missed) may
            # lack create-time facts the stream never carried; fill NULL
            # columns from the durable values. Present local values are
            # never overwritten here -- staleness for those is decided by
            # updatedTime below.
            backfilled = self._backfill_null_rfq_fields(cur, rfq_id, row, wire)
            if not _is_newer(updated_time, row["updated_time"]):
                return "refreshed" if backfilled else "ignored"
            durable_status = wire["status"] or row["status"]
            legs = merged.get("comboLegs")
            if legs is not None:
                self._replace_legs(cur, rfq_id, legs)
            # Terminal durable truth always wins; otherwise only monotonic
            # moves, and terminal local states never regress.
            if rfq_allows(row["status"], durable_status):
                self._record_durable_event(
                    cur, f"durable:{rfq_id}:{updated_time}",
                    "rfq_updated", rfq_id, None, wire["symbol"], merged)
                cur.execute(
                    "UPDATE rfq SET status = ?, updated_time = ?, "
                    "last_event_id = ? WHERE rfq_id = ?",
                    (durable_status, updated_time,
                     f"durable:{rfq_id}:{updated_time}", rfq_id))
            else:
                cur.execute("UPDATE rfq SET updated_time = ? WHERE rfq_id = ?",
                            (updated_time, rfq_id))
            return "refreshed"

    def _backfill_null_rfq_fields(self, cur: sqlite3.Cursor, rfq_id: str,
                                  row: sqlite3.Row,
                                  wire: Dict[str, Any]) -> bool:
        """Fill NULL create-time columns from authoritative durable values.

        Returns True when at least one column was filled.
        """
        sets: List[str] = []
        params: List[Any] = []
        for col in ("symbol", "creator_user_id", "qty_decimal",
                    "cash_order_qty", "created_time"):
            if row[col] is None and wire.get(col) is not None:
                sets.append(f"{col} = ?")
                params.append(wire[col])
        if not sets:
            return False
        params.append(rfq_id)
        cur.execute(f"UPDATE rfq SET {', '.join(sets)} WHERE rfq_id = ?",
                    params)
        return True

    def apply_durable_quote(self, payload: Dict[str, Any]) -> str:
        """Insert or refresh a quote from a durable read (``GetQuotes``).

        Same monotonic rules as :meth:`apply_durable_rfq`: strictly newer
        ``updatedTime`` required; quote statuses move monotonically toward
        terminal; ``quote_executed`` state from the durable read advances the
        quote but never fabricates a fill.
        """
        merged = {**payload, "id": payload.get("id") or payload.get("quote_id")}
        wire = self._quote_wire(merged)
        quote_id = wire["quote_id"]
        if not quote_id:
            raise ValueError("durable quote payload has no id")
        updated_time = merged.get("updatedTime") or merged.get("createdTime")
        with self._lock, self._conn:
            cur = self._conn.cursor()
            row = self._get_quote_row(cur, quote_id)
            event = NormalizedEvent(
                event_key=f"durable:{quote_id}", event_type="quote_created",
                rfq_id=wire["rfq_id"], quote_id=quote_id, symbol=wire["symbol"],
                event_at=updated_time or _utcnow_iso(),
                received_at=_utcnow_iso(), payload=merged)
            if row is None:
                self._record_durable_event(
                    cur, f"durable:{quote_id}", "quote_created",
                    wire["rfq_id"], quote_id, wire["symbol"], merged)
                self._upsert_quote(cur, event, wire,
                                   target_status=wire["status"] or "ACTIVE")
                return "inserted"
            if not _is_newer(updated_time, row["updated_time"]):
                return "ignored"
            self._record_durable_event(
                cur, f"durable:{quote_id}:{updated_time}", "quote_created",
                wire["rfq_id"], quote_id, wire["symbol"], merged)
            self._upsert_quote(cur, event, wire,
                               target_status=wire["status"] or row["status"])
            return "refreshed"

    def mark_rfq_closed_by_absence(self, rfq_id: str, now_iso: str) -> bool:
        """Mark a locally-open RFQ CLOSED: it is absent from ``GetRFQs(open)``.

        The durable OPEN snapshot is exchange truth about what is still
        actionable; a locally non-terminal RFQ missing from it must have gone
        terminal while we were away. Returns True if the state changed.
        Terminal local states never regress; legs are untouched (no leg data
        in this inference).
        """
        with self._lock, self._conn:
            cur = self._conn.cursor()
            row = self._get_rfq_row(cur, rfq_id)
            if row is None or row["status"] in RFQ_TERMINAL_STATUSES:
                return False
            if not _is_newer(now_iso, row["updated_time"]):
                return False
            event_id = f"durable:{rfq_id}:absence:{now_iso}"
            self._record_durable_event(
                cur, event_id, "rfq_closed", rfq_id, None, row["symbol"],
                {"id": rfq_id, "status": "CLOSED", "updatedTime": now_iso,
                 "reason": "absent from GetRFQs(open) snapshot"})
            cur.execute(
                "UPDATE rfq SET status = 'CLOSED', updated_time = ?, "
                "last_event_id = ? WHERE rfq_id = ?",
                (now_iso, event_id, rfq_id))
            return True

    def _record_durable_event(self, cur: sqlite3.Cursor, event_id: str,
                              event_type: str, rfq_id: Optional[str],
                              quote_id: Optional[str], symbol: Optional[str],
                              payload: Dict[str, Any]) -> None:
        cur.execute(
            """
            INSERT OR IGNORE INTO raw_events
                (event_id, event_type, rfq_id, quote_id, symbol, event_key,
                 client_derived, payload_json, source, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'durable', ?)
            """,
            (event_id, event_type, rfq_id, quote_id, symbol, event_id,
             json.dumps(payload), _utcnow_iso()),
        )

    # -- books -----------------------------------------------------------------
    def ingest_book(self, symbol: str, bid: Optional[float], ask: Optional[float],
                    bid_size: float = 0.0, ask_size: float = 0.0,
                    seq: int = 0, ts: Optional[str] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO books (symbol, bid, ask, bid_size, ask_size, seq, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    bid=excluded.bid, ask=excluded.ask,
                    bid_size=excluded.bid_size, ask_size=excluded.ask_size,
                    seq=excluded.seq, ts=excluded.ts
                """,
                (symbol, bid, ask, bid_size, ask_size, seq, ts or _utcnow_iso()),
            )

    def get_last_books(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM books WHERE symbol IN "
                f"({','.join('?' for _ in symbols)})",
                symbols,
            ).fetchall() if symbols else []
            return {r["symbol"]: dict(r) for r in rows}

    def get_books(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {r["symbol"]: dict(r)
                    for r in self._conn.execute("SELECT * FROM books").fetchall()}

    def get_book_stats(self) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM books").fetchone()
            return {"symbols": row["n"]}

    # -- shadow decisions ----------------------------------------------------------
    def record_shadow_decision(self, *, rfq_id: str, decision: str,
                               reason: str = "",
                               fair_price: Optional[float] = None,
                               buy_price: Optional[float] = None,
                               sell_price: Optional[float] = None,
                               spread_bps: Optional[float] = None,
                               expected_edge_bps: Optional[float] = None,
                               buy_qty: Optional[str] = None,
                               sell_qty: Optional[str] = None,
                               components_json: Optional[str] = None,
                               ts: Optional[str] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO shadow_decisions
                    (rfq_id, decision, reason, fair_price, buy_price,
                     sell_price, spread_bps, expected_edge_bps, buy_qty,
                     sell_qty, components_json, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rfq_id, decision, reason, fair_price, buy_price,
                 sell_price, spread_bps, expected_edge_bps, buy_qty,
                 sell_qty, components_json, ts or _utcnow_iso()),
            )

    def get_shadow_decisions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shadow_decisions ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_shadow_stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT decision, COUNT(*) AS n FROM shadow_decisions "
                "GROUP BY decision").fetchall()
            return {r["decision"]: r["n"] for r in rows}

    # -- quote latency (live only) ---------------------------------------------
    def record_live_trade(self, rfq_id: str, price: Any, size: Any,
                          executed_at: Optional[str] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO live_trades VALUES (?, ?, ?, ?, ?)",
                (rfq_id, _to_float(price), _to_float(size), executed_at, _utcnow_iso()))

    def update_live_health(self, *, started_at: str, messages_processed: int,
                           errors: int, gateway_connected: bool,
                           buffer_drops: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO live_engine_health VALUES (1, ?, ?, ?, ?, ?, ?)",
                (started_at, _utcnow_iso(), messages_processed, errors,
                 int(gateway_connected), buffer_drops))

    def record_live_latency(self, *, rfq_id: str, posted_at: str,
                            started_at: str, decided_at: str, quoted: bool,
                            budget_ms: float = 400.0,
                            fetch_ms: Optional[float] = None,
                            solve_ms: Optional[float] = None,
                            local_received_at: Optional[str] = None) -> None:
        def millis(value: str) -> float:
            return _parse_ts(value).timestamp() * 1000
        try:
            wait_ms = max(0.0, millis(started_at) - millis(posted_at))
            compute_ms = max(0.0, millis(decided_at) - millis(started_at))
        except (ValueError, TypeError, AttributeError):
            return
        # Split "wait" when we know when this process first saw the frame:
        #   delivery_ms = local receipt - upstream post  (network + clock skew)
        #   queue_ms    = worker start   - local receipt (our own backlog)
        # A fat delivery with a tiny queue means the clock/network, not us.
        delivery_ms = queue_ms = None
        if local_received_at:
            try:
                local = millis(local_received_at)
                delivery_ms = max(0.0, local - millis(posted_at))
                queue_ms = max(0.0, millis(started_at) - local)
            except (ValueError, TypeError, AttributeError):
                delivery_ms = queue_ms = None
        # fetch_ms (book network reads) and solve_ms (joint model) are the two
        # halves of compute; the pricer measures them, so the dashboard can
        # blame a slow network vs a slow solve instead of guessing.
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO quote_latency "
                "(rfq_id, event_type, posted_at, started_at, decided_at, "
                "wait_ms, compute_ms, fetch_ms, solve_ms, delivery_ms, queue_ms, "
                "latency_ms, quoted, over_budget, source) "
                "VALUES (?, 'rfq_created', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live_capture')",
                (rfq_id, posted_at, started_at, decided_at, wait_ms, compute_ms,
                 fetch_ms, solve_ms, delivery_ms, queue_ms, wait_ms + compute_ms,
                 int(quoted), int(wait_ms + compute_ms > budget_ms)))

    def record_quote_latency(self, *, rfq_id: str, event_type: str,
                             posted_at: str, decided_at: str,
                             latency_ms: float, quoted: bool,
                             over_budget: bool, source: str = "live") -> None:
        """One live wall-clock sample: ``decided_at`` minus the RFQ's posted time.

        Live-only (see :mod:`combo_mm.live_monitor`); replay/backtest never
        calls this.
        """
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO quote_latency
                    (rfq_id, event_type, posted_at, decided_at, latency_ms,
                     quoted, over_budget, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rfq_id, event_type, posted_at, decided_at, latency_ms,
                 1 if quoted else 0, 1 if over_budget else 0, source),
            )

    def get_latency_stats(self, limit: int = 5000) -> Dict[str, Any]:
        """Rolling stats over the most recent ``limit`` latency samples."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT latency_ms, over_budget FROM quote_latency "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        if not rows:
            return {"count": 0, "last_ms": None, "p50_ms": None, "p95_ms": None,
                    "max_ms": None, "breaches": 0, "breach_rate": None}
        latencies = sorted(r["latency_ms"] for r in rows)
        n = len(latencies)

        def pct(p: float) -> float:
            return latencies[min(n - 1, int(p * n))]

        breaches = sum(1 for r in rows if r["over_budget"])
        return {
            "count": n,
            "last_ms": rows[0]["latency_ms"],  # rows are newest-first
            "p50_ms": pct(0.50),
            "p95_ms": pct(0.95),
            "max_ms": latencies[-1],
            "breaches": breaches,
            "breach_rate": breaches / n,
        }

    # -- live RFQ screen -------------------------------------------------------
    def upsert_rfq_screen(self, rfq_id: str, *, n_legs: int, n_resolved: int,
                          n_nfl_legs: int, screen: str, rank: int, catalog_version: int,
                          direction: Optional[str] = None, side: Optional[str] = None,
                          condition_id: Optional[str] = None,
                          submission_deadline: Optional[str] = None,
                          checks_json: Optional[str] = None) -> None:
        """Insert an RFQ's screen, or refresh the screen columns of an existing row.

        Gateway extras (direction, deadline, ...) are kept from the first
        insert when a re-screen passes None.
        """
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO rfq_screen (rfq_id, seq, direction, side, condition_id,
                    submission_deadline, n_legs, n_resolved, n_nfl_legs, screen, rank,
                    catalog_version, checks_json)
                VALUES (?1, COALESCE((SELECT rowid FROM rfq WHERE rfq_id = ?1), 0),
                        ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
                ON CONFLICT(rfq_id) DO UPDATE SET
                    direction = COALESCE(excluded.direction, direction),
                    side = COALESCE(excluded.side, side),
                    condition_id = COALESCE(excluded.condition_id, condition_id),
                    submission_deadline = COALESCE(excluded.submission_deadline,
                                                   submission_deadline),
                    n_legs = excluded.n_legs, n_resolved = excluded.n_resolved,
                    n_nfl_legs = excluded.n_nfl_legs, screen = excluded.screen,
                    rank = excluded.rank, catalog_version = excluded.catalog_version,
                    checks_json = COALESCE(excluded.checks_json, checks_json)
                """,
                (rfq_id, direction, side, condition_id, submission_deadline, n_legs,
                 n_resolved, n_nfl_legs, screen, rank, catalog_version, checks_json))

    def unresolved_screen_rfqs(self, catalog_version: int, limit: int = 5000
                               ) -> List[Dict[str, Any]]:
        """RFQs with unresolved legs screened before ``catalog_version``, with leg symbols."""
        with self._lock:
            ids = [r["rfq_id"] for r in self._conn.execute(
                "SELECT rfq_id FROM rfq_screen WHERE n_resolved < n_legs "
                "AND catalog_version < ? LIMIT ?", (catalog_version, limit))]
            out = []
            for rfq_id in ids:
                legs = [r["symbol"] for r in self._conn.execute(
                    "SELECT symbol FROM rfq_legs WHERE rfq_id = ? ORDER BY rowid", (rfq_id,))]
                out.append({"rfq_id": rfq_id, "legs": legs})
            return out

    def get_rfq_screen(self, rfq_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rfq_screen WHERE rfq_id = ?", (rfq_id,)).fetchone()
            return dict(row) if row else None

    # -- read model ----------------------------------------------------------
    def get_rfq(self, rfq_id: str) -> Optional[Dict[str, Any]]:
        """RFQ entity with ordered legs and raw settlement prices."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rfq WHERE rfq_id = ?", (rfq_id,)).fetchone()
            if row is None:
                return None
            legs = self._conn.execute(
                "SELECT symbol, side, settlement_price FROM rfq_legs "
                "WHERE rfq_id = ? ORDER BY rowid", (rfq_id,)).fetchall()
            entity = dict(row)
            entity["legs"] = [dict(l) for l in legs]
            return entity

    def get_rfq_leg_settlements(self, rfq_id: str) -> Dict[str, Optional[float]]:
        """Raw leg settlement prices (never inverted here)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, settlement_price FROM rfq_legs WHERE rfq_id = ?",
                (rfq_id,)).fetchall()
            return {r["symbol"]: r["settlement_price"] for r in rows}

    def get_quote(self, quote_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)).fetchone()
            return dict(row) if row else None

    def get_quotes_for_rfq(self, rfq_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM quotes WHERE rfq_id = ? ORDER BY rowid",
                (rfq_id,)).fetchall()
            return [dict(r) for r in rows]

    def list_quotes(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM quotes ORDER BY rowid").fetchall()
            return [dict(r) for r in rows]

    def list_rfqs(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rfq ORDER BY rowid").fetchall()
            return [dict(r) for r in rows]

    def get_rfq_stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM rfq GROUP BY status"
            ).fetchall()
            # Plain per-status counts. (Previously an aggregate "OPEN" key
            # was merged in, which the per-status "OPEN" count silently
            # overwrote whenever any OPEN row existed.)
            return {r["status"]: r["n"] for r in rows}

    def get_quote_stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM quotes GROUP BY status"
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}

    # -- shadow draft quotes ---------------------------------------------------
    def record_shadow_draft(self, *, quote_id: str, rfq_id: str,
                            symbol: Optional[str] = None,
                            fair: Optional[float] = None,
                            buy_price: float = 0.0,
                            sell_price: float = 0.0,
                            buy_qty: str = "0", sell_qty: str = "0",
                            expected_edge_bps: Optional[float] = None,
                            model_version: Optional[str] = None,
                            params_version: Optional[str] = None,
                            input_snapshot_json: Optional[str] = None,
                            decided_by: Optional[str] = None,
                            decided_at: Optional[str] = None) -> None:
        """Store a paper draft quote. Always ``status='shadow'`` / ``origin='shadow'``.

        The ``quotes`` table's ``origin`` CHECK constraint makes a live row
        unmistakable, and ``QuoteTracker.has_live_quote`` stays False for
        shadow rows (it only matches DRAFT/ACTIVE/REPLACED statuses).
        """
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    """
                    INSERT INTO quotes
                        (quote_id, rfq_id, symbol, status, origin,
                         buy_price, sell_price, buy_qty_decimal, sell_qty_decimal,
                         model_version, params_version, input_snapshot_json,
                         decided_by, created_time, updated_time)
                    VALUES (?, ?, ?, 'shadow', 'shadow', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (quote_id, rfq_id, symbol, buy_price, sell_price,
                     _to_float(buy_qty), _to_float(sell_qty),
                     model_version, params_version, input_snapshot_json,
                     decided_by, decided_at or _utcnow_iso(),
                     decided_at or _utcnow_iso()),
                )
            except sqlite3.IntegrityError:
                # quote_id collision: the draft is already stored (e.g. the
                # same event was processed twice). Treat as a duplicate.
                log.warning("record_shadow_draft: duplicate quote_id %s "
                            "for rfq %s; keeping the stored draft",
                            quote_id, rfq_id)

    def count_shadow_quotes(self, rfq_id: str) -> int:
        """Number of stored shadow drafts for an RFQ (for deterministic ids)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM quotes "
                "WHERE rfq_id = ? AND status = 'shadow'",
                (rfq_id,)).fetchone()
            return int(row["n"])

    def get_shadow_quotes(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Stored shadow drafts, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM quotes WHERE status = 'shadow' "
                "ORDER BY rowid DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(r) for r in rows]

    def inventory_rows(self) -> tuple[list[dict], list[dict], list[dict], bool]:
        """Consistent inputs for a rebuildable inventory snapshot."""
        with self._lock:
            rfqs = [dict(r) for r in self._conn.execute(
                "SELECT r.rfq_id, r.symbol, r.status, r.updated_time, "
                "s.submission_deadline FROM rfq r LEFT JOIN rfq_screen s "
                "ON s.rfq_id=r.rfq_id ORDER BY r.rfq_id")]
            for rfq in rfqs:
                rfq["legs"] = [dict(r) for r in self._conn.execute(
                    "SELECT symbol, side, settlement_price FROM rfq_legs "
                    "WHERE rfq_id=? ORDER BY rowid",
                    (rfq["rfq_id"],))]
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

    def record_risk_event(self, *, ts: str, rfq_id: str, quote_id: str,
                          game_id: str, verdict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO risk_events (ts, rfq_id, quote_id, game_id, action, "
                "reason, detail_json, policy_version) VALUES (?,?,?,?,?,?,?,?)",
                (ts, rfq_id, quote_id, game_id, verdict.action, verdict.reason,
                 json.dumps({"detail": verdict.detail,
                             "flags": verdict.flags,
                             "widen_bps": verdict.widen_bps,
                             "skew_bps": verdict.skew_bps,
                             "exposure_before": verdict.exposure_before,
                             "exposure_after": verdict.exposure_after}, sort_keys=True),
                 "inventory-v1"))

    def record_exposure_snapshot(self, ts: str, source_id: str, snapshot) -> None:
        rows = []
        for game in sorted(snapshot.exposures):
            rows.append((source_id, ts, "game", game,
                         snapshot.pending.get(game, 0),
                         snapshot.executed.get(game, 0),
                         snapshot.exposures[game], snapshot.equity,
                         snapshot.buying_power))
        rows.append((source_id, ts, "portfolio", "ALL",
                     sum(snapshot.pending.values()), sum(snapshot.executed.values()),
                     sum(snapshot.exposures.values()), snapshot.equity,
                     snapshot.buying_power))
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO exposure_snapshots "
                "(source_id,ts,level,key,pending_wcl,executed_wcl,total_wcl,equity,buying_power) "
                "VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def set_kill_switch(self, halted: bool, *, ts: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("a reason is required")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kill_switch_events (ts,state,trigger,detail_json) "
                "VALUES (?,?,?,?)", (ts, "tripped" if halted else "reset",
                                     reason, "{}"))

    def get_fill_stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(qty), 0) AS qty FROM fills"
            ).fetchone()
            return {"fills": rows["n"], "total_qty": rows["qty"]}

    def get_fills_for_position(self, symbol: Optional[str] = None
                               ) -> List[Dict[str, Any]]:
        with self._lock:
            if symbol is None:
                rows = self._conn.execute(
                    "SELECT * FROM fills ORDER BY executed_time").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM fills WHERE symbol = ? ORDER BY executed_time",
                    (symbol,)).fetchall()
            return [dict(r) for r in rows]

    def get_recent_events(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, event_id, event_type, rfq_id, quote_id, symbol,
                       client_derived, source, recorded_at
                FROM raw_events ORDER BY id DESC LIMIT ?
                """,
                (limit,)).fetchall()
            return [dict(r) for r in rows]

    def count_raw_events(self) -> int:
        """Number of rows in the append-only raw event log (incl. no-ops)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM raw_events").fetchone()
            return int(row["n"])

    def state_digest(self) -> str:
        """Byte-for-byte state digest: sha256 over the canonical read model.

        The snapshot covers the entity rows themselves (not just per-status
        counts, which would hide e.g. a wrong price or a swapped RFQ), sorted
        by key and serialized as canonical JSON, so two stores holding the
        same logical state produce the identical 64-char hex digest
        regardless of insertion order. Bookkeeping columns that may carry
        wall-clock or ingest-order values (``updated_time`` set by recovery
        inference, ``last_event_id``, book ``ts``, row ids) are excluded.
        One caveat: ``quotes.created_time`` is included, and it falls back
        to wall-clock time when a shadow draft is recorded without
        ``decided_at``. The engine always passes exchange time
        (``decided_at=event.event_at``), so engine-produced digests are
        deterministic; direct ``record_shadow_draft`` calls without
        ``decided_at`` are not digest-stable across runs.
        """
        def rows(sql: str) -> List[Dict[str, Any]]:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

        with self._lock:
            snapshot = {
                "rfqs": rows(
                    "SELECT rfq_id, symbol, creator_user_id, qty_decimal, "
                    "cash_order_qty, created_time, rest_remainder, status "
                    "FROM rfq ORDER BY rfq_id"),
                "legs": rows(
                    "SELECT rfq_id, symbol, side, settlement_price "
                    "FROM rfq_legs ORDER BY rfq_id, rowid"),
                "quotes": rows(
                    "SELECT quote_id, rfq_id, symbol, maker_user_id, status, "
                    "origin, model_version, params_version, input_snapshot_json, "
                    "buy_price, sell_price, buy_qty_decimal, sell_qty_decimal, "
                    "accepted_side, confirmation_deadline, execution_deadline, "
                    "order_id, client_order_id, created_time "
                    "FROM quotes ORDER BY quote_id"),
                "fills": rows(
                    "SELECT fill_id, rfq_id, quote_id, symbol, side, price, "
                    "qty, executed_time, source, drop_copy_seq "
                    "FROM fills ORDER BY fill_id"),
                "books": rows(
                    "SELECT symbol, bid, ask, bid_size, ask_size, seq "
                    "FROM books ORDER BY symbol"),
                "shadow": rows(
                    "SELECT rfq_id, decision, reason, fair_price, buy_price, "
                    "sell_price, spread_bps, expected_edge_bps, buy_qty, "
                    "sell_qty, components_json, ts "
                    "FROM shadow_decisions ORDER BY rfq_id, ts, id"),
                "drop_copy_token": self.get_drop_copy_token(),
                "risk_events": rows(
                    "SELECT ts, rfq_id, quote_id, game_id, action, reason, "
                    "detail_json, policy_version FROM risk_events "
                    "ORDER BY ts, rfq_id, id"),
                "exposure_snapshots": rows(
                    "SELECT source_id, ts, level, key, pending_wcl, executed_wcl, "
                    "total_wcl, equity, buying_power FROM exposure_snapshots "
                    "ORDER BY source_id, level, key"),
                "kill_switch_events": rows(
                    "SELECT ts, state, trigger, detail_json FROM kill_switch_events "
                    "ORDER BY ts, id"),
            }
        canonical = json.dumps(snapshot, sort_keys=True,
                               separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

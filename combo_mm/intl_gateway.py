"""Live international RFQ adapter (polymarket.com quoter gateway).

RECEIVE-ONLY. This module opens the gateway websocket, authenticates, and
reads the ``RFQ_REQUEST`` / ``RFQ_TRADE`` broadcast feed. It contains no
code path that sends quotes, orders, or any other trading message -- there
is deliberately no client for the quote-submission endpoint anywhere in
this file. ``tests/test_intl_gateway.py`` asserts that absence structurally.

Wire protocol (docs.polymarket.com, "Quoter Gateway"; message shapes
verified against the official ``polymarket-client`` PyPI SDK's
``_internal/rfq.py`` quoter session):

- ``wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq``
- Auth: ``{"type": "auth",
    "auth": {"apiKey", "passphrase", "secret"},
    "identity": {"signer_address", "maker_address", "signature_type": 0}}``
  Server replies ``{"type": "auth", "success": true}``.
- ``RFQ_REQUEST``: ``rfq_id``, ``requestor_public_id``,
  ``leg_position_ids[]``, ``condition_id``, ``yes_position_id``,
  ``no_position_id``, ``direction`` (BUY/SELL), ``side`` (YES/NO),
  ``requested_size`` (``{"unit": "notional"|"shares", "value_e6"}``),
  ``submission_deadline`` (unix ms).
- ``RFQ_TRADE``: ``rfq_id``, ``requester_id``, ``condition_id``,
  ``leg_position_ids[]``, ``direction``, ``side`` -- a confirmed combo
  trade broadcast, mapped to the pipeline's ``rfq_closed`` ("stop quoting").
- ``RFQ_ERROR``: logged, never raised to the caller.

Mapping notes (gateway -> pipeline normalized events):

- ``RFQ_REQUEST`` -> ``rfq_created``. ``requested_size.unit == "notional"``
  becomes ``cashOrderQty``; ``"shares"`` becomes ``qtyDecimal`` (exactly one
  is set, satisfying :func:`combo_mm.normalize.normalize`'s XOR rule).
- The gateway exposes no per-leg market symbol or per-leg side: each leg
  carries its on-chain **position id** as ``symbol`` and inherits the
  combo-level ``side`` (YES/NO). ``symbol`` for the RFQ itself is the
  ``condition_id`` (the combo's on-chain identity); the gateway has no
  ``caoc-...`` style symbol. These choices are marked in the payload and
  documented here so the pricer never mistakes a position id for a market
  symbol.
- ``requestor_public_id`` maps to ``rfqCreatorUserId`` (pseudonymous
  requester, same semantics as the US contract).
- The gateway sends no creation timestamp, so ``exchange_ts`` /
  ``createdTime`` / ``updatedTime`` are the receipt time.
- Gateway-native fields (``direction``, ``condition_id``,
  ``yes/no_position_id``, ``submission_deadline``) ride along as flat
  top-level keys on the raw dict. :func:`combo_mm.normalize.normalize`
  drops unknown keys from the coerced payload by design, so they survive
  only in raw persistence -- downstream readers must not expect them on
  the normalized event.

Credentials come from the runtime environment only
(``POLYMARKET_API_KEY``, ``POLYMARKET_SECRET``, ``POLYMARKET_PASSPHRASE``,
``POLYMARKET_ADDRESS``), optionally via a gitignored local ``.env`` that
never overrides real environment variables. Secrets are never logged,
persisted, or committed -- see README "Live international RFQ feed".
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional

from combo_mm.sources import EventSource

log = logging.getLogger("combo_mm.intl_gateway")

__all__ = [
    "DEFAULT_GATEWAY_URL",
    "GATEWAY_ENV_VARS",
    "GatewayAuthError",
    "GatewayCredentials",
    "InternationalQuoterGatewayAdapter",
    "MappingError",
    "MissingCredentialsError",
    "map_rfq_request",
    "map_rfq_trade",
]

DEFAULT_GATEWAY_URL = "wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq"

#: Environment variables carrying the international API credentials.
GATEWAY_ENV_VARS = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_SECRET",
    "POLYMARKET_PASSPHRASE",
    "POLYMARKET_ADDRESS",
)

_E6 = Decimal(1_000_000)


class MissingCredentialsError(ValueError):
    """Raised when gateway credentials are absent from the environment."""


class MappingError(ValueError):
    """Raised when a gateway frame cannot be mapped to a pipeline event."""


class GatewayAuthError(RuntimeError):
    """Raised when the gateway rejects our auth message."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _e6_to_decimal_str(value_e6: Any, field: str) -> str:
    try:
        return str(Decimal(str(value_e6)) / _E6)
    except (InvalidOperation, ValueError, TypeError, ArithmeticError) as exc:
        raise MappingError(f"bad e6 decimal for {field}: {value_e6!r}") from exc


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` dotenv file (no dependency).

    Blank lines and ``#`` comments are skipped; surrounding single or double
    quotes are stripped. Malformed lines are ignored (never fatal).
    """
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass(frozen=True)
class GatewayCredentials:
    """International quoter-gateway credentials (API triple + wallet)."""

    api_key: str
    api_secret: str
    api_passphrase: str
    wallet_address: str

    def __repr__(self) -> str:  # never leak secrets into logs/tracebacks
        return (
            "GatewayCredentials(api_key=<redacted>, api_secret=<redacted>, "
            f"api_passphrase=<redacted>, wallet_address={self.wallet_address!r})"
        )

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        dotenv_path: Optional[str | Path] = None,
    ) -> "GatewayCredentials":
        """Build from the environment, optionally via a gitignored ``.env``.

        Real environment variables always win; ``.env`` only fills gaps.
        ``dotenv_path`` defaults to ``<repo root>/.env`` (resolved next to
        this package, not the process working directory) when that file
        exists.
        """
        source: Mapping[str, str] = env if env is not None else os.environ
        missing = [v for v in GATEWAY_ENV_VARS if not source.get(v)]
        if missing:
            if dotenv_path is None:
                # Resolve relative to the repo root (this file lives in
                # <root>/combo_mm/), not the process working directory, so
                # the dashboard finds it wherever Streamlit is launched from.
                candidate = Path(__file__).resolve().parents[1] / ".env"
                dotenv_path = candidate if candidate.is_file() else None
            if dotenv_path is not None:
                file_values = _load_dotenv_values(Path(dotenv_path))
                missing = [
                    v
                    for v in missing
                    if not source.get(v) and not file_values.get(v)
                ]
                if not missing:
                    merged = dict(file_values)
                    merged.update(
                        {k: v for k, v in source.items() if k in GATEWAY_ENV_VARS}
                    )
                    source = merged
        if missing:
            raise MissingCredentialsError(
                "live international RFQ feed needs credentials: "
                + ", ".join(missing)
                + ". Set them as environment variables (or in a gitignored "
                ".env file); never commit them."
            )
        return cls(
            api_key=str(source["POLYMARKET_API_KEY"]),
            api_secret=str(source["POLYMARKET_SECRET"]),
            api_passphrase=str(source["POLYMARKET_PASSPHRASE"]),
            wallet_address=str(source["POLYMARKET_ADDRESS"]),
        )


def map_rfq_request(
    frame: Dict[str, Any], *, received_at: Optional[str] = None
) -> Dict[str, Any]:
    """Map a gateway ``RFQ_REQUEST`` frame to a raw ``rfq_created`` dict.

    The returned dict is exactly what :func:`combo_mm.normalize.normalize`
    accepts (``event_type`` / ``rfq_id`` / ``exchange_ts`` envelope plus the
    US-contract wire field names).
    """
    try:
        rfq_id = str(frame["rfq_id"])
        leg_ids = [str(pid) for pid in frame["leg_position_ids"]]
        direction = str(frame["direction"])
        side = str(frame["side"])
        size = frame["requested_size"] or {}
        unit = str(size.get("unit"))
        amount = _e6_to_decimal_str(size.get("value_e6"), "requested_size.value_e6")
    except (KeyError, TypeError) as exc:
        raise MappingError(f"RFQ_REQUEST missing fields: {exc}") from exc
    if direction not in ("BUY", "SELL"):
        raise MappingError(f"RFQ_REQUEST direction must be BUY/SELL, got {direction!r}")
    if side not in ("YES", "NO"):
        raise MappingError(f"RFQ_REQUEST side must be YES/NO, got {side!r}")
    if unit == "notional":
        size_field = {"cashOrderQty": amount}
    elif unit == "shares":
        size_field = {"qtyDecimal": amount}
    else:
        raise MappingError(f"RFQ_REQUEST unknown size unit: {unit!r}")

    ts = received_at or _iso_now()
    raw: Dict[str, Any] = {
        "event_type": "rfq_created",
        "rfq_id": rfq_id,
        "exchange_ts": ts,
        # No caoc-style symbol on the gateway; the combo condition id is the
        # stable on-chain identity.
        "symbol": str(frame.get("condition_id") or rfq_id),
        "id": rfq_id,
        "createdTime": ts,
        "updatedTime": ts,
        "status": "RFQ_STATUS_OPEN",
        # Pseudonymous requester identity, same semantics as rfqCreatorUserId.
        "rfqCreatorUserId": str(frame.get("requestor_public_id") or "unknown"),
        # Legs carry on-chain position ids (not market symbols) and inherit
        # the combo-level side; the gateway provides neither per-leg field.
        "comboLegs": [
            {"symbol": pid, "side": side, "settlementPrice": None}
            for pid in leg_ids
        ],
        # Gateway-native fields: normalize() drops unknown keys from the
        # coerced payload, so these survive only in raw persistence.
        "direction": direction,
        "condition_id": str(frame.get("condition_id") or ""),
        "yes_position_id": str(frame.get("yes_position_id") or ""),
        "no_position_id": str(frame.get("no_position_id") or ""),
        "submission_deadline": str(frame.get("submission_deadline") or ""),
    }
    raw.update(size_field)
    return raw


def map_rfq_trade(
    frame: Dict[str, Any], *, received_at: Optional[str] = None
) -> Dict[str, Any]:
    """Map a gateway ``RFQ_TRADE`` frame to a raw ``rfq_closed`` dict.

    A confirmed trade broadcast is a "stop quoting" signal for the RFQ, the
    same terminal semantics as the US contract's public ``rfq_closed``.
    """
    try:
        rfq_id = str(frame["rfq_id"])
    except KeyError as exc:
        raise MappingError(f"RFQ_TRADE missing rfq_id: {exc}") from exc
    ts = received_at or _iso_now()
    return {
        "event_type": "rfq_closed",
        "rfq_id": rfq_id,
        "exchange_ts": ts,
        "symbol": str(frame.get("condition_id") or rfq_id),
        "id": rfq_id,
        "updatedTime": ts,
        "direction": str(frame.get("direction") or ""),
        "condition_id": str(frame.get("condition_id") or ""),
    }


class InternationalQuoterGatewayAdapter(EventSource):
    """Live ``RFQ_REQUEST``/``RFQ_TRADE`` feed as a pipeline :class:`EventSource`.

    A daemon thread owns the websocket: authenticate once, read frames,
    map them with :func:`map_rfq_request` / :func:`map_rfq_trade`, and buffer
    the raw dicts. :meth:`poll` drains the buffer into the standard
    ``{"kind": "event", "raw": {...}}`` envelope. Reconnect uses exponential
    backoff with jitter; the ``websockets`` ping/pong is the heartbeat, plus
    a silence watchdog that recycles a quiet connection.

    ``poll`` never raises for transport trouble -- the thread owns
    reconnecting -- so a ``PollingConsumer`` keeps its cadence and the
    dashboard can always report :meth:`stats` / :meth:`connected`.
    """

    def __init__(
        self,
        credentials: Optional[GatewayCredentials] = None,
        *,
        url: str = DEFAULT_GATEWAY_URL,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 60.0,
        backoff_jitter: float = 0.25,
        ping_interval_s: float = 20.0,
        silence_watchdog_s: float = 120.0,
        auth_timeout_s: float = 15.0,
        max_buffer: int = 10_000,
        recent_max: int = 200,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._creds = credentials or GatewayCredentials.from_env()
        self._url = url
        self._backoff_initial = backoff_initial_s
        self._backoff_max = backoff_max_s
        self._backoff_jitter = backoff_jitter
        self._ping_interval = ping_interval_s
        self._silence_watchdog = silence_watchdog_s
        self._auth_timeout = auth_timeout_s
        self._max_buffer = max_buffer
        self._rng = rng or random.Random()
        self._buf: Deque[Dict[str, Any]] = collections.deque()
        self._recent: Deque[Dict[str, Any]] = collections.deque(maxlen=recent_max)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake_event: Optional[asyncio.Event] = None
        self._ws: Any = None
        self._stats: Dict[str, Any] = {
            "connects": 0,
            "reconnects": 0,
            "rfqs_seen": 0,
            "trades_seen": 0,
            "frames_dropped": 0,
            "buffer_drops": 0,
            "last_frame_at": None,
            "last_error": None,
        }

    # ------------------------------------------------------------------
    # EventSource
    # ------------------------------------------------------------------
    def poll(self, now: datetime) -> List[Dict[str, Any]]:
        """Drain buffered raw events into the pipeline item envelope."""
        with self._lock:
            raws = [self._buf.popleft() for _ in range(len(self._buf))]
        return [{"kind": "event", "raw": raw} for raw in raws]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> "InternationalQuoterGatewayAdapter":
        """Start the background websocket thread (idempotent)."""
        try:
            import websockets  # noqa: F401  -- optional dependency
        except ImportError as exc:
            raise RuntimeError(
                "the live international feed needs the 'websockets' package: "
                "pip install websockets"
            ) from exc
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="intl-rfq-gateway", daemon=True
        )
        self._thread.start()
        log.info("international quoter-gateway adapter started (receive-only)")
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread to stop and wait for it."""
        self._stop.set()
        loop, wake, ws = self._loop, self._wake_event, self._ws
        if loop is not None:
            if wake is not None:
                try:
                    loop.call_soon_threadsafe(wake.set)  # interrupt backoff sleep
                except RuntimeError:
                    pass  # loop already closed
            if ws is not None:
                try:
                    asyncio.run_coroutine_threadsafe(ws.close(), loop)
                except RuntimeError:
                    pass  # loop already closed
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._connected.clear()
        log.info("international quoter-gateway adapter stopped")

    @property
    def connected(self) -> bool:
        """True while the gateway websocket is authenticated and reading."""
        return self._connected.is_set()

    def stats(self) -> Dict[str, Any]:
        """Snapshot of adapter counters (safe to call from any thread)."""
        with self._lock:
            snap = dict(self._stats)
            snap["buffered"] = len(self._buf)
        return snap

    def recent(self, n: int = 25) -> List[Dict[str, Any]]:
        """Newest-first display summaries of recently seen RFQs/trades."""
        with self._lock:
            return list(self._recent)[-n:][::-1]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _auth_frame(self) -> Dict[str, Any]:
        # Never log this frame: it carries the API secret.
        return {
            "type": "auth",
            "auth": {
                "apiKey": self._creds.api_key,
                "passphrase": self._creds.api_passphrase,
                "secret": self._creds.api_secret,
            },
            "identity": {
                "signer_address": self._creds.wallet_address,
                "maker_address": self._creds.wallet_address,
                "signature_type": 0,
            },
        }

    def _note_error(self, exc: BaseException) -> None:
        # Log the exception class only: source errors must never carry
        # credential material into the logs.
        with self._lock:
            self._stats["last_error"] = type(exc).__name__
        log.warning("quoter gateway error: %s", type(exc).__name__)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        wake = asyncio.Event()
        self._wake_event = wake
        try:
            loop.run_until_complete(self._run_forever(wake))
        except Exception as exc:  # never let the thread die silently
            self._note_error(exc)
        finally:
            self._loop = None
            self._wake_event = None
            asyncio.set_event_loop(None)
            loop.close()

    async def _run_forever(self, wake: asyncio.Event) -> None:
        import websockets

        backoff = self._backoff_initial
        while not self._stop.is_set():
            authed = False
            try:
                authed = await self._session(websockets)
            except GatewayAuthError as exc:
                # Auth rejections back off the same as drops; a rotated key
                # is picked up without a restart once env/.env is updated
                # and the process restarts.
                self._note_error(exc)
            except Exception as exc:
                self._note_error(exc)
            finally:
                self._connected.clear()
                self._ws = None
            if authed:
                # Auth completed this session: it was healthy, so a later
                # drop reconnects promptly instead of at the max backoff.
                # (Auth *failures* keep doubling -- see GatewayAuthError.)
                backoff = self._backoff_initial
            if self._stop.is_set():
                break
            with self._lock:
                self._stats["reconnects"] += 1
            jitter = 1.0 + self._rng.uniform(
                -self._backoff_jitter, self._backoff_jitter
            )
            delay = min(backoff, self._backoff_max) * max(jitter, 0.0)
            log.info("quoter gateway reconnecting in %.1fs", delay)
            try:
                # Interruptible sleep: stop() sets the event to wake us.
                await asyncio.wait_for(wake.wait(), timeout=delay)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            wake.clear()
            backoff = min(backoff * 2.0, self._backoff_max)

    async def _session(self, websockets: Any) -> bool:
        """Run one authenticated read session.

        Returns True when the auth handshake completed (the session was
        healthy up to whatever ended it); False is unreachable -- auth
        failure raises GatewayAuthError.
        """
        async with websockets.connect(
            self._url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_interval,
            max_size=4 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            await ws.send(json.dumps(self._auth_frame()))
            try:
                raw_ack = await asyncio.wait_for(ws.recv(), timeout=self._auth_timeout)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise GatewayAuthError("auth ack timed out") from exc
            try:
                ack = json.loads(raw_ack)
            except json.JSONDecodeError as exc:
                raise GatewayAuthError("auth ack was not JSON") from exc
            if not isinstance(ack, dict) or ack.get("type") != "auth" or ack.get(
                "success"
            ) is not True:
                raise GatewayAuthError(f"auth rejected: {str(ack)[:120]}")
            with self._lock:
                self._stats["connects"] += 1
            self._connected.set()
            log.info("quoter gateway authenticated; streaming RFQ frames")
            while not self._stop.is_set():
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=self._silence_watchdog)
                except (asyncio.TimeoutError, TimeoutError):
                    log.warning("quoter gateway silent too long; recycling connection")
                    return True
                self._on_frame(frame)
            return True  # stopped cleanly after a healthy session

    def _on_frame(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("ignoring non-JSON gateway frame")
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        with self._lock:
            self._stats["last_frame_at"] = time.time()
        if mtype == "RFQ_REQUEST":
            try:
                mapped = map_rfq_request(msg)
            except MappingError as exc:
                with self._lock:
                    self._stats["frames_dropped"] += 1
                log.warning("dropping unmappable RFQ_REQUEST: %s", exc)
                return
            self._emit(mapped, "rfq")
        elif mtype == "RFQ_TRADE":
            try:
                mapped = map_rfq_trade(msg)
            except MappingError as exc:
                with self._lock:
                    self._stats["frames_dropped"] += 1
                log.warning("dropping unmappable RFQ_TRADE: %s", exc)
                return
            self._emit(mapped, "trade")
        elif mtype == "RFQ_ERROR":
            log.warning("gateway RFQ_ERROR: %s", str(msg)[:200])
        elif mtype == "auth":
            log.debug("gateway auth ack (late duplicate)")
        else:
            log.debug("ignoring gateway frame type %r", mtype)

    def _emit(self, raw: Dict[str, Any], kind: str) -> None:
        size = raw.get("cashOrderQty", raw.get("qtyDecimal", "?"))
        summary = {
            "received_at": raw.get("exchange_ts"),
            "kind": kind,
            "rfq_id": raw.get("rfq_id"),
            "symbol": raw.get("symbol"),
            "direction": raw.get("direction"),
            "legs": len(raw.get("comboLegs", [])),
            "size": str(size),
        }
        with self._lock:
            if len(self._buf) >= self._max_buffer:
                self._buf.popleft()
                self._stats["buffer_drops"] += 1
            self._buf.append(raw)
            self._recent.append(summary)
            if kind == "rfq":
                self._stats["rfqs_seen"] += 1
            else:
                self._stats["trades_seen"] += 1

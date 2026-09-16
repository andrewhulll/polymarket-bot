"""Polymarket US **Retail** API adapter: a polling :class:`EventSource`.

The Retail API is REST + API-key auth (``polymarket-us`` SDK); it has **no**
real-time RFQ event stream -- streaming is Exchange (gRPC) only. This module
polls RFQ state, diffs by RFQ id + ``updatedTime``, and emits the pipeline's
existing event model (``rfq_created`` / ``rfq_updated`` / ``rfq_closed``),
plus leg book/BBO snapshots for the existing :class:`LegBookCache`.

Credentials come **only** from the runtime environment::

    POLYMARKET_US_KEY_ID
    POLYMARKET_US_SECRET_KEY

They are never logged, persisted, displayed, or committed. The SDK signs
each request with Ed25519 headers (``X-PM-Access-Key`` / ``X-PM-Timestamp`` /
``X-PM-Signature``); this module never touches the raw secret beyond handing
it to the SDK constructor.

Rate limits (official Retail docs, 2026-09-16):

* 20 requests/second per API key (global). The adapter only performs reads.
* Combo/RFQ *creation* shares a 10-requests-per-10-seconds edge limit -- the
  adapter never creates RFQs or submits quotes, so it does not touch that
  budget. Paper mode is absolute: outbound quotes never call a transport.

Endpoint status (2026-09-16) -- **hand-modeled, verify before trusting**:

* The official ``polymarket-us`` SDK (0.1.2) exposes **no RFQ resource** at
  all (resources: account, events, markets, orders, portfolio, search,
  series, sports). Market data (``client.markets.bbo/book``) is real and
  used as-is.
* The RFQ list/detail paths below are educated guesses routed through the
  SDK's generic authenticated ``get()``. If Andrew confirms the real paths,
  update :attr:`_HandModeledRetailRFQs.LIST_PATH` (or inject a custom
  adapter -- see :class:`RetailPollingSource`). Until then, treat live RFQ
  polling as "best effort against a guessed path" and check
  ``RetailPollingSource.last_error`` after polls.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from combo_mm.auth import CredentialsNotConfigured
from combo_mm.sources import EventSource

log = logging.getLogger("combo_mm.retail")

KEY_ID_ENV = "POLYMARKET_US_KEY_ID"
SECRET_ENV = "POLYMARKET_US_SECRET_KEY"

#: Logged (once per transition) when the Retail RFQ endpoints 403: the RFQ
#: API is beta-gated and this key has not been enabled yet.
RFQ_BETA_MESSAGE = (
    "Retail RFQ beta not enabled for this API key - request access via "
    "support@polymarket.us or the developer portal"
)

# Statuses after which an RFQ is terminal for quoting purposes.
_TERMINAL_STATUSES = {"CANCELLED", "CLOSED", "REJECTED", "DONE_AWAY"}


def _unwrap_list(resp: Any, key: str) -> List[Dict[str, Any]]:
    """Accept a bare list or a ``{key: [...]}`` / ``{data: [...]}`` envelope."""
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        for candidate in (key, "data", "items", "results"):
            items = resp.get(candidate)
            if isinstance(items, list):
                return [r for r in items if isinstance(r, dict)]
    return []


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_amount(value: Any) -> Optional[float]:
    """SDK ``Amount`` is ``{"value": str, "currency": "USD"}``; be liberal."""
    return _coerce_float(value)


def _is_beta_denied(exc: BaseException) -> bool:
    """True if the error is a 403 / permission-denied from an RFQ endpoint.

    The Retail RFQ endpoints are beta-gated ("available only to explicitly
    enabled Retail API users"), so a key without enablement gets 403 here.
    This is an expected capability state, not a transient failure: callers
    must degrade (simulated RFQ events + retail books), never retry-loop.

    Detection is liberal on purpose: the real SDK raises
    ``polymarket_us.errors.PermissionDeniedError`` (``status_code == 403``),
    hand-rolled adapters may raise their own 403 types, and mocks in tests
    only need to look 403-ish. The exception message is inspected but never
    logged (credential hygiene).
    """
    if getattr(exc, "status_code", None) == 403:
        return True
    if getattr(exc, "status", None) == 403:
        return True
    name = type(exc).__name__
    if "PermissionDenied" in name or "Forbidden" in name:
        return True
    return "403" in str(exc)


class _HandModeledRetailRFQs:
    """RFQ list/detail over Retail REST, routed via the SDK's authed ``get()``.

    TODO (Andrew): verify the real RFQ list/detail paths against the Retail
    docs / API team. The SDK has no RFQ resource, so these are guesses.
    Swap in a duck-typed ``list()`` / ``retrieve(rfq_id)`` object via the
    ``client=`` injection if the paths differ.
    """

    LIST_PATH = "/v1/rfqs"

    def __init__(self, get) -> None:
        self._get = get

    def list(self) -> List[Dict[str, Any]]:
        resp = self._get(self.LIST_PATH, authenticated=True)
        return _unwrap_list(resp, "rfqs")

    def retrieve(self, rfq_id: str) -> Dict[str, Any]:
        resp = self._get(f"{self.LIST_PATH}/{rfq_id}", authenticated=True)
        if isinstance(resp, dict):
            for candidate in ("rfq", "data"):
                inner = resp.get(candidate)
                if isinstance(inner, dict):
                    return inner
            return resp
        return {}


class RetailPollingSource(EventSource):
    """Poll the Retail REST API and emit pipeline items.

    Per poll: one RFQ list call, then targeted detail calls (only when the
    list row lacks leg/size fields), then BBO calls for leg symbols -- all
    capped by ``max_requests_per_poll``. Emitted items:

    * ``{"kind": "event", "raw": {...}}`` -- ``rfq_created`` (new RFQ),
      ``rfq_updated`` (newer ``updatedTime``), ``rfq_closed`` (terminal
      status, or disappeared from the listing). Disappearance-derived
      closures carry ``"client_derived": True`` on the item envelope, as do
      ``rfq_expired`` events for ``EXPIRED`` statuses (expiry is always a
      client inference per the event contract).
    * ``{"kind": "book", ...}`` -- per-leg BBO snapshots with ``ts`` set to
      the fetch time, for the existing leg book cache.

    No private quote events exist on Retail, so the quote lifecycle stays
    paper/simulated in live mode.

    **RFQ beta gate.** The Retail RFQ endpoints are beta-gated ("available
    only to explicitly enabled Retail API users"): a key without enablement
    gets 403 on ``/v1/rfqs*``. That 403 is treated as a *runtime capability
    flag* (``rfq_beta_enabled``), re-checked on every poll -- a 403 flips it
    off, a later successful read flips it back on, so enablement is picked
    up without a restart. While the flag is off:

    * RFQ events fall back to ``fallback_rfq_source`` (a simulated feed) --
      the pipeline keeps running instead of crashing or retry-looping;
    * leg market data (book/BBO) is **not** beta-gated, so retail BBO
      refresh continues (mixed mode: simulated RFQ events + live books);
    * the 403 is never raised to the consumer and never touches the
      retry/backoff budget -- it is an expected capability state.
    """

    def __init__(self, *, client: Any = None, poll_interval_s: float = 5.0,
                 max_requests_per_poll: int = 10,
                 fallback_rfq_source: Optional[EventSource] = None) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        if max_requests_per_poll < 1:
            raise ValueError("max_requests_per_poll must be >= 1")
        if client is None:
            key_id, secret_key = self._credentials_from_env()
            sdk_client = self._build_sdk_client(key_id, secret_key)
            rfqs = _HandModeledRetailRFQs(sdk_client.get)
            markets = sdk_client.markets
        else:
            rfqs = getattr(client, "rfqs", None)
            if rfqs is None and hasattr(client, "get"):
                rfqs = _HandModeledRetailRFQs(client.get)
            if rfqs is None:
                raise TypeError(
                    "injected client must expose .rfqs (list/retrieve) or .get")
            markets = client.markets
        self._rfqs = rfqs
        self._markets = markets
        self.poll_interval_s = poll_interval_s
        self.max_requests_per_poll = max_requests_per_poll
        # Simulated RFQ-event feed used while the RFQ beta is disabled.
        # Only its ``kind == "event"`` items are emitted; its book
        # snapshots are skipped so live retail books are never clobbered.
        self._fallback_rfq_source = fallback_rfq_source
        # Runtime capability flag for the beta-gated RFQ endpoints.
        # Reset to True (unknown) at the top of every poll: a 403 flips it
        # off, a successful read leaves it on.
        self.rfq_beta_enabled = True
        # rfq_id -> {"status", "updated_time", "legs": [symbols], "terminal"}
        self._seen: Dict[str, Dict[str, Any]] = {}
        self._pending_books: Deque[str] = deque()
        self.polls = 0
        self.last_request_count = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ #
    # credentials / SDK (only touched on the live path, never in tests)   #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _credentials_from_env():
        key_id = os.environ.get(KEY_ID_ENV)
        secret_key = os.environ.get(SECRET_ENV)
        if not key_id or not secret_key:
            raise CredentialsNotConfigured(
                f"Retail live mode needs {KEY_ID_ENV} and {SECRET_ENV} in the "
                "runtime environment. They are never stored, logged, or "
                "displayed -- set them in your shell before starting.")
        return key_id, secret_key

    @staticmethod
    def _build_sdk_client(key_id: str, secret_key: str):
        try:
            from polymarket_us import PolymarketUS
        except ImportError as exc:
            raise RuntimeError(
                "The 'polymarket-us' SDK is not installed. Install it with "
                "`pip install polymarket-us` (optional dependency -- the "
                "simulated pipeline and tests do not need it).") from exc
        # The SDK holds the secret only in memory to sign requests; we never
        # log or persist it.
        return PolymarketUS(key_id=key_id, secret_key=secret_key, timeout=30.0)

    def __repr__(self) -> str:  # never include credential material
        return (f"RetailPollingSource(poll_interval_s={self.poll_interval_s}, "
                f"max_requests_per_poll={self.max_requests_per_poll}, "
                f"tracked={len(self._seen)}, "
                f"rfq_beta_enabled={self.rfq_beta_enabled})")

    # ------------------------------------------------------------------ #
    # polling                                                             #
    # ------------------------------------------------------------------ #
    def poll(self, now: datetime) -> List[Dict[str, Any]]:
        now_iso = now.astimezone(timezone.utc).isoformat()
        self._requests = 0
        items: List[Dict[str, Any]] = []
        # The RFQ beta gate is a runtime capability, re-checked every poll:
        # reset to True (unknown); a 403 flips it off, a successful read
        # leaves it on -- enablement is picked up without a restart.
        was_enabled = self.rfq_beta_enabled
        self.rfq_beta_enabled = True
        try:
            listings = self._list_rfqs()
        except Exception as exc:
            if _is_beta_denied(exc):
                if was_enabled:
                    log.error(RFQ_BETA_MESSAGE)
                else:
                    log.debug("retail RFQ beta still not enabled; serving "
                              "simulated RFQ events + retail books")
                return self._degraded_poll(now, now_iso, items)
            # Log the failure class only: messages/paths must never carry
            # credential material.
            self.last_error = type(exc).__name__
            log.warning("retail RFQ list failed (%s)", self.last_error)
            raise
        live_ids = set()
        for entry in listings:
            try:
                fields = self._rfq_fields(entry)
            except _UnmappableRFQ as exc:
                log.warning("skipping unmappable retail RFQ: %s", exc)
                continue
            rid = fields["rfq_id"]
            live_ids.add(rid)
            prev = self._seen.get(rid)
            if prev is None:
                items.extend(self._handle_new_rfq(fields, now_iso))
            elif self._is_newer(fields, prev):
                items.extend(self._handle_changed_rfq(fields, prev, now_iso))
        for rid, prev in list(self._seen.items()):
            if rid not in live_ids and not prev.get("terminal"):
                # Absent from a full listing => treat like the durable
                # GetRFQs(open) absence reconciliation: stop quoting.
                items.append(self._raw_item(
                    "rfq_closed", prev["fields"], now_iso,
                    client_derived=True,
                    status=prev["fields"]["status"]))
                prev["terminal"] = True
                log.info("retail RFQ %s disappeared from listing; "
                         "emitting client-derived rfq_closed", rid)
        items.extend(self._refresh_books(now_iso))
        self.polls += 1
        self.last_request_count = self._requests
        self.last_error = None
        return items

    def _degraded_poll(self, now: datetime, now_iso: str,
                       items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Serve a poll while the RFQ beta is disabled for this key.

        A 403 on ``/v1/rfqs*`` is an expected capability state, not a
        transient failure: do NOT raise (the consumer would back off and
        retry-loop) and do NOT count it against any retry/backoff budget.
        RFQ events come from the fallback simulated feed; leg market data
        is not beta-gated, so retail BBO refresh continues -- mixed mode.
        """
        self.rfq_beta_enabled = False
        self.last_error = "rfq_beta_not_enabled"
        if self._fallback_rfq_source is not None:
            for fb_item in self._fallback_rfq_source.poll(now):
                # RFQ/quote events only: the simulated feed's book snapshots
                # must not clobber live retail books.
                if fb_item.get("kind") == "event":
                    items.append(fb_item)
        else:
            log.warning("retail RFQ beta not enabled and no fallback RFQ "
                        "source configured; emitting no RFQ events this poll")
        items.extend(self._refresh_books(now_iso))
        self.polls += 1
        self.last_request_count = self._requests
        return items

    # -- request budget -------------------------------------------------- #
    def _spend(self) -> bool:
        if self._requests >= self.max_requests_per_poll:
            return False
        self._requests += 1
        return True

    def _list_rfqs(self) -> List[Dict[str, Any]]:
        # The list call is mandatory; the budget guards the follow-ups.
        # Unwrap here (idempotent): the hand-modeled shim already unwraps,
        # but injected adapters may return the raw ``{"rfqs": [...]}``
        # envelope.
        self._requests += 1
        return _unwrap_list(self._rfqs.list(), "rfqs")

    # -- RFQ mapping ------------------------------------------------------ #
    def _rfq_fields(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        rid = str(entry.get("id") or entry.get("rfqId") or "").strip()
        if not rid:
            raise _UnmappableRFQ("RFQ entry has no id")
        status = str(entry.get("status") or "UNKNOWN").upper()
        updated = (entry.get("updatedTime") or entry.get("updated_time")
                   or entry.get("updatedAt") or entry.get("createdTime"))
        legs = self._leg_entries(entry.get("legs"))
        return {
            "rfq_id": rid,
            "symbol": entry.get("symbol"),
            "status": status,
            "updated_time": str(updated) if updated else None,
            "legs": legs,
            "qty_decimal": entry.get("qtyDecimal"),
            "cash_order_qty": entry.get("cashOrderQty"),
            "settlement_price": _coerce_float(entry.get("settlementPrice")),
            "raw": entry,
        }

    @staticmethod
    def _leg_entries(legs: Any) -> List[Dict[str, Optional[str]]]:
        """Coerce leg rows to ``[{symbol, side}]``; side is None when unknown.

        The wire contract accepts only YES/NO sides, so ``comboLegs`` omits
        legs whose side is unknown (they still get book refreshes).
        """
        out: List[Dict[str, Optional[str]]] = []
        if isinstance(legs, list):
            for leg in legs:
                if isinstance(leg, dict):
                    sym = leg.get("symbol") or leg.get("marketSlug")
                    side = leg.get("side")
                    side = side if side in ("YES", "NO") else None
                else:
                    sym, side = leg, None
                if sym and all(e["symbol"] != str(sym) for e in out):
                    out.append({"symbol": str(sym), "side": side})
        return out

    @staticmethod
    def _leg_symbols(legs: List[Dict[str, Optional[str]]]) -> List[str]:
        return [e["symbol"] for e in legs]

    @staticmethod
    def _is_newer(fields: Dict[str, Any],
                  prev: Dict[str, Any]) -> bool:
        if fields["status"] != prev["status"]:
            return True
        new_ts, old_ts = fields["updated_time"], prev["updated_time"]
        if not new_ts or new_ts == old_ts:
            return False
        if old_ts is None:
            return True
        # Parsed comparison: mixed ISO precisions (…01Z vs …01.100000Z)
        # do not order correctly as raw strings.
        new_dt, old_dt = _parse_ts(new_ts), _parse_ts(old_ts)
        if new_dt is not None and old_dt is not None:
            return new_dt > old_dt
        return new_ts > old_ts

    def _handle_new_rfq(self, fields: Dict[str, Any],
                        now_iso: str) -> List[Dict[str, Any]]:
        rid = fields["rfq_id"]
        if not fields["legs"] and self._spend():
            # The list row may be sparse; the detail read is the targeted
            # follow-up that fills legs/size before we judge the RFQ.
            try:
                detail = self._rfqs.retrieve(rid)
            except Exception as exc:
                if _is_beta_denied(exc):
                    # Same beta gate on the detail endpoint: flip the flag
                    # (the next poll takes the degraded path) and continue
                    # this poll without detail enrichment.
                    if self.rfq_beta_enabled:
                        log.error(RFQ_BETA_MESSAGE)
                    self.rfq_beta_enabled = False
                    self.last_error = "rfq_beta_not_enabled"
                    detail = {}
                else:
                    raise
            if isinstance(detail, dict):
                fields["legs"] = (self._leg_entries(detail.get("legs"))
                                  or fields["legs"])
                for camel, key in (("qtyDecimal", "qty_decimal"),
                                   ("cashOrderQty", "cash_order_qty"),
                                   ("settlementPrice", "settlement_price"),
                                   ("symbol", "symbol"),
                                   ("status", "status"),
                                   ("updatedTime", "updated_time")):
                    if (fields[key] in (None, "", [])
                            and detail.get(camel) not in (None, "")):
                        fields[key] = detail.get(camel)
                fields["status"] = str(fields["status"]).upper()
                fields["settlement_price"] = _coerce_float(
                    fields["settlement_price"])
        if (fields["status"] in _TERMINAL_STATUSES
                or fields["status"] == "EXPIRED"):
            # Never actionable; remember it so we don't re-log every poll.
            self._seen[rid] = {"status": fields["status"],
                               "updated_time": fields["updated_time"],
                               "legs": fields["legs"],
                               "terminal": True, "fields": fields}
            return []
        if not fields["qty_decimal"] and not fields["cash_order_qty"]:
            log.warning("retail RFQ %s has no size field (qtyDecimal/ "
                        "cashOrderQty); skipping until the mapping is fixed",
                        rid)
            self._seen[rid] = {"status": fields["status"],
                               "updated_time": fields["updated_time"],
                               "legs": fields["legs"],
                               "terminal": False, "fields": fields,
                               "unmappable": True}
            return []
        self._seen[rid] = {"status": fields["status"],
                           "updated_time": fields["updated_time"],
                           "legs": fields["legs"],
                           "terminal": False, "fields": fields}
        self._prioritize_books(self._leg_symbols(fields["legs"]))
        return [self._raw_item("rfq_created", fields, now_iso)]

    def _handle_changed_rfq(self, fields: Dict[str, Any],
                            prev: Dict[str, Any],
                            now_iso: str) -> List[Dict[str, Any]]:
        rid = fields["rfq_id"]
        if prev.get("unmappable"):
            return []
        prev.update(status=fields["status"],
                    updated_time=fields["updated_time"],
                    legs=fields["legs"] or prev["legs"],
                    fields=fields)
        self._prioritize_books(self._leg_symbols(prev["legs"]))
        if fields["status"] == "EXPIRED":
            prev["terminal"] = True
            return [self._raw_item("rfq_expired", fields, now_iso,
                                    client_derived=True)]
        if fields["status"] in _TERMINAL_STATUSES:
            prev["terminal"] = True
            return [self._raw_item("rfq_closed", fields, now_iso)]
        return [self._raw_item("rfq_updated", fields, now_iso)]

    def _raw_item(self, event_type: str, fields: Dict[str, Any],
                  now_iso: str, *, client_derived: bool = False,
                  status: Optional[str] = None) -> Dict[str, Any]:
        rid = fields["rfq_id"]
        payload: Dict[str, Any] = {
            "id": rid,
            "symbol": fields["symbol"],
            "status": status or fields["status"],
            "updatedTime": fields["updated_time"] or now_iso,
            "comboLegs": [{"symbol": e["symbol"], "side": e["side"]}
                          for e in fields["legs"] if e["side"]],
        }
        if fields["qty_decimal"]:
            payload["qtyDecimal"] = str(fields["qty_decimal"])
        if fields["cash_order_qty"]:
            payload["cashOrderQty"] = str(fields["cash_order_qty"])
        if fields["settlement_price"] is not None:
            payload["settlementPrice"] = str(fields["settlement_price"])
        return {
            "kind": "event",
            "client_derived": client_derived,
            "raw": {
                "event_type": event_type,
                # Deterministic: re-polls of the same change dedupe in store.
                "event_id": f"retail:{rid}:{event_type}:"
                            f"{fields['updated_time'] or now_iso}",
                "rfq_id": rid,
                "exchange_ts": fields["updated_time"] or now_iso,
                "payload": payload,
            },
        }

    # -- books ------------------------------------------------------------ #
    def _prioritize_books(self, symbols: List[str]) -> None:
        for sym in symbols:
            if sym in self._pending_books:
                self._pending_books.remove(sym)
            self._pending_books.appendleft(sym)

    def _refresh_books(self, now_iso: str) -> List[Dict[str, Any]]:
        # Changed/new RFQ legs jump the queue; the rest round-robin over the
        # legs of open RFQs so books stay fresh within the request budget.
        for rid, prev in self._seen.items():
            if not prev.get("terminal"):
                for sym in self._leg_symbols(prev["legs"]):
                    if sym not in self._pending_books:
                        self._pending_books.append(sym)
        items: List[Dict[str, Any]] = []
        while self._pending_books and self._spend():
            symbol = self._pending_books.popleft()
            try:
                resp = self._markets.bbo(symbol)
            except Exception as exc:
                log.warning("retail BBO failed for %s (%s)",
                            symbol, type(exc).__name__)
                continue
            book = self._parse_bbo(symbol, resp, now_iso)
            if book is not None:
                items.append(book)
        return items

    @staticmethod
    def _parse_bbo(symbol: str, resp: Any,
                   now_iso: str) -> Optional[Dict[str, Any]]:
        # The raw REST shape nests under "marketData"; the SDK types are flat.
        data = resp.get("marketData", resp) if isinstance(resp, dict) else {}
        if not isinstance(data, dict):
            return None
        bid = _coerce_amount(data.get("bestBid"))
        ask = _coerce_amount(data.get("bestAsk"))
        if bid is None or ask is None:
            log.warning("retail BBO for %s missing bestBid/bestAsk; skipping",
                        symbol)
            return None
        return {
            "kind": "book",
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "bid_size": _coerce_float(data.get("bidDepth")) or 0.0,
            "ask_size": _coerce_float(data.get("askDepth")) or 0.0,
            "seq": 0,
            "ts": now_iso,  # fetch time; staleness flags apply as usual
        }


def _parse_ts(value: Any) -> Optional[datetime]:
    """Best-effort ISO-8601 parse for ``updatedTime`` comparisons."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class _UnmappableRFQ(ValueError):
    """A retail RFQ row lacked the fields needed for the event model."""


__all__ = [
    "RetailPollingSource",
    "KEY_ID_ENV",
    "SECRET_ENV",
    "RFQ_BETA_MESSAGE",
]

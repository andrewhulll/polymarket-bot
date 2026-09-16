"""Validation + coercion of raw exchange messages into :class:`NormalizedEvent`.

``normalize(raw)`` is pure except for the default ingest timestamp. It never
touches the network or the database.

Wire contract (``polymarket.v1`` per https://docs.polymarket.us/grpc-api/overview):

- The stream envelope is modeled as ``{event_id?, event_type, exchange_ts,
  rfq_id, quote_id?, symbol?, payload}``. Real stream messages carry no
  ``event_id`` (dedup then relies on the ``event_key`` fallback + per-entity
  ``updatedTime`` monotonic apply in the store); ``exchange_ts`` stands in for
  the wire ``createdTime``/``updatedTime`` when the raw message already
  carries it, otherwise it is derived from the payload's ``updatedTime``
  (preferred) or ``createdTime``.
- Payloads use the EXACT wire field names (``buyPrice``, ``qtyDecimal``,
  ``comboLegs`` ...); coercion only validates types and normalizes
  timestamps/decimals.
- ``settlementPrice`` is stored RAW (the YES/LONG result in [0,1]) -- it is
  NEVER inverted for SELL-side legs at ingest. ``"0"`` is a valid settled
  price; an absent field means no valid settlement. The combo-side mapping
  (``q_i``) happens in the pricer, not here.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from combo_mm.events import (
    ALL_EVENT_TYPES,
    NormalizedEvent,
    deterministic_quote_id,
)

__all__ = ["NormalizeError", "normalize"]

_RESERVED_TOP_LEVEL = {
    "event_id",
    "event_type",
    "rfq_id",
    "quote_id",
    "exchange_ts",
    "symbol",
    "payload",
}


class NormalizeError(ValueError):
    """Raised when a raw message fails validation."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_ts(value: Any) -> str:
    """Accept ISO-8601 strings or epoch (ms if large, else seconds); -> ISO UTC."""
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if abs(value) > 1e12 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise NormalizeError("empty exchange timestamp")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NormalizeError(f"unparseable exchange timestamp: {value!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise NormalizeError(f"unsupported exchange timestamp type: {type(value).__name__}")


def _coerce_decimal(value: Any, field_name: str) -> str:
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError) as exc:
        raise NormalizeError(f"bad decimal for {field_name}: {value!r}") from exc


def _coerce_bool_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and value in (0, 1):
        return int(value)
    if isinstance(value, str) and value.lower() in ("true", "false", "0", "1"):
        return int(value.lower() in ("true", "1"))
    raise NormalizeError(f"bad boolean for {field_name}: {value!r}")


def _get(raw: Dict[str, Any], payload: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
        if name in payload:
            return payload[name]
    return None


def _normalize_legs(value: Any) -> list:
    """Coerce comboLegs[] -> [{symbol, side, settlementPrice|None}].

    ``settlementPrice`` is kept RAW (never inverted); None means unsettled.
    """
    if value is None:
        return []  # missing legs are legal; ReferenceCache supplies a fallback
    if not isinstance(value, list):
        raise NormalizeError(f"legs must be a list, got {type(value).__name__}")
    legs = []
    for i, leg in enumerate(value):
        if not isinstance(leg, dict):
            raise NormalizeError(f"leg {i} must be a dict")
        symbol = leg.get("symbol")
        if not symbol:
            raise NormalizeError(f"leg {i} missing symbol")
        side = leg.get("side")
        # MODELING: the contract does not enumerate leg side values; we accept
        # YES/NO (used by our sim and the design doc).
        if side not in ("YES", "NO"):
            raise NormalizeError(f"leg {i} side must be YES/NO, got {side!r}")
        settle = leg.get("settlementPrice", leg.get("settlement_price"))
        if settle is not None:
            settle = _coerce_decimal(settle, f"legs[{i}].settlementPrice")
            if not (Decimal("0") <= Decimal(settle) <= Decimal("1")):
                raise NormalizeError(
                    f"legs[{i}].settlementPrice out of [0,1]: {settle!r}"
                )
        legs.append({"symbol": str(symbol), "side": side, "settlementPrice": settle})
    return legs


def _event_key(event_id: Optional[str], event_type: str, rfq_id: Optional[str],
               quote_id: Optional[str], event_at: str) -> str:
    if event_id:
        return str(event_id)
    basis = "|".join([event_type, rfq_id or "", quote_id or "", event_at])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def normalize(raw: Dict[str, Any], *, now: Optional[datetime] = None) -> NormalizedEvent:
    """Validate and coerce a raw exchange message.

    :param raw: raw message dict. Known top-level keys are ``event_id``,
        ``event_type``, ``rfq_id``, ``quote_id``, ``exchange_ts``, ``symbol``,
        plus an optional nested ``payload`` dict; everything else is treated
        as payload.
    :param now: ingest timestamp override (tests / deterministic replay);
        defaults to current UTC time.
    :raises NormalizeError: on any validation failure.
    """
    if not isinstance(raw, dict):
        raise NormalizeError(f"raw event must be a dict, got {type(raw).__name__}")

    event_type = raw.get("event_type")
    if event_type not in ALL_EVENT_TYPES:
        raise NormalizeError(f"unknown event_type: {event_type!r}")

    nested = raw.get("payload") or {}
    if not isinstance(nested, dict):
        raise NormalizeError("payload must be a dict")
    # Flat unknown top-level keys merge into the payload.
    payload: Dict[str, Any] = {
        k: v for k, v in raw.items() if k not in _RESERVED_TOP_LEVEL
    }
    payload.update(nested)

    rfq_id = raw.get("rfq_id") or payload.get("rfqId") or payload.get("id")
    if not rfq_id:
        raise NormalizeError(f"{event_type} requires rfq_id")
    rfq_id = str(rfq_id)

    # Exchange time: explicit exchange_ts (our envelope), else the wire
    # updatedTime (preferred) / createdTime.
    ts_raw = raw.get("exchange_ts")
    if ts_raw is None:
        ts_raw = payload.get("updatedTime") or payload.get("createdTime")
    if ts_raw is None:
        raise NormalizeError(f"{event_type} requires exchange_ts/updatedTime/createdTime")
    event_at = _coerce_ts(ts_raw)

    received_at = (
        now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if now is not None
        else _utcnow_iso()
    )

    symbol = raw.get("symbol") or payload.get("symbol")
    symbol = str(symbol) if symbol is not None else None

    coerced: Dict[str, Any] = {}

    # --- RFQ payload fields (exact wire names) --------------------------------
    if event_type in ("rfq_created", "rfq_updated", "rfq_cancelled",
                      "rfq_expired", "rfq_closed"):
        qty = _get(raw, payload, "qtyDecimal")
        cash = _get(raw, payload, "cashOrderQty")
        if event_type == "rfq_created":
            # qtyDecimal XOR cashOrderQty: exactly one must be present.
            if (qty is None) == (cash is None):
                raise NormalizeError(
                    "rfq_created requires exactly one of qtyDecimal / cashOrderQty"
                )
        if qty is not None:
            coerced["qtyDecimal"] = _coerce_decimal(qty, "qtyDecimal")
        if cash is not None:
            coerced["cashOrderQty"] = _coerce_decimal(cash, "cashOrderQty")
        creator = _get(raw, payload, "rfqCreatorUserId")
        if creator is not None:
            coerced["rfqCreatorUserId"] = str(creator)
        rest = _get(raw, payload, "restRemainder")
        if rest is not None:
            coerced["restRemainder"] = _coerce_bool_int(rest, "restRemainder")
        coerced["comboLegs"] = _normalize_legs(_get(raw, payload, "comboLegs"))
        status = _get(raw, payload, "status")
        if status is not None:
            coerced["status"] = str(status)
        for key in ("id", "symbol", "createdTime", "updatedTime"):
            value = _get(raw, payload, key)
            if value is not None:
                coerced[key] = str(value)

    # --- Quote payload fields (exact wire names) ------------------------------
    if event_type in ("quote_draft_revised", "quote_created", "quote_deleted",
                      "quote_accepted", "quote_confirmed", "quote_executed"):
        maker = _get(raw, payload, "creatorRfqUserId")
        quote_id = raw.get("quote_id") or payload.get("id")
        if quote_id is None:
            if maker is None or rfq_id is None:
                raise NormalizeError(
                    f"{event_type} needs quote id or (creatorRfqUserId + rfq_id)"
                )
            quote_id = deterministic_quote_id(str(maker), rfq_id)
        quote_id = str(quote_id)
        if maker is not None:
            coerced["creatorRfqUserId"] = str(maker)
        for key in ("buyPrice", "sellPrice"):
            value = _get(raw, payload, key)
            if value is not None:
                try:
                    coerced[key] = float(value)
                except (TypeError, ValueError) as exc:
                    raise NormalizeError(f"bad decimal for {key}: {value!r}") from exc
        for key in ("buyQtyDecimal", "sellQtyDecimal", "rfqCashOrderQty"):
            value = _get(raw, payload, key)
            if value is not None:
                coerced[key] = _coerce_decimal(value, key)
        for key in ("restRemainder", "postOnly"):
            value = _get(raw, payload, key)
            if value is not None:
                coerced[key] = _coerce_bool_int(value, key)
        for key in ("rfqCreatorUserId", "symbol", "status", "createdTime",
                    "updatedTime", "acceptedSide", "acceptedTime",
                    "confirmationDeadline", "confirmedTime",
                    "executionDeadline", "orderId", "clientOrderId",
                    "executedTime"):
            value = _get(raw, payload, key)
            if value is not None:
                coerced[key] = str(value)
    else:
        quote_id = raw.get("quote_id") or payload.get("id") or payload.get("quoteId")
        quote_id = str(quote_id) if quote_id is not None else None

    # --- Drop Copy fill fields (modeled shape; see combo_mm.dropcopy) ---------
    if event_type == "drop_copy_fill":
        side = _get(raw, payload, "side")
        if side not in ("BUY", "SELL"):
            raise NormalizeError(f"drop_copy_fill side must be BUY/SELL, got {side!r}")
        coerced["side"] = side
        price = _get(raw, payload, "price")
        if price is None:
            raise NormalizeError("drop_copy_fill requires price")
        try:
            coerced["price"] = float(price)
        except (TypeError, ValueError) as exc:
            raise NormalizeError(f"bad decimal for price: {price!r}") from exc
        qty_f = _get(raw, payload, "qty", "quantity")
        if qty_f is None:
            raise NormalizeError("drop_copy_fill requires qty")
        coerced["qty"] = _coerce_decimal(qty_f, "qty")
        seq = _get(raw, payload, "dropCopySeq")
        if seq is not None:
            coerced["dropCopySeq"] = str(seq)
        fill_id = _get(raw, payload, "fillId")
        coerced["fillId"] = str(fill_id) if fill_id is not None else None
        exec_time = _get(raw, payload, "executedTime")
        if exec_time is not None:
            coerced["executedTime"] = _coerce_ts(exec_time)

    key = _event_key(
        raw.get("event_id"), event_type, rfq_id, quote_id, event_at
    )
    return NormalizedEvent(
        event_key=key,
        event_type=event_type,
        rfq_id=rfq_id,
        quote_id=quote_id,
        symbol=symbol,
        event_at=event_at,
        received_at=received_at,
        payload=coerced,
    )

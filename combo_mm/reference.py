"""Combo reference metadata cache.

Lazy-loads per-symbol combo metadata (tick size, price limits, min quantity,
fallback leg list for RFQs that arrive with no inline legs) from the
transport's durable ``get_combos`` read, with a TTL. This is control-plane
(not hot-path): transport calls happen only on miss/expiry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from combo_mm.stream import RfqTransport

__all__ = ["ComboMeta", "ReferenceCache"]


@dataclass(frozen=True)
class ComboMeta:
    symbol: str
    tick_size: float = 0.001
    price_min: float = 0.001
    price_max: float = 0.999
    min_qty: float = 1.0
    legs: List[Dict[str, Any]] = field(default_factory=list)


class ReferenceCache:
    """TTL-guarded lazy cache over ``transport.get_combos``."""

    def __init__(self, transport: RfqTransport, ttl_s: float = 300.0) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._transport = transport
        self._ttl_s = ttl_s
        self._cache: Dict[str, tuple] = {}  # symbol -> (ComboMeta, loaded_monotonic)

    def _load(self, symbol: Optional[str] = None) -> None:
        now = time.monotonic()
        for combo in self._transport.get_combos(symbol=symbol):
            sym = combo.get("symbol")
            if not sym:
                continue
            self._cache[sym] = (
                ComboMeta(
                    symbol=sym,
                    tick_size=float(combo.get("tick_size", 0.001)),
                    price_min=float(combo.get("price_min", 0.001)),
                    price_max=float(combo.get("price_max", 0.999)),
                    min_qty=float(combo.get("min_qty", 1.0)),
                    legs=list(combo.get("legs") or []),
                ),
                now,
            )

    def get(self, symbol: str) -> Optional[ComboMeta]:
        """Return metadata, refreshing from the transport on miss/expiry."""
        entry = self._cache.get(symbol)
        if entry is None or (time.monotonic() - entry[1]) > self._ttl_s:
            self._load(symbol=symbol)
            entry = self._cache.get(symbol)
        return entry[0] if entry else None

    def refresh(self, symbol: Optional[str] = None) -> None:
        self._load(symbol=symbol)

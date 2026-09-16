"""In-memory leg book cache.

``LegBookCache`` is the hot-path price feed: ``get(symbols)`` is synchronous,
pure in-memory, never blocks on I/O and never raises -- unknown symbols come
back flagged ``missing``. Latency budget: dict lookups only, ~microseconds;
keep it allocation-light (no copies beyond the small snapshot dataclasses).
Staleness is computed at read time against a caller-supplied ``now_ms`` so
both live (wall-clock) and replay (exchange-time) callers share the code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

__all__ = ["LegSnapshot", "LegBookCache"]


@dataclass(frozen=True)
class LegSnapshot:
    symbol: str
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    updated_at: str          # ISO-8601
    updated_ms: int          # epoch ms, for staleness math
    seq: int = 0
    stale: bool = False      # computed at read time
    missing: bool = False    # True when the symbol was never seen


def _iso_to_ms(value: Optional[str]) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


class LegBookCache:
    """``symbol -> latest book`` with per-symbol staleness flags."""

    def __init__(self, staleness_ms: int = 2000) -> None:
        if staleness_ms <= 0:
            raise ValueError("staleness_ms must be positive")
        self._staleness_ms = staleness_ms
        self._books: Dict[str, dict] = {}

    def update(self, symbol: str, bid: Optional[float], ask: Optional[float],
               bid_size: Optional[float] = None, ask_size: Optional[float] = None,
               updated_at: Optional[str] = None, seq: Optional[int] = None) -> None:
        """Record the latest snapshot for a symbol (out-of-order seq ignored)."""
        prev = self._books.get(symbol)
        seq = int(seq or 0)
        if prev is not None and seq < prev["seq"]:
            return  # late snapshot: keep the newer one
        self._books[symbol] = {
            "bid": bid,
            "ask": ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "updated_at": updated_at or "",
            "updated_ms": _iso_to_ms(updated_at),
            "seq": seq,
        }

    def is_stale(self, symbol: str, now_ms: Optional[int] = None) -> bool:
        """True if the symbol is unknown or its snapshot is older than the budget."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        book = self._books.get(symbol)
        if book is None:
            return True
        return (now - book["updated_ms"]) > self._staleness_ms

    def get(self, symbols: Iterable[str],
            now_ms: Optional[int] = None) -> Dict[str, LegSnapshot]:
        """Per-leg snapshot + stale flag. Unknown -> ``missing=True``.

        Synchronous, pure in-memory, never blocks, never raises.
        """
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        out: Dict[str, LegSnapshot] = {}
        for symbol in symbols:
            book = self._books.get(symbol)
            if book is None:
                out[symbol] = LegSnapshot(
                    symbol=symbol, bid=None, ask=None, bid_size=None,
                    ask_size=None, updated_at="", updated_ms=0, missing=True,
                    stale=True,
                )
                continue
            out[symbol] = LegSnapshot(
                symbol=symbol,
                bid=book["bid"],
                ask=book["ask"],
                bid_size=book["bid_size"],
                ask_size=book["ask_size"],
                updated_at=book["updated_at"],
                updated_ms=book["updated_ms"],
                seq=book["seq"],
                stale=(now - book["updated_ms"]) > self._staleness_ms,
            )
        return out

    def symbols(self) -> List[str]:
        return sorted(self._books)

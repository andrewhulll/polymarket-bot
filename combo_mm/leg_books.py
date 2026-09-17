"""Live leg books for combo RFQ legs (receive-only, public endpoints, no auth).

The quoter gateway sends no leg prices, and combo position ids are not CLOB
token ids (the CLOB answers 404 for them). The combo catalog's market ``id``
*is* the Gamma market id, though, and Gamma maps it to the CLOB token ids of
the same market (same ``condition_id``)::

    GET  https://gamma-api.polymarket.com/markets?id=<id>&id=<id>...
         -> clobTokenIds[i] (aligned with outcomes[i]), gameStartTime,
            bestBid / bestAsk (outcome 0), closed, acceptingOrders
    POST https://clob.polymarket.com/books  [{"token_id": ...}, ...]
         -> full book per token (bids / asks with sizes, ms timestamp)

:class:`LiveLegBooks` resolves a catalog leg (market id + outcome index) to a
top-of-book snapshot: CLOB first (fresh, with sizes), Gamma ``bestBid`` /
``bestAsk`` as the fallback (no sizes; outcome 1 is the mirror
``1 - ask`` / ``1 - bid``). Market metadata is cached for ``meta_ttl_s``,
books for ``book_ttl_s``, so many RFQs on one game cost one fetch.

Only GET/POST reads of public market data live here -- nothing places or
cancels orders.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

__all__ = ["GAMMA_MARKETS_URL", "CLOB_BOOKS_URL", "LegBook", "LegRef", "LiveLegBooks"]

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOKS_URL = "https://clob.polymarket.com/books"
_USER_AGENT = "combo_mm/1.0 (curl-compatible)"


@dataclass(frozen=True)
class LegRef:
    """What is needed to look a leg's book up: its catalog market id and outcome."""

    market_id: str
    outcome_index: int


@dataclass(frozen=True)
class LegBook:
    """Top of book for one market outcome."""

    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    ts_ms: int                      # when we fetched it (unix ms)
    source: str                     # "clob" | "gamma"
    kickoff_utc: Optional[str] = None
    closed: bool = False

    def age_s(self, now_ms: int) -> float:
        return max(0.0, (now_ms - self.ts_ms) / 1000.0)


def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _http_post_json(url: str, body: Any, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": _USER_AGENT, "Accept": "application/json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kickoff_iso(value: Any) -> Optional[str]:
    """Gamma ``gameStartTime`` ("2026-09-18 00:15:00+00") -> ISO-8601 UTC."""
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    if text.endswith("+00"):
        text += ":00"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def top_of_book(book: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """(best bid, best ask, bid size, ask size) from a CLOB book payload."""
    def best(levels: Any, highest: bool) -> Tuple[Optional[float], Optional[float]]:
        rows = [(_float(lv.get("price")), _float(lv.get("size"))) for lv in levels or []
                if isinstance(lv, dict)]
        rows = [(p, s) for p, s in rows if p is not None and s and s > 0]
        if not rows:
            return None, None
        return (max if highest else min)(rows, key=lambda r: r[0])
    bid, bid_size = best(book.get("bids"), True)
    ask, ask_size = best(book.get("asks"), False)
    return bid, ask, bid_size, ask_size


class LiveLegBooks:
    """Market id + outcome -> :class:`LegBook`, cached, safe to share across threads."""

    GAMMA_BATCH = 20
    CLOB_BATCH = 50

    def __init__(self, *, get_json: Callable[[str], Any] = _http_get_json,
                 post_json: Callable[[str, Any], Any] = _http_post_json,
                 book_ttl_s: float = 3.0, meta_ttl_s: float = 600.0,
                 clock: Callable[[], float] = time.time) -> None:
        self._get = get_json
        self._post = post_json
        self._book_ttl = book_ttl_s
        self._meta_ttl = meta_ttl_s
        self._clock = clock
        self._meta: Dict[str, Tuple[float, Dict[str, Any]]] = {}     # market id -> (fetched, meta)
        self._books: Dict[str, Tuple[float, LegBook]] = {}          # token id -> (fetched, book)
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None
        self.clob_fetches = 0
        self.gamma_fetches = 0

    # -- metadata -------------------------------------------------------------
    def _market_meta(self, market_ids: Iterable[str], force: bool = False) -> Dict[str, Dict[str, Any]]:
        now = self._clock()
        wanted = sorted({str(m) for m in market_ids if m})
        with self._lock:
            stale = [m for m in wanted if force or m not in self._meta
                     or now - self._meta[m][0] > self._meta_ttl]
        for i in range(0, len(stale), self.GAMMA_BATCH):
            chunk = stale[i:i + self.GAMMA_BATCH]
            url = f"{GAMMA_MARKETS_URL}?" + urllib.parse.urlencode([("id", m) for m in chunk])
            try:
                rows = self._get(url)
                self.gamma_fetches += 1
            except Exception as exc:  # keep what we have; the pricer declines on a missing book
                self.last_error = f"gamma {type(exc).__name__}"
                log.warning("gamma market lookup failed: %s", type(exc).__name__)
                continue
            fetched = self._clock()
            with self._lock:
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict) and row.get("id") is not None:
                        self._meta[str(row["id"])] = (fetched, row)
        with self._lock:
            return {m: self._meta[m][1] for m in wanted if m in self._meta}

    def kickoff(self, market_id: str) -> Optional[str]:
        meta = self._market_meta([market_id]).get(str(market_id))
        return _kickoff_iso(meta.get("gameStartTime")) if meta else None

    # -- books ----------------------------------------------------------------
    def books(self, legs: Sequence[LegRef]) -> Dict[LegRef, Optional[LegBook]]:
        """Top of book for each leg (None when neither CLOB nor Gamma has one)."""
        metas = self._market_meta(leg.market_id for leg in legs)
        tokens: Dict[LegRef, Optional[str]] = {}
        for leg in legs:
            ids = _json_list((metas.get(leg.market_id) or {}).get("clobTokenIds"))
            tokens[leg] = str(ids[leg.outcome_index]) if leg.outcome_index < len(ids) else None
        self._refresh_clob([t for t in tokens.values() if t])

        out: Dict[LegRef, Optional[LegBook]] = {}
        missing: List[LegRef] = []
        with self._lock:
            for leg, token in tokens.items():
                hit = self._books.get(token) if token else None
                out[leg] = hit[1] if hit else None
                # Only a genuinely absent book falls back (unknown token, or the
                # CLOB call failed). A book with an empty side is the real book:
                # re-asking Gamma would cost a round trip and say the same thing.
                if out[leg] is None:
                    missing.append(leg)
        if missing:  # Gamma fallback: best bid/ask of outcome 0, mirrored for outcome 1
            fresh = self._market_meta((leg.market_id for leg in missing), force=True)
            for leg in missing:
                meta = fresh.get(leg.market_id)
                if meta is not None:
                    out[leg] = self._gamma_book(meta, leg.outcome_index) or out[leg]
        return out

    def _refresh_clob(self, tokens: Sequence[str]) -> None:
        now = self._clock()
        with self._lock:
            stale = sorted({t for t in tokens if t not in self._books
                            or now - self._books[t][0] > self._book_ttl})
        for i in range(0, len(stale), self.CLOB_BATCH):
            chunk = stale[i:i + self.CLOB_BATCH]
            try:
                payload = self._post(CLOB_BOOKS_URL, [{"token_id": t} for t in chunk])
                self.clob_fetches += 1
            except Exception as exc:
                self.last_error = f"clob {type(exc).__name__}"
                log.warning("clob books fetch failed: %s", type(exc).__name__)
                continue
            fetched = self._clock()
            with self._lock:
                for book in payload if isinstance(payload, list) else []:
                    if not isinstance(book, dict) or book.get("asset_id") is None:
                        continue
                    bid, ask, bid_size, ask_size = top_of_book(book)
                    # Fetch time, not the book's own timestamp: that is its last
                    # change, and a quiet book is still the current book.
                    self._books[str(book["asset_id"])] = (fetched, LegBook(
                        bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
                        ts_ms=int(fetched * 1000), source="clob"))

    def _gamma_book(self, meta: Dict[str, Any], outcome_index: int) -> Optional[LegBook]:
        bid, ask = _float(meta.get("bestBid")), _float(meta.get("bestAsk"))
        if bid is None or ask is None or not 0.0 <= bid <= ask <= 1.0:
            return None
        if outcome_index == 1:
            bid, ask = 1.0 - ask, 1.0 - bid
        return LegBook(bid=round(bid, 6), ask=round(ask, 6), bid_size=None, ask_size=None,
                       ts_ms=int(self._clock() * 1000), source="gamma",
                       kickoff_utc=_kickoff_iso(meta.get("gameStartTime")),
                       closed=bool(meta.get("closed")))

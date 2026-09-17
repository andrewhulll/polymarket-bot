"""Combo leg catalog: resolve gateway leg position ids to readable markets.

The quoter gateway's ``RFQ_REQUEST`` names each leg only by its on-chain
position id. Polymarket's public combo catalog maps those ids back to
markets::

    GET https://combos-rfq-api.polymarket.com/v1/rfq/combo-markets?limit=100&cursor=...

Each catalog market carries ``position_ids`` / ``outcomes`` /
``outcome_prices`` aligned by index (``[0]`` YES, ``[1]`` NO) plus ``slug``,
``title`` and ``tags``. No auth is needed, but the endpoint answers 403 to
clients without a browser/curl-like ``User-Agent``.

The catalog lists *active* markets ordered by volume, tens of thousands of
them, so :class:`ComboMarketCatalog` crawls it in a background thread,
merges every page into an in-memory index (entries are never dropped, so
RFQs referencing a market that later closes still resolve) and persists the
index to a JSON cache so a restart resolves legs immediately.

Game identity: every sports game market is tagged ``games`` and its slug
starts with ``<league>-<team>-<team>-<yyyy-mm-dd>`` (e.g.
``nfl-sea-ari-2026-09-20-spread-away-2pt5``); the slug up to and including
the date is the game key shared by all markets on that game.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

__all__ = [
    "CATALOG_URL",
    "ComboMarketCatalog",
    "LegMarket",
    "game_key",
    "parse_catalog_page",
]

CATALOG_URL = "https://combos-rfq-api.polymarket.com/v1/rfq/combo-markets"
_USER_AGENT = "combo_mm/1.0 (curl-compatible)"

_GAME_KEY = re.compile(r"^(.+?-\d{4}-\d{2}-\d{2})(?:-|$)")


def game_key(slug: Optional[str]) -> Optional[str]:
    """The ``<league>-...-<yyyy-mm-dd>`` prefix of a game market slug, else None."""
    m = _GAME_KEY.match(slug or "")
    return m.group(1) if m else None


@dataclass(frozen=True)
class LegMarket:
    """One side (YES or NO) of a catalog market, keyed by its position id."""

    position_id: str
    outcome_index: int
    outcome: str
    price: Optional[float]
    market_id: str
    condition_id: str
    slug: str
    title: str
    tags: Tuple[str, ...]

    @property
    def is_game(self) -> bool:
        return "games" in self.tags

    @property
    def is_nfl(self) -> bool:
        return self.is_game and "nfl" in self.tags

    @property
    def game(self) -> Optional[str]:
        """Game key for sports game markets; None for everything else."""
        return game_key(self.slug) if self.is_game else None

    @property
    def league(self) -> Optional[str]:
        return self.slug.split("-", 1)[0] if self.is_game else None


def _price(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_catalog_page(payload: Dict[str, Any]) -> List[LegMarket]:
    """Flatten one catalog response page into per-position :class:`LegMarket` rows."""
    out: List[LegMarket] = []
    for m in payload.get("markets") or []:
        if not isinstance(m, dict):
            continue
        ids = m.get("position_ids") or []
        outcomes = m.get("outcomes") or []
        prices = m.get("outcome_prices") or []
        tags = tuple(str(t) for t in (m.get("tags") or []))
        for i, pid in enumerate(ids):
            out.append(LegMarket(
                position_id=str(pid),
                outcome_index=i,
                outcome=str(outcomes[i]) if i < len(outcomes) else ("Yes" if i == 0 else "No"),
                price=_price(prices[i]) if i < len(prices) else None,
                market_id=str(m.get("id") or ""),
                condition_id=str(m.get("condition_id") or ""),
                slug=str(m.get("slug") or ""),
                title=str(m.get("title") or ""),
                tags=tags,
            ))
    return out


def _http_get_json(url: str, timeout: float = 30.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class ComboMarketCatalog:
    """Position id -> :class:`LegMarket` index, crawled and cached in the background.

    ``version`` increments whenever new entries are merged, so consumers can
    re-resolve legs they could not resolve before.
    """

    CHECKPOINT_PAGES = 200

    def __init__(self, cache_path: Optional[str | Path] = None, *,
                 fetch_json: Callable[[str], Dict[str, Any]] = _http_get_json,
                 url: str = CATALOG_URL, page_limit: int = 100,
                 refresh_interval_s: float = 1800.0,
                 max_pages: Optional[int] = None) -> None:
        self._cache_path = Path(cache_path) if cache_path else None
        self._fetch_json = fetch_json
        self._url = url
        self._page_limit = page_limit
        self._refresh_interval = refresh_interval_s
        self._max_pages = max_pages
        self._index: Dict[str, LegMarket] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.version = 0
        self.refreshing = False
        self.pages_fetched = 0
        self.last_refresh_at: Optional[str] = None
        self.last_error: Optional[str] = None

    # -- lookup ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._index)

    def lookup(self, position_id: str) -> Optional[LegMarket]:
        return self._index.get(str(position_id))

    def resolve(self, position_ids: Iterable[str]) -> List[Optional[LegMarket]]:
        return [self._index.get(str(pid)) for pid in position_ids]

    def merge(self, markets: Iterable[LegMarket]) -> int:
        """Add or refresh entries; returns how many were merged."""
        batch = {m.position_id: m for m in markets}
        if not batch:
            return 0
        with self._lock:
            self._index.update(batch)
            self.version += 1
        return len(batch)

    # -- cache ----------------------------------------------------------------
    def load_cache(self) -> int:
        if self._cache_path is None or not self._cache_path.is_file():
            return 0
        try:
            rows = json.loads(self._cache_path.read_text(encoding="utf-8"))["positions"]
            markets = [LegMarket(**{**row, "tags": tuple(row.get("tags") or ())}) for row in rows]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.last_error = f"cache unreadable ({type(exc).__name__})"
            return 0
        return self.merge(markets)

    def save_cache(self) -> None:
        if self._cache_path is None:
            return
        with self._lock:
            rows = [asdict(m) for m in self._index.values()]
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"saved_at": _now_iso(), "positions": rows}), encoding="utf-8")
        tmp.replace(self._cache_path)

    # -- crawl ----------------------------------------------------------------
    def refresh_once(self) -> int:
        """Crawl every catalog page, merging as it goes; returns pages fetched."""
        self.refreshing = True
        pages, cursor = 0, None
        try:
            while not self._stop.is_set():
                query: Dict[str, Any] = {"limit": self._page_limit}
                if cursor:
                    query["cursor"] = cursor
                payload = self._fetch_json(f"{self._url}?{urllib.parse.urlencode(query)}")
                self.merge(parse_catalog_page(payload))
                pages += 1
                self.pages_fetched += 1
                if pages % self.CHECKPOINT_PAGES == 0:
                    self.save_cache()  # a restart mid-crawl keeps what was fetched
                cursor = payload.get("next_cursor")
                if not cursor or (self._max_pages is not None and pages >= self._max_pages):
                    break
            self.last_refresh_at = _now_iso()
            self.last_error = None
            self.save_cache()
        except Exception as exc:  # network trouble: keep what we have, retry next cycle
            self.last_error = type(exc).__name__
            log.warning("combo catalog refresh failed: %s", type(exc).__name__)
        finally:
            self.refreshing = False
        return pages

    def start(self) -> "ComboMarketCatalog":
        """Load the cache, then crawl in a daemon thread every ``refresh_interval_s``."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self.load_cache()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="combo-catalog", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.refresh_once()
            wait = max(self._refresh_interval - (time.monotonic() - started), 60.0)
            self._stop.wait(wait)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

"""Shared fixture: a catalog page of real-shaped NFL markets plus a stub book source.

Slugs, titles, outcome order and tags follow the live combo catalog
(``combos-rfq-api.polymarket.com/v1/rfq/combo-markets``, verified 2026-09-17):
DET @ BUF on 2026-09-18, a second game for cross-game legs, and a political
market. Position ids are ``<market id>-<outcome index>`` here; only their
uniqueness matters.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from combo_mm.leg_books import LegBook, LegRef

GAME = "nfl-det-buf-2026-09-18"
GAME2 = "nfl-mia-sf-2026-09-20"
KICKOFF = "2026-09-18T00:15:00Z"
KICKOFF2 = "2026-09-20T20:25:00Z"

# slug -> (market id, title, outcomes, prices)
_MARKETS = [
    ("3398287", GAME, "Lions vs. Bills", ["Lions", "Bills"], ["0.325", "0.675"]),
    ("3517187", f"{GAME}-spread-home-4pt5", "Spread: Bills (-4.5)", ["Bills", "Lions"],
     ["0.525", "0.475"]),
    ("3517188", f"{GAME}-spread-home-7pt5", "Spread: Bills (-7.5)", ["Bills", "Lions"],
     ["0.395", "0.605"]),
    ("3517189", f"{GAME}-spread-away-13pt5", "Spread: Lions (-13.5)", ["Lions", "Bills"],
     ["0.06", "0.94"]),
    ("3517192", f"{GAME}-total-54pt5", "Lions vs. Bills: O/U 54.5", ["Over", "Under"],
     ["0.515", "0.485"]),
    ("3517193", f"{GAME}-total-72pt5", "Lions vs. Bills: O/U 72.5", ["Over", "Under"],
     ["0.0895", "0.9105"]),
    # Priced consistently with the spread/total anchors above (implied BUF mean
    # ~30.1 points), so the model's marginal and the market's agree.
    ("3517194", f"{GAME}-team-total-buf-27pt5", "Bills Team Total: O/U 27.5", ["Over", "Under"],
     ["0.615", "0.385"]),
    ("3517195", f"{GAME}-1h-total-28pt5", "Lions vs. Bills: 1H O/U 28.5", ["Over", "Under"],
     ["0.435", "0.565"]),
    ("3517196", f"{GAME}-anytime-td-josh-allen", "Josh Allen anytime TD", ["Yes", "No"],
     ["0.41", "0.59"]),
    ("4384970", GAME2, "Dolphins vs. 49ers", ["Dolphins", "49ers"], ["0.105", "0.895"]),
    ("4384971", f"{GAME2}-total-46pt5", "MIA vs. SF: O/U 46.5", ["Over", "Under"],
     ["0.435", "0.565"]),
    ("4384972", f"{GAME2}-spread-home-12pt5", "Spread: SF (-12.5)", ["SF", "MIA"],
     ["0.495", "0.505"]),
    ("9000001", "will-the-us-invade-iran-before-2027", "US invades Iran before 2027",
     ["Yes", "No"], ["0.12", "0.88"]),
]


def catalog_payload() -> Dict[str, object]:
    """One ``combo-markets`` page in the catalog's wire shape."""
    markets = []
    for market_id, slug, title, outcomes, prices in _MARKETS:
        tags = (["sports", "nfl", "games"] if slug.startswith("nfl-")
                else ["politics"])
        markets.append({
            "id": market_id, "slug": slug, "title": title, "outcomes": outcomes,
            "outcome_prices": prices, "tags": tags,
            "condition_id": f"0x{market_id}", "pending": False,
            "position_ids": [f"{market_id}-0", f"{market_id}-1"],
        })
    return {"markets": markets}


def position(slug: str, outcome_index: int) -> str:
    for market_id, s, *_ in _MARKETS:
        if s == slug:
            return f"{market_id}-{outcome_index}"
    raise KeyError(slug)


class StubBooks:
    """Book source over a ``{(market id, outcome): (bid, ask)}`` table."""

    def __init__(self, prices: Optional[Dict[str, Sequence[float]]] = None, *,
                 kickoffs: Optional[Dict[str, str]] = None, now_ms: int = 0,
                 missing: Sequence[str] = ()) -> None:
        # Default: every market's outcome 0 quoted 1 cent either side of its
        # catalog price, outcome 1 the mirror.
        self.prices: Dict[str, float] = {}
        for market_id, slug, _title, _outcomes, catalog_prices in _MARKETS:
            self.prices[market_id] = float(catalog_prices[0])
        self.prices.update({k: float(v) for k, v in (prices or {}).items()})  # type: ignore[arg-type]
        self.kickoffs = kickoffs or {}
        self.now_ms = now_ms
        self.missing = set(missing)
        self.calls: List[List[LegRef]] = []

    def books(self, legs: Sequence[LegRef]) -> Dict[LegRef, Optional[LegBook]]:
        self.calls.append(list(legs))
        out: Dict[LegRef, Optional[LegBook]] = {}
        for leg in legs:
            if leg.market_id in self.missing:
                out[leg] = None
                continue
            mid = self.prices.get(leg.market_id)
            if mid is None:
                out[leg] = None
                continue
            bid, ask = round(mid - 0.005, 4), round(mid + 0.005, 4)
            if leg.outcome_index == 1:
                bid, ask = round(1.0 - ask, 4), round(1.0 - bid, 4)
            out[leg] = LegBook(bid=bid, ask=ask, bid_size=5000.0, ask_size=5000.0,
                               ts_ms=self.now_ms, source="clob")
        return out

    def kickoff(self, market_id: str) -> Optional[str]:
        return self.kickoffs.get(market_id, KICKOFF)

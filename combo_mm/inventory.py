"""Rebuildable paper inventory from the event store's quotes and fills.

Exposure is conservative maximum loss. A two-sided pending draft can fill
on only one side, so its reservation is the larger side loss. Executed
fills use their actual side and price. No order is sent from this module.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional

from combo_mm.risk import InventoryState


def _game_from_legs(legs: list[dict], symbol: str,
                    resolver: Optional[Callable[[str], Optional[str]]]) -> tuple[str, tuple[str, ...]]:
    games = {resolver(leg["symbol"]) for leg in legs} if resolver else set()
    games.discard(None)
    if len(games) == 1:
        game = next(iter(games))
    else:
        # Canonical NFL symbols share this prefix. Unknown combinations
        # retain their combo symbol, never an invented NFL game id.
        heads = {"-".join(leg["symbol"].split("-")[:5]) for leg in legs
                 if leg.get("symbol", "").startswith("NFL-")}
        game = next(iter(heads)) if len(heads) == 1 else symbol
    nfl_legs = [leg["symbol"] for leg in legs
                if leg.get("symbol", "").startswith("NFL-")]
    parts = nfl_legs[0].split("-") if nfl_legs else []
    teams = tuple(parts[3:5]) if len(parts) >= 5 else ()
    return game, teams


class InventoryProvider:
    """Read a fresh, deterministic snapshot; event ids are deduped by SQLite."""

    def __init__(self, store, *, capital: float = 50000.0,
                 game_resolver: Optional[Callable[[str], Optional[str]]] = None):
        self.store = store
        self.capital = capital
        self.game_resolver = game_resolver

    def __call__(self, as_of: str = "", *,
                 exclude_pending_rfqs: Optional[set[str]] = None) -> InventoryState:
        rfqs, quotes, fills, halted = self.store.inventory_rows()
        pending = defaultdict(float)
        executed = defaultdict(float)
        markets = defaultdict(float)
        teams = defaultdict(float)
        net_by_game = defaultdict(float)
        notional_by_game = defaultdict(float)
        positions = {}
        realized_pnl = 0.0
        rfq_map = {r["rfq_id"]: r for r in rfqs}
        latest = {}
        snapshot_time = (datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                         if as_of else datetime.now(timezone.utc))
        if snapshot_time.tzinfo is None:
            snapshot_time = snapshot_time.replace(tzinfo=timezone.utc)
        for quote in quotes:
            if quote["origin"] != "shadow":
                continue
            rfq_id = quote["rfq_id"]
            if exclude_pending_rfqs and rfq_id in exclude_pending_rfqs:
                continue
            if rfq_id not in rfq_map or rfq_map[rfq_id]["status"] not in (
                    "OPEN", "QUOTED", "RFQ_STATUS_OPEN", "RFQ_STATUS_QUOTED"):
                continue
            deadline = rfq_map[rfq_id].get("submission_deadline")
            if deadline:
                try:
                    expires = (datetime.fromtimestamp(int(deadline) / 1000, timezone.utc)
                               if str(deadline).isdigit() else
                               datetime.fromisoformat(str(deadline).replace("Z", "+00:00")))
                    if expires <= snapshot_time:
                        continue
                except (ValueError, OverflowError):
                    pass
            if as_of and quote["created_time"] and quote["created_time"] > as_of:
                continue
            latest[rfq_id] = quote
        filled_by_quote = defaultdict(lambda: defaultdict(float))
        for fill in sorted(fills, key=lambda row: (row["executed_time"] or "", row["fill_id"])):
            if as_of and fill["executed_time"] and fill["executed_time"] > as_of:
                continue
            filled_by_quote[fill["quote_id"]][fill["side"]] += float(fill["qty"] or 0)
            rfq = rfq_map.get(fill["rfq_id"], {})
            game, members = _game_from_legs(rfq.get("legs", []), fill["symbol"] or "",
                                            self.game_resolver)
            qty = float(fill["qty"] or 0)
            price = float(fill["price"] or 0)
            symbol = fill["symbol"] or ""
            signed = qty if fill["side"] == "BUY" else -qty
            prior, avg, _, _, _ = positions.get(symbol, (0.0, 0.0, game, members, ()))
            if prior * signed < 0:
                closing = min(abs(prior), abs(signed))
                realized_pnl += closing * (price - avg) * (1 if prior > 0 else -1)
            net = prior + signed
            if not net:
                avg = 0.0
            elif prior * signed >= 0:
                avg = (abs(prior) * avg + qty * price) / abs(net)
            elif prior * net < 0:
                avg = price
            leg_symbols = tuple(leg["symbol"] for leg in rfq.get("legs", [])) or (symbol,)
            positions[symbol] = (net, avg, game, members, leg_symbols)
        for net, price, game, members, leg_symbols in positions.values():
            if not net:
                continue
            loss = abs(net) * (price if net > 0 else 1 - price)
            executed[game] += loss
            notional_by_game[game] += abs(net) * price
            net_by_game[game] += net
            for symbol in leg_symbols:
                markets[symbol] += loss
            for team in members:
                teams[team] += loss
        for rfq_id, quote in latest.items():
            rfq = rfq_map[rfq_id]
            game, members = _game_from_legs(rfq["legs"], quote["symbol"] or "",
                                            self.game_resolver)
            buy_qty = max(0.0, float(quote["buy_qty_decimal"] or 0)
                          - filled_by_quote[quote["quote_id"]]["SELL"])
            sell_qty = max(0.0, float(quote["sell_qty_decimal"] or 0)
                           - filled_by_quote[quote["quote_id"]]["BUY"])
            loss = max(buy_qty * (1 - float(quote["buy_price"] or 0)),
                       sell_qty * float(quote["sell_price"] or 0))
            pending[game] += loss
            notional_by_game[game] += max(buy_qty * float(quote["buy_price"] or 0),
                                         sell_qty * float(quote["sell_price"] or 0))
            for leg in rfq.get("legs", []) or [{"symbol": quote["symbol"] or ""}]:
                markets[leg["symbol"]] += loss
            for team in members:
                teams[team] += loss
        pending_map = dict(pending)
        executed_map = dict(executed)
        exposures = {key: pending_map.get(key, 0) + executed_map.get(key, 0)
                     for key in sorted(set(pending_map) | set(executed_map))}
        reserved = sum(exposures.values())
        return InventoryState(exposures=exposures,
                               notional_by_game=dict(notional_by_game), capital=self.capital,
                              pending=pending_map, executed=executed_map,
                              markets=dict(markets), teams=dict(teams),
                              net_by_game=dict(net_by_game),
                              equity=self.capital + realized_pnl,
                              buying_power=self.capital + realized_pnl - reserved,
                              realized_pnl=realized_pnl,
                              kill_switch=halted, as_of=as_of)

    def record(self, ts: str, source_id: str) -> InventoryState:
        snapshot = self(ts)
        self.store.record_exposure_snapshot(ts, source_id, snapshot)
        return snapshot

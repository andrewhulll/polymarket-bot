"""Deterministic additive worst-loss limits and inventory quote adjustments."""
from __future__ import annotations

import math
from combo_mm.risk_config import RiskConfig

from combo_mm.risk import (InventoryState, RiskVerdict, RISK_OK,
    RISK_SIZE_REDUCED, RISK_CAPITAL, RISK_WIDEN, RISK_SKEW,
    RISK_LIMIT_MARKET, RISK_LIMIT_TEAM, RISK_GAME_EXPOSURE,
    RISK_LIMIT_PORTFOLIO, RISK_KILL_SWITCH)


def _round(value: float, tick: float, up: bool) -> float:
    ticks = value / tick
    return (math.ceil(ticks - 1e-10) if up else math.floor(ticks + 1e-10)) * tick


class InventoryRiskCheck:
    """Conservative additive caps; both hypothetical fill sides pass independently.

    The model reserves pending drafts at their larger one-sided loss. It
    deliberately does not assume same-game offsets between different
    contracts, so its exposure can exceed an exact scenario WCL.
    """

    def __init__(self, config: RiskConfig = RiskConfig()):
        self.config = config

    def check(self, draft, notional: float, inventory: InventoryState,
              game_key: str) -> RiskVerdict:
        c = self.config
        e = draft.extra
        buy_qty = max(0, int(float(e.get("buy_qty") or 0)))
        sell_qty = max(0, int(float(e.get("sell_qty") or 0)))
        buy_price = float(e.get("buy_price") or 0)
        sell_price = float(e.get("sell_price") or 0)
        fair = float(draft.fair_value)
        markets = tuple(e.get("markets") or (str(e.get("symbol") or game_key),))
        market = max(markets, key=lambda key: inventory.markets.get(key, 0))
        teams = tuple(e.get("teams") or ())
        before = {"market": inventory.markets.get(market, 0),
                  "game": inventory.exposures.get(game_key, 0),
                  "team": max((inventory.teams.get(t, 0) for t in teams), default=0),
                  "portfolio": sum(inventory.exposures.values()),
                  "buying_power": inventory.buying_power}
        detail = {"game_key": game_key, "market": market, "teams": teams}
        if inventory.kill_switch or c.risk_halt:
            return RiskVerdict(False, "0", "0", RISK_KILL_SWITCH, detail,
                               action="reject", exposure_before=before)
        if inventory.buying_power < c.min_buying_power:
            return RiskVerdict(False, "0", "0", RISK_CAPITAL, detail,
                               action="reject", exposure_before=before)

        limits = (("rfq", c.max_rfq_loss, 0.0, RISK_SIZE_REDUCED),
                  ("market", c.max_market_loss, before["market"], RISK_LIMIT_MARKET),
                  ("game", c.max_game_loss, before["game"], RISK_GAME_EXPOSURE),
                  ("team", c.max_team_loss, before["team"], RISK_LIMIT_TEAM),
                  ("portfolio", c.max_portfolio_loss, before["portfolio"],
                   RISK_LIMIT_PORTFOLIO),
                  ("capital", max(0, inventory.buying_power - c.min_buying_power),
                   0.0, RISK_CAPITAL))

        def allowed(qty: int, unit_loss: float) -> tuple[int, str]:
            if qty == 0 or unit_loss <= 0:
                return qty, RISK_OK
            bounds = [(max(0, math.floor((cap - used + 1e-9) / unit_loss)), code)
                      for _, cap, used, code in limits]
            size, code = min(bounds, key=lambda pair: pair[0])
            return (min(qty, size), code if size < qty else RISK_OK)

        # buyPrice is our offer (short); sellPrice is our bid (long).
        utilization = max(before["game"] / c.max_game_loss,
                          before["portfolio"] / c.max_portfolio_loss,
                          before["market"] / c.max_market_loss,
                          before["team"] / c.max_team_loss)
        x = max(0.0, min(1.0, (utilization - c.soft_utilization)
                         / (1 - c.soft_utilization)))
        widen = c.max_widen_bps * x * x * (3 - 2 * x)
        net = inventory.net_by_game.get(game_key, 0)
        skew = c.max_skew_bps * min(1.0, utilization) * (
            net / (abs(net) + max(buy_qty, sell_qty, 1)))
        # Long inventory: lower both prices to encourage selling and
        # discourage buying. The sign reverses for short inventory.
        offer = buy_price + (widen - skew) / 10000
        bid = sell_price - (widen + skew) / 10000
        min_edge = c.min_edge_bps / 10000
        offer = _round(min(0.999, max(fair + min_edge, offer)), c.tick_size, True)
        bid = _round(max(0.001, min(fair - min_edge, bid)), c.tick_size, False)
        bq, bc = allowed(buy_qty, max(0, 1 - offer))
        sq, sc = allowed(sell_qty, max(0, bid))
        if bq < c.min_qty:
            bq = 0
        if sq < c.min_qty:
            sq = 0
        if not bq and not sq:
            reason = bc if bc != RISK_OK else sc
            return RiskVerdict(False, "0", "0", reason, detail, action="reject",
                               exposure_before=before, exposure_after=before)
        if bq == 0:
            offer = 0.0
        if sq == 0:
            bid = 0.0
        flags = tuple(code for code, yes in ((RISK_SIZE_REDUCED, bq < buy_qty or sq < sell_qty),
                                              (RISK_SKEW, abs(skew) > 1e-9),
                                              (RISK_WIDEN, widen > 1e-9)) if yes)
        action = ("reduce" if RISK_SIZE_REDUCED in flags else
                  "skew" if RISK_SKEW in flags else
                  "widen" if RISK_WIDEN in flags else "quote")
        after = {**before, "market": before["market"] + max(bq * (1 - offer), sq * bid),
                 "game": before["game"] + max(bq * (1 - offer), sq * bid),
                 "team": before["team"] + max(bq * (1 - offer), sq * bid),
                 "portfolio": before["portfolio"] + max(bq * (1 - offer), sq * bid),
                 "buying_power": before["buying_power"] - max(bq * (1 - offer), sq * bid)}
        reason = (f"{game_key}: game ${before['game']:.2f}/${c.max_game_loss:.2f}; "
                  f"{action}; widen {widen:.0f} bps, skew {skew:.0f} bps")
        return RiskVerdict(True, str(bq), str(sq), reason, detail, action=action,
                           adjusted_buy_price=offer, adjusted_sell_price=bid,
                           widen_bps=widen, skew_bps=skew, flags=flags,
                           exposure_before=before, exposure_after=after)

"""Minimal V1 independent-leg combo pricer (design doc §05).

Pure module: no I/O, no network, no database. It takes leg book snapshots as
arguments and returns a structured :class:`QuoteDecision` carrying every
spread component and reason code, so the dashboard can explain each quote
(and each decline).

Pricing model
-------------
- Leg mark: bounded microprice ``(ask_size*bid + bid_size*ask)/(bid_size+ask_size)``
  clamped into ``[bid, ask]``; midpoint fallback when sizes are missing/zero.
  Crossed books, missing sides, and stale snapshots are rejected with reason
  codes (flagged, never blocking the pipeline -- the decline is recorded).
- ``q_i = p_i`` for YES legs, ``1 - p_i`` for NO legs. A resolved winning leg
  contributes 1; a resolved losing leg forces the combo to 0 (decline).
- ``fair = product(q_i)``, clamped to ``[0, 1]``.
- ``half_spread = min(max_half_spread_bps, leg_width_multiplier *
  avg_leg_half_spread) / 10000`` in ABSOLUTE price units (not scaled by
  ``fair``): the spread is copied from the legs' own books, which have
  already priced in uncertainty and disagreement; the combo inherits it
  plus combination risk via the multiplier. The cap keeps the total spread
  at or under 100 bps. One knob (the multiplier), one gate: decline when
  any leg book is stale or wider than ``max_leg_spread_bps``.
- ``center = fair`` (no inventory skew -- the risk engine is parked).
- ``buyPrice`` (our offer / creator's buy) = round UP to tick of
  ``center + half_spread``; ``sellPrice`` (our bid / creator's sell) = round
  DOWN to tick of ``center - half_spread``; tick 0.001. Prices clamp to
  instrument limits; a side that cannot be quoted is set to ``0.0``. At least
  one side must stay positive.

Sizing
------
- Quantity RFQs: ``qtyDecimal`` per side.
- Cash RFQs: ``floor(cashOrderQty / sidePrice)`` per side, each with a
  minimum-size validity flag.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

__all__ = [
    "LegMarkInput",
    "QuoteDecision",
    "price_combo",
    # reason codes
    "QUOTED_OK",
    "STALE_LEG",
    "WIDE_LEG_BOOK",
    "MISSING_LEG",
    "CROSSED_BOOK",
    "RESOLVED_LOSER",
    "RFQ_CLOSED",
    "SIZE_BELOW_MINIMUM",
    "ZERO_FAIR",
]

QUOTED_OK = "QUOTED_OK"
STALE_LEG = "STALE_LEG"
WIDE_LEG_BOOK = "WIDE_LEG_BOOK"
MISSING_LEG = "MISSING_LEG"
CROSSED_BOOK = "CROSSED_BOOK"
RESOLVED_LOSER = "RESOLVED_LOSER"
RFQ_CLOSED = "RFQ_CLOSED"
SIZE_BELOW_MINIMUM = "SIZE_BELOW_MINIMUM"
ZERO_FAIR = "ZERO_FAIR"

_EPS = 1e-9


@dataclass(frozen=True)
class LegMarkInput:
    """One combo leg with its book snapshot for pricing."""

    symbol: str
    side: str                      # "YES" or "NO": combo side of this leg
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    stale: bool = False
    resolved_price: Optional[float] = None  # leg's own settlement (1 win / 0 loss)


@dataclass(frozen=True)
class QuoteDecision:
    """Structured pricing outcome: quote or decline with full explanation."""

    rfq_id: str
    model_version: str
    fair: float
    half_spread: float              # price units
    buy_price: float                # our offer (0.0 = side unavailable)
    sell_price: float               # our bid   (0.0 = side unavailable)
    buy_qty: str
    sell_qty: str
    expected_edge_bps: float
    reason_code: str
    components: Dict[str, Any] = field(default_factory=dict)
    legs_snapshot_hash: str = ""
    decided_at: str = ""

    @property
    def quoted(self) -> bool:
        return self.reason_code == QUOTED_OK


def _leg_mark(leg: LegMarkInput) -> tuple[Optional[float], Optional[str]]:
    """Return (mark, decline_reason). Mark is None when the leg is unpriceable."""
    if leg.bid is None or leg.ask is None:
        return None, MISSING_LEG
    if leg.bid > leg.ask:
        return None, CROSSED_BOOK
    if leg.stale:
        return None, STALE_LEG
    if leg.bid_size and leg.ask_size and (leg.bid_size + leg.ask_size) > 0:
        micro = (leg.ask_size * leg.bid + leg.bid_size * leg.ask) / (
            leg.bid_size + leg.ask_size
        )
    else:
        micro = (leg.bid + leg.ask) / 2.0  # midpoint fallback
    # Bound the microprice inside the touch.
    mark = min(max(micro, leg.bid), leg.ask)
    return mark, None


def _round_up_to_tick(value: float, tick: float) -> float:
    return math.ceil(value / tick - 1e-9) * tick


def _round_down_to_tick(value: float, tick: float) -> float:
    return math.floor(value / tick + 1e-9) * tick


def price_combo(
    legs: List[LegMarkInput],
    *,
    rfq_id: str,
    rfq_status: str = "OPEN",
    qty_decimal: Optional[str] = None,
    cash_order_qty: Optional[str] = None,
    model_version: str = "v1",
    decided_at: str = "",
    # Markup knobs: the spread is copied from the legs' own books.
    # half_spread = min(max_half_spread_bps,
    #                   leg_width_multiplier * avg leg half-spread).
    leg_width_multiplier: float = 2.0,
    max_half_spread_bps: float = 50.0,   # total spread never exceeds 100 bps
    max_leg_spread_bps: float = 1000.0,  # decline when a leg book is wider
    tick_size: float = 0.001,
    price_min: float = 0.001,
    price_max: float = 0.999,
    min_qty: float = 1.0,
    fair_override: Optional[float] = None,
) -> QuoteDecision:
    """Price one combo RFQ. Pure function -- no I/O.

    Returns a :class:`QuoteDecision` with ``reason_code=QUOTED_OK`` on success,
    or a decline reason otherwise. The markup inputs are recorded in
    ``components`` for dashboard explanation.

    ``fair_override`` lets a dependence-aware pricer (e.g. the NFL joint
    model) supply the combo fair while reusing this function's leg checks,
    markup, tick rounding and sizing. The independent-leg product is still
    computed and recorded as ``components["naive_fair"]``.
    """
    leg_inputs = [
        {
            "symbol": leg.symbol,
            "side": leg.side,
            "bid": leg.bid,
            "ask": leg.ask,
            "bid_size": leg.bid_size,
            "ask_size": leg.ask_size,
            "stale": leg.stale,
            "resolved_price": leg.resolved_price,
        }
        for leg in legs
    ]
    snapshot_hash = hashlib.sha256(
        json.dumps(leg_inputs, sort_keys=True, default=str).encode()
    ).hexdigest()

    def decline(code: str, **extra: Any) -> QuoteDecision:
        components: Dict[str, Any] = {
            "leg_count": len(legs),
            "leg_marks": [],
            **extra,
        }
        return QuoteDecision(
            rfq_id=rfq_id,
            model_version=model_version,
            fair=0.0,
            half_spread=0.0,
            buy_price=0.0,
            sell_price=0.0,
            buy_qty="0",
            sell_qty="0",
            expected_edge_bps=0.0,
            reason_code=code,
            components=components,
            legs_snapshot_hash=snapshot_hash,
            decided_at=decided_at,
        )

    # Only OPEN (new RFQ) and QUOTED (replacement flow) are quotable. Once
    # the requester has accepted our quote -- or the RFQ is otherwise
    # terminal -- quoting again is wrong: the deal is bilateral/done.
    if rfq_status in ("CANCELLED", "EXPIRED", "CLOSED",
                      "ACCEPTED", "CONFIRMED", "EXECUTED"):
        return decline(RFQ_CLOSED, rfq_status=rfq_status)
    if not legs:
        return decline(MISSING_LEG)

    # --- leg marks -------------------------------------------------------
    q_list: List[float] = []
    leg_marks: List[Dict[str, Any]] = []
    for leg in legs:
        if leg.side not in ("YES", "NO"):
            return decline(MISSING_LEG, bad_side=leg.side, symbol=leg.symbol)
        # Resolved legs bypass the book.
        if leg.resolved_price is not None:
            rp = float(leg.resolved_price)
            if leg.side == "YES":
                q = 1.0 if rp >= 0.5 else 0.0
                won = rp >= 0.5
            else:
                q = 1.0 if rp < 0.5 else 0.0
                won = rp < 0.5
            if not won:
                return decline(
                    RESOLVED_LOSER, symbol=leg.symbol, side=leg.side,
                    resolved_price=rp,
                )
            q_list.append(q)
            leg_marks.append({"symbol": leg.symbol, "q": q, "resolved": True})
            continue
        mark, reason = _leg_mark(leg)
        if reason is not None:
            return decline(reason, symbol=leg.symbol, side=leg.side)
        assert mark is not None
        q = mark if leg.side == "YES" else 1.0 - mark
        q_list.append(q)
        # A 0.0 bid is a real (empty) side, not a missing one: keep the true
        # mid rather than collapsing it to _EPS (which made spread_bps ~1e11).
        mid = (leg.bid + leg.ask) / 2.0
        spread_bps = ((leg.ask - leg.bid) / max(mid, _EPS)) * 10000.0
        leg_width_bps = (leg.ask - leg.bid) * 10000.0  # absolute, for the markup
        leg_marks.append(
            {
                "symbol": leg.symbol,
                "side": leg.side,
                "bid": leg.bid,
                "ask": leg.ask,
                "mark": mark,
                "q": q,
                "spread_bps": spread_bps,
                "leg_width_bps": leg_width_bps,
            }
        )

    fair = 1.0
    for q in q_list:
        fair *= q
    fair = min(max(fair, 0.0), 1.0)
    naive_fair = fair
    if fair_override is not None:
        fair = min(max(float(fair_override), 0.0), 1.0)

    # --- markup: copy the spread from the legs' own books ------------------
    # half_spread = multiplier x average leg half-spread, in ABSOLUTE price
    # units (not scaled by fair), capped so the total spread never exceeds
    # 100 bps. The legs' books have already priced in uncertainty,
    # disagreement and event risk; the combo inherits all of it plus
    # combination risk via the multiplier. Resolved legs have no book, so
    # they contribute no width.
    live_half_widths_bps: List[float] = []
    for m in leg_marks:
        if m.get("resolved"):
            continue
        width_bps = float(m["leg_width_bps"])
        if width_bps > max_leg_spread_bps:
            return decline(
                WIDE_LEG_BOOK,
                symbol=m["symbol"],
                leg_width_bps=width_bps,
                max_leg_spread_bps=max_leg_spread_bps,
            )
        live_half_widths_bps.append(width_bps / 2.0)
    avg_leg_half_spread_bps = (
        sum(live_half_widths_bps) / len(live_half_widths_bps)
        if live_half_widths_bps else 0.0
    )
    uncapped_half_spread_bps = leg_width_multiplier * avg_leg_half_spread_bps
    half_spread_bps = min(max_half_spread_bps, uncapped_half_spread_bps)
    half_spread = half_spread_bps / 10000.0  # absolute price units
    total_bps = 2.0 * half_spread_bps  # full spread; kept as spread_bps_total

    # --- prices ----------------------------------------------------------
    # center = fair (no inventory skew: the risk engine is parked).
    raw_buy = fair + half_spread    # our offer
    raw_sell = fair - half_spread   # our bid
    buy_price = _round_up_to_tick(raw_buy, tick_size)
    sell_price = _round_down_to_tick(raw_sell, tick_size)
    # A side outside instrument limits cannot be quoted -> 0.0. Never clamp
    # it back inside: clamping the offer down to price_max would sell below
    # fair + required edge (at fair == 1.0, below fair itself).
    if not (price_min <= buy_price <= price_max):
        buy_price = 0.0
    if not (price_min <= sell_price <= price_max):
        sell_price = 0.0
    if buy_price == 0.0 and sell_price == 0.0:
        # fair == 0 (worthless combo): nothing to quote.
        return decline(ZERO_FAIR, fair=fair, note="no quotable side")
    # Invariant: our offer (buyPrice) is never below our bid (sellPrice)
    # for the live side(s).
    if buy_price and sell_price:
        assert buy_price >= sell_price, "offer below bid"

    # --- sizing ----------------------------------------------------------
    def valid_qty(qty: Decimal) -> bool:
        return qty >= Decimal(str(min_qty))

    buy_qty_d = sell_qty_d = Decimal(0)
    valid_buy = valid_sell = False
    if qty_decimal is not None:
        qd = Decimal(qty_decimal)
        buy_qty_d = sell_qty_d = qd
        valid_buy = valid_sell = valid_qty(qd)
    elif cash_order_qty is not None:
        cash = Decimal(cash_order_qty)
        if buy_price > 0:
            buy_qty_d = Decimal(math.floor(float(cash / Decimal(str(buy_price)))))
            valid_buy = valid_qty(buy_qty_d)
            if not valid_buy:
                buy_price = 0.0
        if sell_price > 0:
            sell_qty_d = Decimal(math.floor(float(cash / Decimal(str(sell_price)))))
            valid_sell = valid_qty(sell_qty_d)
            if not valid_sell:
                sell_price = 0.0
    if buy_price == 0.0 and sell_price == 0.0:
        return decline(SIZE_BELOW_MINIMUM, fair=fair, note="no live side after sizing")
    if qty_decimal is not None and not (valid_buy or valid_sell):
        # Quantity below the per-side minimum on every live side.
        return decline(SIZE_BELOW_MINIMUM, fair=fair, qty=qty_decimal)

    expected_edge_bps = (half_spread / max(fair, _EPS)) * 10000.0

    components = {
        "leg_count": len(legs),
        "leg_marks": leg_marks,
        "fair": fair,
        "naive_fair": naive_fair,
        "spread_bps_total": total_bps,
        "half_spread_bps": half_spread_bps,
        "half_spread_bps_uncapped": uncapped_half_spread_bps,
        "avg_leg_half_spread_bps": avg_leg_half_spread_bps,
        "leg_width_multiplier": leg_width_multiplier,
        "max_half_spread_bps": max_half_spread_bps,
        "spread_capped": half_spread_bps < uncapped_half_spread_bps,
        "half_spread": half_spread,
        "center": fair,
        "tick_size": tick_size,
        "valid_buy": valid_buy,
        "valid_sell": valid_sell,
        "size_mode": "qty" if qty_decimal is not None else "cash",
    }
    return QuoteDecision(
        rfq_id=rfq_id,
        model_version=model_version,
        fair=fair,
        half_spread=half_spread,
        buy_price=buy_price,
        sell_price=sell_price,
        buy_qty=str(buy_qty_d),
        sell_qty=str(sell_qty_d),
        expected_edge_bps=expected_edge_bps,
        reason_code=QUOTED_OK,
        components=components,
        legs_snapshot_hash=snapshot_hash,
        decided_at=decided_at,
    )

"""V1 pricer: fair-value bounds, spread monotonicity, reason codes, sizing."""
import pytest

from combo_mm.pricing import (
    CROSSED_BOOK,
    MISSING_LEG,
    QUOTED_OK,
    RESOLVED_LOSER,
    RFQ_CLOSED,
    SIZE_BELOW_MINIMUM,
    STALE_LEG,
    WIDE_LEG_BOOK,
    ZERO_FAIR,
    LegMarkInput,
    price_combo,
)


def _legs(**over):
    kw = dict(bid=0.52, ask=0.54, bid_size=1000.0, ask_size=1000.0)
    kw.update(over)
    return [
        LegMarkInput(symbol="DEM", side="YES", **kw),
        LegMarkInput(symbol="GOP", side="NO", bid=0.44, ask=0.46,
                     bid_size=1000.0, ask_size=1000.0),
    ]


def test_fair_value_in_unit_interval():
    d = price_combo(_legs(), rfq_id="R", qty_decimal="100")
    assert d.reason_code == QUOTED_OK
    assert 0.0 <= d.fair <= 1.0
    # microprice 0.53 * (1 - 0.45) = 0.2915
    assert abs(d.fair - 0.2915) < 1e-9


def test_offer_never_below_bid():
    for qty in ("1", "100", "10000"):
        d = price_combo(_legs(), rfq_id="R", qty_decimal=qty)
        assert d.reason_code == QUOTED_OK
        assert d.buy_price >= d.sell_price > 0


def test_tick_rounding():
    d = price_combo(_legs(), rfq_id="R", qty_decimal="100", tick_size=0.001)
    assert d.reason_code == QUOTED_OK
    assert abs(d.buy_price * 1000 - round(d.buy_price * 1000)) < 1e-9
    assert abs(d.sell_price * 1000 - round(d.sell_price * 1000)) < 1e-9
    # buy rounded UP from center+hs, sell rounded DOWN from center-hs
    assert d.buy_price >= d.fair + d.half_spread - 1e-9
    assert d.sell_price <= d.fair - d.half_spread + 1e-9


def test_markup_copies_leg_width():
    # legs 0.60/0.62 and 0.70/0.71: half-spreads 100 and 50 bps, avg 75.
    legs = [LegMarkInput(symbol="A", side="YES", bid=0.60, ask=0.62),
            LegMarkInput(symbol="B", side="YES", bid=0.70, ask=0.71)]
    d = price_combo(legs, rfq_id="R", qty_decimal="100")
    assert d.reason_code == QUOTED_OK
    assert d.components["avg_leg_half_spread_bps"] == pytest.approx(75.0)
    # 2 x 75 = 150 uncapped, but the total spread never exceeds 100 bps.
    assert d.components["half_spread_bps_uncapped"] == pytest.approx(150.0)
    assert d.components["half_spread_bps"] == 50.0
    assert d.components["spread_capped"] is True
    assert d.components["spread_bps_total"] == 100.0


def test_markup_below_cap_is_untouched():
    # 40bps-wide legs -> half-spreads 20 bps each -> 2 x 20 = 40, no cap.
    legs = [LegMarkInput(symbol="A", side="YES", bid=0.600, ask=0.604),
            LegMarkInput(symbol="B", side="YES", bid=0.700, ask=0.704)]
    d = price_combo(legs, rfq_id="R", qty_decimal="100")
    assert d.reason_code == QUOTED_OK
    assert d.components["avg_leg_half_spread_bps"] == pytest.approx(20.0)
    assert d.components["half_spread_bps"] == pytest.approx(40.0)
    assert d.components["spread_capped"] is False
    assert d.components["spread_bps_total"] == pytest.approx(80.0)
    assert abs(d.half_spread - 0.0040) < 1e-12


def test_wider_legs_never_tighten_markup():
    tight = price_combo(_legs(), rfq_id="R", qty_decimal="100")
    wide = price_combo(_legs(bid=0.50, ask=0.56), rfq_id="R", qty_decimal="100")
    assert wide.reason_code == QUOTED_OK
    assert wide.half_spread >= tight.half_spread
    assert (wide.components["avg_leg_half_spread_bps"]
            > tight.components["avg_leg_half_spread_bps"])


def test_size_does_not_change_markup():
    # Depth is gone: the spread comes only from the legs' books.
    small = price_combo(_legs(), rfq_id="R", qty_decimal="10")
    big = price_combo(_legs(), rfq_id="R", qty_decimal="100000")
    assert big.half_spread == small.half_spread
    assert big.components["spread_bps_total"] == small.components["spread_bps_total"]


def test_wide_leg_book_declines():
    legs = [LegMarkInput(symbol="A", side="YES", bid=0.40, ask=0.60),
            LegMarkInput(symbol="B", side="YES", bid=0.70, ask=0.71)]
    d = price_combo(legs, rfq_id="R", qty_decimal="10")
    assert d.reason_code == WIDE_LEG_BOOK
    assert not d.quoted
    assert d.components["symbol"] == "A"


def test_multiplier_knob():
    legs = [LegMarkInput(symbol="A", side="YES", bid=0.600, ask=0.604),
            LegMarkInput(symbol="B", side="YES", bid=0.700, ask=0.704)]
    d = price_combo(legs, rfq_id="R", qty_decimal="100", leg_width_multiplier=3.0)
    assert d.components["half_spread_bps_uncapped"] == pytest.approx(60.0)
    assert d.components["half_spread_bps"] == 50.0  # capped at 50
    assert d.components["spread_bps_total"] == 100.0


def test_midpoint_fallback_without_sizes():
    legs = [LegMarkInput(symbol="A", side="YES", bid=0.48, ask=0.52),
            LegMarkInput(symbol="B", side="NO", bid=0.48, ask=0.52)]
    d = price_combo(legs, rfq_id="R", qty_decimal="10")
    assert d.reason_code == QUOTED_OK
    assert abs(d.fair - 0.25) < 1e-9  # 0.5 * (1 - 0.5)


def test_microprice_bounded_in_touch():
    # size-skewed microprice would print outside [bid, ask] if unbounded
    leg = LegMarkInput(symbol="A", side="YES", bid=0.5, ask=0.52,
                       bid_size=1.0, ask_size=100000.0)
    d = price_combo([leg], rfq_id="R", qty_decimal="10")
    mark = d.components["leg_marks"][0]["mark"]
    assert 0.5 <= mark <= 0.52


def test_resolved_legs():
    win = [LegMarkInput(symbol="A", side="YES", resolved_price=1.0),
           LegMarkInput(symbol="B", side="NO", bid=0.48, ask=0.52,
                        bid_size=10.0, ask_size=10.0)]
    d = price_combo(win, rfq_id="R", qty_decimal="10")
    assert d.reason_code == QUOTED_OK
    assert abs(d.fair - 0.5) < 1e-9  # 1.0 * (1 - 0.5)

    lose = [LegMarkInput(symbol="A", side="YES", resolved_price=0.0),
            LegMarkInput(symbol="B", side="NO", bid=0.48, ask=0.52,
                         bid_size=10.0, ask_size=10.0)]
    d = price_combo(lose, rfq_id="R", qty_decimal="10")
    assert d.reason_code == RESOLVED_LOSER
    assert not d.quoted and d.fair == 0.0

    # NO leg: resolved_price is the leg's own settlement; 0.0 means NO wins.
    # All legs resolved -> no live book to copy a width from, and quoting at
    # exactly 1.0 is outside the instrument limits -> decline.
    no_win = [LegMarkInput(symbol="B", side="NO", resolved_price=0.0)]
    d = price_combo(no_win, rfq_id="R", qty_decimal="10")
    assert d.reason_code == ZERO_FAIR
    assert not d.quoted


def test_decline_reasons():
    assert price_combo(_legs(), rfq_id="R", qty_decimal="10",
                       rfq_status="CLOSED").reason_code == RFQ_CLOSED
    assert price_combo(_legs(), rfq_id="R", qty_decimal="10",
                       rfq_status="CANCELLED").reason_code == RFQ_CLOSED
    tiny = price_combo(_legs(), rfq_id="R", qty_decimal="0.001",
                       min_qty=1.0)
    assert tiny.reason_code == SIZE_BELOW_MINIMUM


def test_cash_sizing_floors():
    d = price_combo(_legs(), rfq_id="R", cash_order_qty="500")
    assert d.reason_code == QUOTED_OK
    assert d.components["size_mode"] == "cash"
    # floor(cash / price), per side
    import math
    assert int(d.buy_qty) == math.floor(500 / d.buy_price)
    assert int(d.sell_qty) == math.floor(500 / d.sell_price)
    assert d.components["valid_buy"] and d.components["valid_sell"]


def test_expected_edge_positive_and_explained():
    d = price_combo(_legs(), rfq_id="R", qty_decimal="100")
    assert d.expected_edge_bps > 0
    comps = d.components
    for key in ("avg_leg_half_spread_bps", "leg_width_multiplier",
                "half_spread_bps", "half_spread_bps_uncapped",
                "max_half_spread_bps", "spread_capped", "spread_bps_total",
                "half_spread", "leg_marks"):
        assert key in comps
    # half-spread = multiplier x avg leg half-spread, capped at 50 bps
    assert comps["half_spread_bps"] == min(
        comps["max_half_spread_bps"], comps["half_spread_bps_uncapped"])
    assert comps["spread_bps_total"] == 2.0 * comps["half_spread_bps"]
    assert comps["spread_bps_total"] <= 100.0


def test_pricer_is_pure_no_io():
    # No arguments are mutated; repeated calls are identical.
    legs = _legs()
    a = price_combo(legs, rfq_id="R", qty_decimal="100")
    b = price_combo(legs, rfq_id="R", qty_decimal="100")
    assert a == b
    assert legs[0].bid == 0.52  # inputs untouched

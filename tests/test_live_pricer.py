"""Live NFL same-game pricer (#2): fair value, quote terms, and every decline path."""
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from combo_mm.combo_markets import ComboMarketCatalog, parse_catalog_page
from combo_mm.config import PipelineConfig
from combo_mm.nfl.joint import GameModel, calibrate_means, home_cover, home_ml, over
from combo_mm.nfl.live_pricer import (
    CONTRADICTORY_LEGS,
    GAME_STARTED,
    MISSING_CALIBRATION_MARKET,
    MODEL_MARKET_DISAGREE,
    NO_NFL_SAME_GAME,
    OTHER_SAME_GAME,
    PARAMS_STALE,
    PARAMS_UNAVAILABLE,
    UNRESOLVED_LEG,
    UNSUPPORTED_LEG,
    LiveRfq,
    NflLivePricer,
    NflLivePricerConfig,
)
from combo_mm.nfl.params_io import matchup_covariance
from combo_mm.nfl.params_provider import ParamsProvider
from combo_mm.pricing import MISSING_LEG, QUOTED_OK, STALE_LEG
from tests.nfl_live_fixtures import GAME, GAME2, KICKOFF2, StubBooks, catalog_payload, position

PARAMS_DIR = Path(__file__).resolve().parents[1] / "params"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)   # day after the params pull, pre-kickoff
NOW_MS = int(NOW.timestamp() * 1000)

ML_HOME = position(GAME, 1)                       # Bills win
ML_AWAY = position(GAME, 0)                       # Lions win
FAV_COVER = position(f"{GAME}-spread-home-4pt5", 0)   # Bills -4.5
DOG_COVER = position(f"{GAME}-spread-home-4pt5", 1)   # Lions +4.5
OVER = position(f"{GAME}-total-54pt5", 0)
UNDER = position(f"{GAME}-total-54pt5", 1)
TEAM_TOTAL = position(f"{GAME}-team-total-buf-27pt5", 0)
FIRST_HALF = position(f"{GAME}-1h-total-28pt5", 0)
PROP = position(f"{GAME}-anytime-td-josh-allen", 0)
OTHER_GAME_ML = position(GAME2, 1)
OTHER_GAME_TOTAL = position(f"{GAME2}-total-46pt5", 0)
POLITICS = position("will-the-us-invade-iran-before-2027", 0)


def build(books=None, *, params_dir=PARAMS_DIR, model_config=None, config=None):
    catalog = ComboMarketCatalog()
    catalog.merge(parse_catalog_page(catalog_payload()))
    books = books or StubBooks(now_ms=NOW_MS, kickoffs={"4384970": KICKOFF2, "4384971": KICKOFF2,
                                                        "4384972": KICKOFF2})
    return NflLivePricer(catalog, books, ParamsProvider(params_dir),
                         model_config=model_config, config=config)


def price(pricer, *positions, side="YES", direction="BUY", qty="25", cash=None, now=NOW):
    return pricer.price(LiveRfq(rfq_id="RFQ-1", leg_position_ids=tuple(positions), side=side,
                                direction=direction, qty_decimal=qty, cash_order_qty=cash), now=now)


def test_paper_quote_is_kept_after_deadline_and_latency_budget():
    positions = (ML_HOME, FAV_COVER)
    expired = build().price(LiveRfq("late", positions, qty_decimal="25",
                                   submission_deadline_ms=NOW_MS - 1), now=NOW)
    assert expired.quoted and expired.after_deadline
    assert expired.bid is not None and expired.ask is not None

    class SlowBooks(StubBooks):
        def books(self, legs):
            time.sleep(0.01)
            return super().books(legs)

    slow = build(SlowBooks(now_ms=NOW_MS))
    finished_late = slow.price(LiveRfq("slow", positions, qty_decimal="25",
                                       submission_deadline_ms=NOW_MS + 1), now=NOW)
    assert finished_late.quoted and finished_late.after_deadline
    assert finished_late.bid is not None and finished_late.ask is not None

    over_budget = build(SlowBooks(now_ms=NOW_MS),
                        config=PipelineConfig(quote_latency_budget_ms=1)).price(
        LiveRfq("cold", positions, qty_decimal="25"), now=NOW)
    assert over_budget.quoted and over_budget.latency_ms > 1
    assert over_budget.bid is not None and over_budget.ask is not None


# -- fair value -------------------------------------------------------------

def test_nested_combo_prices_far_above_naive_and_below_its_cheapest_leg():
    """Fav ML + Fav covers: covering implies winning, so the naive product is much too low."""
    quote = price(build(), ML_HOME, FAV_COVER)
    assert quote.reason_code == QUOTED_OK
    assert quote.fair_yes > quote.naive_yes + 0.10
    assert quote.fair_yes <= min(leg["q_market"] for leg in quote.legs) + 1e-9   # Frechet upper bound
    assert quote.corr_adjustment_bps > 1000


def test_fav_ml_with_dog_cover_prices_far_below_naive():
    """"Bills win but don't cover" -- the backtest's worst naive miss (32.8% vs 16.9% realized)."""
    quote = price(build(), ML_HOME, DOG_COVER)
    assert quote.reason_code == QUOTED_OK
    assert quote.fair_yes < quote.naive_yes - 0.10
    assert quote.corr_adjustment_bps < -1000


def test_spread_times_total_reproduces_the_naive_product():
    """The tuned league_constant model has no margin/total dependence (docs 4.2)."""
    quote = price(build(), FAV_COVER, OVER)
    assert quote.reason_code == QUOTED_OK
    assert quote.fair_yes == pytest.approx(quote.naive_yes, abs=2e-4)
    assert quote.games[0]["lift"] == pytest.approx(1.0, abs=2e-3)


def test_market_lift_equals_market_marginals_times_model_dependence():
    quote = price(build(), ML_HOME, FAV_COVER)
    game = quote.games[0]
    lift = game["model_joint"] / math.prod(leg["p_model"] for leg in quote.legs)
    assert game["lift"] == pytest.approx(lift, rel=1e-9)
    assert quote.fair_yes == pytest.approx(min(game["naive"] * lift, min(
        leg["q_market"] for leg in quote.legs)), rel=1e-9)


def test_model_joint_method_matches_the_joint_engine_directly():
    pricer = build(model_config=NflLivePricerConfig(method="model_joint"))
    quote = price(pricer, ML_HOME, FAV_COVER)
    game = quote.games[0]
    params = json.loads((PARAMS_DIR / "nfl_2026_w02.json").read_text())
    cal = calibrate_means(game["spread_line"], game["p_home_cover"], game["total_line"],
                          game["p_over"],
                          lambda mh, ma: matchup_covariance(params, "BUF", "DET", mh, ma))
    expected = GameModel((cal.mu_home, cal.mu_away), cal.cov).joint([home_ml(), home_cover(4.5)])
    assert game["model_joint"] == pytest.approx(expected, rel=1e-9)
    assert quote.fair_yes == pytest.approx(min(expected, min(
        leg["q_market"] for leg in quote.legs)), rel=1e-9)


def test_three_legs_and_team_totals_price():
    quote = price(build(), ML_HOME, FAV_COVER, TEAM_TOTAL)
    assert quote.reason_code == QUOTED_OK
    assert len(quote.legs) == 3
    # the Gaussian-tail blind spot is still explained, just no longer priced
    assert any("Tails" in e for e in quote.explanations)


def test_legs_from_another_game_multiply_in_as_independent():
    both = price(build(), ML_HOME, FAV_COVER, OTHER_GAME_ML)
    same_game = price(build(), ML_HOME, FAV_COVER)
    other_leg = next(leg for leg in both.legs if leg["position_id"] == OTHER_GAME_ML)
    assert other_leg["modeled"] is False
    assert both.fair_yes == pytest.approx(same_game.fair_yes * other_leg["q_market"], rel=1e-9)


def test_a_non_game_leg_is_independent_too():
    quote = price(build(), ML_HOME, FAV_COVER, POLITICS)
    assert quote.reason_code == QUOTED_OK
    assert any("independent" in e for e in quote.explanations)


# -- quote terms ------------------------------------------------------------

def test_bid_and_ask_straddle_fair_and_sit_on_the_tick():
    quote = price(build(), ML_HOME, FAV_COVER)
    assert quote.bid < quote.fair < quote.ask
    for p in (quote.bid, quote.ask):
        assert round(p / 0.001) == pytest.approx(p / 0.001, abs=1e-6)
    assert quote.spread_bps_total > 0
    # markup: half-spread = 2 x avg leg half-spread, total never over 100 bps
    comps = quote.components
    assert comps["half_spread_bps"] == pytest.approx(
        min(50.0, 2.0 * comps["avg_leg_half_spread_bps"]))
    assert comps["spread_bps_total"] <= 100.0


def test_the_response_side_follows_the_requester_direction():
    buy = price(build(), ML_HOME, FAV_COVER, direction="BUY")
    sell = price(build(), ML_HOME, FAV_COVER, direction="SELL")
    assert (buy.response_action, buy.response_price) == ("SELL", buy.ask)
    assert (sell.response_action, sell.response_price) == ("BUY", sell.bid)


def test_a_no_side_rfq_is_priced_at_one_minus_the_yes_fair():
    yes = price(build(), ML_HOME, FAV_COVER, side="YES")
    no = price(build(), ML_HOME, FAV_COVER, side="NO")
    assert no.fair == pytest.approx(1.0 - yes.fair_yes, abs=1e-6)
    assert no.fair_yes == pytest.approx(yes.fair_yes, abs=1e-9)
    assert no.bid < no.fair < no.ask


def test_cash_rfqs_are_sized_from_the_quoted_price():
    quote = price(build(), ML_HOME, FAV_COVER, qty=None, cash="100")
    assert quote.reason_code == QUOTED_OK
    assert int(quote.ask_qty) == int(100 / quote.ask)
    assert quote.size_unit == "notional"


def test_every_quote_names_the_params_file_it_priced_with():
    quote = price(build(), ML_HOME, FAV_COVER)
    assert quote.params_version.startswith("nfl_2026_w02.json@")
    assert quote.model_version.endswith("-live")
    assert quote.games[0]["params_game_row"] is True      # the slate row for BUF/DET


def test_calibration_is_cached_across_rfqs_on_one_game():
    pricer = build()
    price(pricer, ML_HOME, FAV_COVER)
    first = len(pricer._calibrations)
    price(pricer, ML_HOME, OVER)
    assert len(pricer._calibrations) == first == 1


# -- declines ---------------------------------------------------------------

def test_unknown_position_declines_rather_than_guessing():
    quote = price(build(), ML_HOME, "not-in-the-catalog")
    assert quote.reason_code == UNRESOLVED_LEG and quote.bid is None


@pytest.mark.parametrize("leg", [FIRST_HALF, PROP])
def test_unmodelable_leg_on_the_same_game_declines(leg):
    quote = price(build(), ML_HOME, leg)
    assert quote.reason_code == UNSUPPORTED_LEG
    assert "not a final-score leg" in quote.reason_detail


def test_cross_game_only_combo_has_no_correlation_to_price():
    quote = price(build(), ML_HOME, OTHER_GAME_ML)
    assert quote.reason_code == NO_NFL_SAME_GAME


def test_two_legs_on_a_non_nfl_game_decline():
    catalog = ComboMarketCatalog()
    page = catalog_payload()
    page["markets"] += [{
        "id": "7000001", "slug": "epl-ars-che-2026-09-20", "title": "Arsenal vs. Chelsea",
        "outcomes": ["Arsenal", "Chelsea"], "outcome_prices": ["0.55", "0.45"],
        "tags": ["sports", "epl", "games"], "condition_id": "0x7", "pending": False,
        "position_ids": ["7000001-0", "7000001-1"]}, {
        "id": "7000002", "slug": "epl-ars-che-2026-09-20-total-2pt5", "title": "O/U 2.5",
        "outcomes": ["Over", "Under"], "outcome_prices": ["0.5", "0.5"],
        "tags": ["sports", "epl", "games"], "condition_id": "0x8", "pending": False,
        "position_ids": ["7000002-0", "7000002-1"]}]
    catalog.merge(parse_catalog_page(page))
    pricer = NflLivePricer(catalog, StubBooks(now_ms=NOW_MS), ParamsProvider(PARAMS_DIR))
    quote = pricer.price(LiveRfq(rfq_id="R", leg_position_ids=("7000001-0", "7000002-0"),
                                 qty_decimal="10"), now=NOW)
    assert quote.reason_code == OTHER_SAME_GAME


def test_impossible_combo_declines_instead_of_quoting_zero():
    quote = price(build(), ML_AWAY, FAV_COVER)     # Lions win AND Bills cover -4.5
    assert quote.reason_code == CONTRADICTORY_LEGS
    assert quote.bid is None and quote.ask is None


def test_a_started_game_is_not_quoted():
    quote = price(build(), ML_HOME, FAV_COVER, now=datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc))
    assert quote.reason_code == GAME_STARTED


def test_missing_and_stale_leg_books_decline():
    missing = build(StubBooks(now_ms=NOW_MS, missing=["3398287"]))
    assert price(missing, ML_HOME, FAV_COVER).reason_code == MISSING_LEG
    old = build(StubBooks(now_ms=NOW_MS - 60_000))
    assert price(old, ML_HOME, FAV_COVER).reason_code == STALE_LEG


def test_missing_calibration_market_declines():
    # No priced full-game total for the game: the score distribution is unpinned.
    books = StubBooks(now_ms=NOW_MS, missing=["3517192", "3517193"])
    quote = price(build(books), ML_HOME, FAV_COVER)
    assert quote.reason_code == MISSING_CALIBRATION_MARKET
    assert "total" in quote.reason_detail


def test_params_must_exist_and_be_recent(tmp_path):
    assert price(build(params_dir=tmp_path), ML_HOME, FAV_COVER).reason_code == PARAMS_UNAVAILABLE
    late = NOW + timedelta(days=30)
    quote = price(build(), ML_HOME, FAV_COVER, now=late)
    assert quote.reason_code in (PARAMS_STALE, GAME_STARTED)
    quote = build().price(LiveRfq(rfq_id="R", leg_position_ids=(ML_HOME, FAV_COVER),
                                  qty_decimal="10"), now=late)
    assert quote.reason_code == PARAMS_STALE


def test_a_leg_the_model_and_market_disagree_on_declines():
    """A team total priced 0.45 against a ~0.61 model marginal is not ours to quote."""
    books = StubBooks(now_ms=NOW_MS, prices={"3517194": 0.45})
    quote = price(build(books), ML_HOME, FAV_COVER, TEAM_TOTAL)
    assert quote.reason_code == MODEL_MARKET_DISAGREE
    assert "model" in quote.reason_detail and "market" in quote.reason_detail


def test_a_decline_never_carries_a_price():
    for quote in (price(build(), ML_AWAY, FAV_COVER), price(build(), ML_HOME, FIRST_HALF)):
        assert quote.status == "DECLINED"
        assert (quote.bid, quote.ask, quote.response_price) == (None, None, None)


def test_pricing_is_fast_once_warm():
    pricer = build()
    price(pricer, ML_HOME, FAV_COVER)                      # pays imports + first integration
    quote = price(pricer, ML_HOME, OVER)
    assert quote.latency_ms < 100.0                        # CI-safe bound; ~2ms in practice

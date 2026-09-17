"""Settling stored live quotes against final scores (``docs/settlement-tracking.md``)."""
from __future__ import annotations

import pytest

from combo_mm.combo_markets import ComboMarketCatalog, parse_catalog_page
from combo_mm.nfl.ingest import Game
from combo_mm.nfl.markets import ML, NflLegMarket
from combo_mm.nfl.settle_live import (
    PENDING,
    SETTLED,
    TERMINAL,
    UNRESOLVED,
    UNSETTLEABLE,
    VOID,
    GameIndex,
    find_game,
    settle_quote,
)
from combo_mm import quote_selections
from combo_mm.quote_selections import QuoteSelectionStore
from tests.nfl_live_fixtures import GAME, catalog_payload, position

SLUG_SPR_HOME = f"{GAME}-spread-home-4pt5"   # Bills (home) -4.5
SLUG_TOT = f"{GAME}-total-54pt5"
SLUG_TT = f"{GAME}-team-total-buf-27pt5"
NOW = "2026-09-19T12:00:00Z"


def make_game(**kw) -> Game:
    """DET at BUF on 2026-09-18. BUF 30 DET 20 by default: Bills cover, under, BUF over 27.5."""
    base = dict(
        game_id="2026_02_DET_BUF", season=2026, week=2, game_type="REG",
        gameday="2026-09-18", home="BUF", away="DET", home_score=30, away_score=20,
        overtime=False, neutral=False, spread_line=-4.5, total_line=54.5, gametime="20:15",
    )
    base.update(kw)
    return Game(**base)


@pytest.fixture
def resolve():
    catalog = ComboMarketCatalog()
    catalog.merge(parse_catalog_page(catalog_payload()))
    return catalog.lookup


def quote(*legs, side="YES", fair=0.30, naive=0.25, bid=0.27, ask=0.33, rfq_id="rfq-1"):
    """A stored ``priced_quotes`` row, shaped as ``list_priced_quotes`` returns it."""
    return {
        "rfq_id": rfq_id, "trigger": "auto", "status": "QUOTED", "reason_code": "QUOTED_OK",
        "priced_at": "2026-09-17T22:00:00Z", "side": side, "direction": "BUY",
        "fair": fair, "naive": naive, "bid": bid, "ask": ask,
        "model_version": "nfl-live-1", "params_version": "nfl_2026_w02.json",
        "detail": {"legs": [{"position_id": p, "label": f"leg {p}"} for p in legs]},
    }


def settle(q, resolve, games=None, **kw):
    index = GameIndex(games if games is not None else [make_game()])
    return settle_quote(q, resolve, index, settled_at=NOW, **kw)


# -- the combo fold ----------------------------------------------------------

def test_every_leg_wins_settles_to_one(resolve):
    # Bills ML, Bills -4.5, BUF over 27.5 -- all hit on BUF 30 DET 20.
    out = settle(quote(position(GAME, 1), position(SLUG_SPR_HOME, 0), position(SLUG_TT, 0)),
                 resolve)
    assert out.status == SETTLED
    assert out.combo_value == 1.0
    assert out.n_legs == 3 and out.n_legs_settled == 3
    assert [leg.value for leg in out.legs] == [1.0, 1.0, 1.0]


def test_one_losing_leg_settles_the_combo_to_zero(resolve):
    # Lions ML loses; the other two hit.
    out = settle(quote(position(GAME, 0), position(SLUG_SPR_HOME, 0)), resolve)
    assert out.status == SETTLED
    assert out.combo_value == 0.0


def test_no_side_of_a_total_is_inverted(resolve):
    # Under 54.5 is outcome 1 (side NO). BUF 30 DET 20 totals 50, so the under hits.
    out = settle(quote(position(SLUG_TOT, 1)), resolve)
    assert out.legs[0].side == "NO"
    assert out.legs[0].settlement_price == "0"   # raw YES value: the over missed
    assert out.legs[0].value == 1.0              # the position we held paid
    assert out.combo_value == 1.0


def test_requested_no_side_inverts_the_combo(resolve):
    out = settle(quote(position(GAME, 1), side="NO"), resolve)
    assert out.combo_yes == 1.0
    assert out.combo_value == 0.0                # we were asked about NO; the combo hit


def test_a_tie_voids_the_ml_leg_and_the_whole_combo(resolve):
    tied = make_game(home_score=24, away_score=24)
    out = settle(quote(position(GAME, 1), position(SLUG_TOT, 1)), resolve, games=[tied])
    assert out.status == VOID
    assert out.combo_value is None
    assert out.brier is None                     # a void quote is never scored


def test_void_outranks_a_leg_we_could_not_resolve(resolve):
    # One leg voids on the tie; the other is unknown. Void is absorbing.
    tied = make_game(home_score=24, away_score=24)
    out = settle(quote(position(GAME, 1), "not-a-position"), resolve, games=[tied])
    assert out.status == VOID


# -- pending, unresolved -----------------------------------------------------

def test_unplayed_game_is_pending_not_a_loss(resolve):
    unplayed = make_game(home_score=None, away_score=None)
    out = settle(quote(position(GAME, 1)), resolve, games=[unplayed])
    assert out.status == PENDING
    assert out.combo_value is None


def test_unknown_position_is_unresolved_and_names_the_id(resolve):
    out = settle(quote("position-we-never-saw"), resolve)
    assert out.status == UNRESOLVED
    assert "position-we-never-saw" in out.reason_detail


def test_period_market_is_unsettleable_not_unresolved(resolve):
    # A first-half total is in the catalog; no final score will ever settle it.
    out = settle(quote(position(f"{GAME}-1h-total-28pt5", 0)), resolve)
    assert out.status == UNSETTLEABLE
    assert out.is_terminal                     # never retried
    assert "period market" in out.reason_detail


def test_player_prop_leg_is_unsettleable(resolve):
    out = settle(quote(position(f"{GAME}-anytime-td-josh-allen", 0)), resolve)
    assert out.status == UNSETTLEABLE
    assert "prop" in out.reason_detail


def test_non_nfl_leg_riding_along_is_unsettleable(resolve):
    # The screen allows a politics leg through as an independent multiplier.
    out = settle(quote(position(GAME, 1),
                       position("will-the-us-invade-iran-before-2027", 0)), resolve)
    assert out.status == UNSETTLEABLE
    assert "not an NFL game market" in out.reason_detail


def test_unsettleable_outranks_unresolved_but_not_void(resolve):
    both = quote(position(f"{GAME}-anytime-td-josh-allen", 0), "unknown-position")
    assert settle(both, resolve).status == UNSETTLEABLE
    tied = make_game(home_score=24, away_score=24)
    with_void = quote(position(GAME, 1), position(f"{GAME}-anytime-td-josh-allen", 0))
    assert settle(with_void, resolve, games=[tied]).status == VOID


def test_terminal_statuses_match_the_store(resolve):
    assert quote_selections.TERMINAL == TERMINAL


def test_quote_without_legs_is_unresolved(resolve):
    q = quote()
    q["detail"] = {"legs": []}
    assert settle(q, resolve).status == UNRESOLVED


# -- the score join ----------------------------------------------------------

def market(**kw) -> NflLegMarket:
    base = dict(symbol="s", game_id="g", season=2026, week=0,
                kickoff_utc="2026-09-18T00:15:00Z", home="BUF", away="DET",
                kind=ML, subject="BUF", line=None)
    base.update(kw)
    return NflLegMarket(**base)


def test_join_tolerates_one_day_of_kickoff_drift():
    # The slug says the 18th; nflverse's ET gameday says the 17th.
    game, reason = find_game(GameIndex([make_game(gameday="2026-09-17")]), market())
    assert reason == "" and game is not None


def test_join_rejects_a_game_a_week_away():
    game, reason = find_game(GameIndex([make_game(gameday="2026-09-25")]), market())
    assert game is None
    assert "no kickoff within" in reason


def test_join_maps_relocated_franchises():
    # nflverse maps the CSV's OAK to LV; the slug still says OAK.
    game, reason = find_game(GameIndex([make_game(away="LV", home="SF", gameday="2026-09-18")]),
                             market(away="OAK", home="SF", subject="SF"))
    assert reason == "" and game is not None


def test_two_candidate_games_refuse_to_guess():
    twice = [make_game(game_id="a"), make_game(game_id="b")]
    game, reason = find_game(GameIndex(twice), market())
    assert game is None
    assert "refusing to guess" in reason


def test_missing_game_says_so():
    game, reason = find_game(GameIndex([]), market())
    assert game is None
    assert "in the pull" in reason


# -- scoring -----------------------------------------------------------------

def test_brier_scores_model_against_naive(resolve):
    # The combo hits (1.0). fair 0.40 is closer than naive 0.25, so the model wins.
    out = settle(quote(position(GAME, 1), fair=0.40, naive=0.25, bid=0.37, ask=0.43), resolve)
    assert out.combo_value == 1.0
    assert out.brier == pytest.approx(0.36)
    assert out.naive_brier == pytest.approx(0.5625)
    assert out.edge_vs_naive == pytest.approx(0.2025)
    assert out.hypo_edge_bid == pytest.approx(0.63)    # bought at 0.37, worth 1.0
    assert out.hypo_edge_ask == pytest.approx(-0.57)   # sold at 0.43, worth 1.0


def test_the_metric_can_say_the_model_lost(resolve):
    # The combo misses. naive 0.25 was closer to 0 than fair 0.40.
    out = settle(quote(position(GAME, 0), fair=0.40, naive=0.25), resolve)
    assert out.combo_value == 0.0
    assert out.edge_vs_naive == pytest.approx(-0.0975)
    assert out.edge_vs_naive < 0


def test_realized_pnl_only_when_a_fill_exists(resolve):
    q = quote(position(GAME, 1))
    assert settle(q, resolve).realized_pnl is None
    out = settle(q, resolve, fill={"price": 0.33, "size": 25, "direction": "BUY"})
    assert out.realized_pnl == pytest.approx(16.75)   # (1.0 - 0.33) * 25


# -- the store ---------------------------------------------------------------

def test_store_round_trip_is_idempotent(resolve):
    store = QuoteSelectionStore(":memory:")
    legs = [{"position_id": position(GAME, 1), "label": "Bills ML"}]
    store.record_priced_quote(
        {"rfq_id": "rfq-1", "priced_at": "2026-09-17T22:00:00Z", "status": "QUOTED",
         "reason_code": "QUOTED_OK", "side": "YES", "fair": 0.4, "naive": 0.25,
         "bid": 0.37, "ask": 0.43, "legs": legs}, "auto")

    due = store.quotes_needing_settlement()
    assert len(due) == 1
    index = GameIndex([make_game()])
    for _ in range(2):
        out = settle_quote(due[0], resolve, index, settled_at=NOW)
        store.record_settlement(out.to_dict())

    rows = store.list_settlements()
    assert len(rows) == 1                       # replaced, not appended
    assert rows[0]["status"] == SETTLED
    assert rows[0]["combo_value"] == 1.0
    assert rows[0]["legs"][0]["position_id"] == position(GAME, 1)
    assert store.quotes_needing_settlement() == []   # terminal rows are not retried
    store.close()


def test_pending_is_retried_and_flips_once_the_score_lands(resolve):
    store = QuoteSelectionStore(":memory:")
    store.record_priced_quote(
        {"rfq_id": "rfq-1", "priced_at": "2026-09-17T22:00:00Z", "status": "QUOTED",
         "reason_code": "QUOTED_OK", "side": "YES", "fair": 0.4, "naive": 0.25,
         "legs": [{"position_id": position(GAME, 1)}]}, "auto")

    due = store.quotes_needing_settlement()
    unplayed = GameIndex([make_game(home_score=None, away_score=None)])
    store.record_settlement(settle_quote(due[0], resolve, unplayed, settled_at=NOW).to_dict())
    assert store.settlement_stats() == {PENDING: 1}

    due = store.quotes_needing_settlement()     # pending comes back round
    assert len(due) == 1
    played = GameIndex([make_game()])
    store.record_settlement(settle_quote(due[0], resolve, played, settled_at=NOW).to_dict())
    assert store.settlement_stats() == {SETTLED: 1}
    store.close()


def test_declined_quotes_are_never_scored():
    store = QuoteSelectionStore(":memory:")
    store.record_priced_quote(
        {"rfq_id": "rfq-2", "priced_at": "2026-09-17T22:00:00Z", "status": "DECLINED",
         "reason_code": "NO_BOOK", "legs": []}, "auto")
    assert store.quotes_needing_settlement() == []
    store.close()


def test_settlement_metrics_aggregate_the_pair(resolve):
    store = QuoteSelectionStore(":memory:")
    index = GameIndex([make_game()])
    for i, legs in enumerate([[position(GAME, 1)], [position(GAME, 0)]]):
        store.record_priced_quote(
            {"rfq_id": f"rfq-{i}", "priced_at": "2026-09-17T22:00:00Z", "status": "QUOTED",
             "reason_code": "QUOTED_OK", "side": "YES", "fair": 0.4, "naive": 0.25,
             "legs": [{"position_id": p} for p in legs]}, "auto")
    for q in store.quotes_needing_settlement():
        store.record_settlement(settle_quote(q, resolve, index, settled_at=NOW).to_dict())

    m = store.settlement_metrics()
    assert m["n"] == 2
    assert m["hit_rate"] == pytest.approx(0.5)          # one hit, one miss
    assert m["brier"] == pytest.approx((0.36 + 0.16) / 2)
    assert m["naive_brier"] == pytest.approx((0.5625 + 0.0625) / 2)
    store.close()

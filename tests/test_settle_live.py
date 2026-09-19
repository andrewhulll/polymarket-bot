"""Settling priced live quotes: pure settlement logic plus the store rows.

stdlib plus pytest, no network, deterministic. The Game fixtures below are
final scores; the runner (scripts/settle_live_quotes.py) feeds these same
pure functions from the cached nflverse pull.
"""
import pytest

from combo_mm.nfl.ingest import Game
from combo_mm.nfl.settle_live import (
    fold_combo,
    join_game,
    resolve_leg,
    settle_leg_against,
    settle_quote,
    settlement_metrics,
)
from combo_mm.quote_selections import QuoteSelectionStore


def _game(game_id="2026_01_DET_BUF", season=2026, gameday="2026-09-13",
          away="DET", home="BUF", home_score=24, away_score=31):
    return Game(game_id=game_id, season=season, week=1, game_type="REG",
                gameday=gameday, home=home, away=away,
                home_score=home_score, away_score=away_score,
                overtime=False, neutral=False,
                spread_line=-2.5, total_line=45.5)


# Slugs use the catalog line grammar (3pt5 = 3.5). DET beats BUF 31-24.
CATALOG = {
    "pid-ml-det": ("nfl-det-buf-2026-09-13", 0),                      # DET moneyline (away wins)
    "pid-spread-det": ("nfl-det-buf-2026-09-13-spread-home-2pt5", 1),  # DET +2.5 (covers)
    "pid-over": ("nfl-det-buf-2026-09-13-total-45pt5", 0),             # over 45.5 (55 lands)
    "pid-under": ("nfl-det-buf-2026-09-13-total-45pt5", 1),            # under 45.5 (side NO)
    "pid-ml-buf": ("nfl-det-buf-2026-09-13", 1),                       # BUF moneyline (home loses)
    "pid-spread-push": ("nfl-det-buf-2026-09-13-spread-home-7pt0", 1),  # DET +7 on a 7-pt loss
}

GAMES = [_game()]


def _settle(rfq_id, pids, games=GAMES, catalog_index=None, **kw):
    args = dict(rfq_id=rfq_id, trigger="auto",
                legs=[{"position_id": p} for p in pids],
                fair=0.40, naive=0.30, bid=0.35, ask=0.45,
                model_version="m1", params_version="p1",
                catalog_index=(catalog_index if catalog_index is not None
                               else {p: CATALOG[p] for p in pids}),
                games=games, scores_vintage="2026-09-14")
    args.update(kw)
    return settle_quote(**args)


def test_all_legs_win_settles_to_one():
    row = _settle("r1", ["pid-ml-det", "pid-spread-det", "pid-over"])
    assert row["status"] == "SETTLED"
    assert row["combo_value"] == 1.0
    assert row["n_legs"] == 3 and row["n_legs_settled"] == 3
    assert [leg["settlement_price"] for leg in row["legs"]] == ["1", "1", "1"]
    assert row["legs"][0]["game_id"] == "2026_01_DET_BUF"


def test_one_leg_loses_settles_to_zero():
    row = _settle("r2", ["pid-ml-det", "pid-ml-buf"])
    assert row["status"] == "SETTLED"
    assert row["combo_value"] == 0.0
    by_pid = {leg["position_id"]: leg["settlement_price"] for leg in row["legs"]}
    assert by_pid == {"pid-ml-det": "1", "pid-ml-buf": "0"}


def test_no_side_leg_inverts_the_raw_yes_value():
    # Total lands 55: the over (YES) wins, the under (NO) loses. The raw
    # settlement is the YES value ("1"); the inversion is in `won`.
    row = _settle("r3", ["pid-under"])
    assert row["status"] == "SETTLED"
    assert row["combo_value"] == 0.0
    assert row["legs"][0]["settlement_price"] == "1"
    assert row["legs"][0]["side"] == "NO"


def test_pushed_leg_voids_the_combo_with_no_edges():
    # BUF beats DET by exactly 7: the DET +7 leg pushes and voids the combo.
    push_game = _game(game_id="2026_01_DET_BUF", home_score=31, away_score=24)
    row = _settle("r4", ["pid-ml-buf", "pid-spread-push"], games=[push_game])
    assert row["status"] == "VOID"
    assert row["combo_value"] is None
    assert row["brier"] is None and row["naive_brier"] is None
    assert row["edge_vs_naive"] is None
    assert row["hypo_edge_bid"] is None and row["hypo_edge_ask"] is None
    assert "push" in row["reason_detail"]


def test_idempotent_store_write_is_byte_identical():
    store = QuoteSelectionStore()
    row = _settle("r5", ["pid-ml-det", "pid-over"])
    store.record_quote_settlement(row)
    first = store.get_quote_settlement("r5", "auto")
    store.record_quote_settlement(_settle("r5", ["pid-ml-det", "pid-over"]))
    second = store.get_quote_settlement("r5", "auto")
    assert len(store.list_quote_settlements()) == 1
    assert first == second  # settled_at keeps the first computation


def test_pending_flips_to_settled_on_rerun_with_newer_scores():
    monday = _game(game_id="2026_01_KC_LV", gameday="2026-09-14", away="KC", home="LV",
                   home_score=None, away_score=None)
    catalog = {"pid-kc": ("nfl-kc-lv-2026-09-14", 0)}
    pending = settle_quote(rfq_id="r6", trigger="auto",
                           legs=[{"position_id": "pid-kc"}],
                           fair=0.55, naive=0.50, bid=0.50, ask=0.60,
                           model_version="m1", params_version="p1",
                           catalog_index=catalog, games=[monday],
                           scores_vintage="2026-09-14")
    assert pending["status"] == "PENDING"
    assert pending["combo_value"] is None
    assert "not played yet" in pending["reason_detail"]

    played = _game(game_id="2026_01_KC_LV", gameday="2026-09-14", away="KC", home="LV",
                   home_score=17, away_score=24)
    store = QuoteSelectionStore()
    store.record_quote_settlement(pending)
    settled = settle_quote(rfq_id="r6", trigger="auto",
                           legs=[{"position_id": "pid-kc"}],
                           fair=0.55, naive=0.50, bid=0.50, ask=0.60,
                           model_version="m1", params_version="p1",
                           catalog_index=catalog, games=[played],
                           scores_vintage="2026-09-15")
    store.record_quote_settlement(settled)
    rows = store.list_quote_settlements()
    assert len(rows) == 1
    assert rows[0]["status"] == "SETTLED"
    assert rows[0]["combo_value"] == 1.0
    assert rows[0]["scores_vintage"] == "2026-09-15"


def test_franchise_alias_joins():
    # The catalog slug still says OAK; nflverse has the relocated LV.
    game = _game(game_id="2026_01_OAK_KC", gameday="2026-09-13", away="LV", home="KC",
                 home_score=30, away_score=27)
    catalog = {"pid-oak": ("nfl-oak-kc-2026-09-13", 0)}
    res = resolve_leg("pid-oak", catalog)
    assert res.resolved and res.away == "LV"
    join = join_game(res, [game])
    assert join.status == "found" and join.game.game_id == "2026_01_OAK_KC"


def test_gameday_drift_within_one_day_joins():
    # Slug rendered in a different timezone than nflverse gameday (ET).
    game = _game(gameday="2026-09-14")
    catalog = {"pid-ml-det": ("nfl-det-buf-2026-09-13", 0)}
    res = resolve_leg("pid-ml-det", catalog)
    join = join_game(res, [game])
    assert join.status == "found"


def test_two_candidate_games_is_unresolved_not_a_guess():
    games = [_game(game_id="g1", gameday="2026-09-12"),
             _game(game_id="g2", gameday="2026-09-14")]
    row = _settle("r7", ["pid-ml-det"], games=games)
    assert row["status"] == "UNRESOLVED"
    assert "ambiguous" in row["reason_detail"]
    assert "g1" in row["reason_detail"] and "g2" in row["reason_detail"]
    assert row["combo_value"] is None


def test_missing_position_id_is_unresolved_with_the_id_in_detail():
    row = settle_quote(rfq_id="r8", trigger="auto",
                       legs=[{"position_id": "pid-ghost"}, {"position_id": "pid-ml-det"}],
                       fair=0.40, naive=0.30, bid=0.35, ask=0.45,
                       model_version="m1", params_version="p1",
                       catalog_index={"pid-ml-det": CATALOG["pid-ml-det"]},
                       games=GAMES, scores_vintage="2026-09-14")
    assert row["status"] == "UNRESOLVED"
    assert "pid-ghost" in row["reason_detail"]
    assert row["combo_value"] is None


def test_unsupported_market_is_unresolved_not_a_guess():
    # A period market joins the right game but cannot settle on a final score.
    row = _settle("r9", ["pid-q1"],
                  catalog_index={"pid-q1": ("nfl-det-buf-2026-09-13-1q-spread-home-0pt5", 0)})
    assert row["status"] == "UNRESOLVED"
    assert "period market" in row["reason_detail"]


def test_brier_arithmetic_against_hand_computed_values():
    m = settlement_metrics(fair=0.62, naive=0.55, combo_value=1.0, bid=0.50, ask=0.70)
    assert m["brier"] == pytest.approx((0.62 - 1.0) ** 2)
    assert m["naive_brier"] == pytest.approx((0.55 - 1.0) ** 2)
    assert m["edge_vs_naive"] == pytest.approx(m["naive_brier"] - m["brier"])
    assert m["edge_vs_naive"] > 0  # the joint model won
    assert m["hypo_edge_bid"] == pytest.approx(1.0 - 0.50)
    assert m["hypo_edge_ask"] == pytest.approx(0.70 - 1.0)


def test_metric_can_say_the_naive_price_beat_the_model():
    m = settlement_metrics(fair=0.30, naive=0.55, combo_value=1.0, bid=0.25, ask=0.35)
    assert m["brier"] == pytest.approx(0.49)
    assert m["naive_brier"] == pytest.approx(0.2025)
    assert m["edge_vs_naive"] == pytest.approx(-0.2875)
    assert m["edge_vs_naive"] < 0  # we lost to the naive product


def test_fold_combo_convention():
    won = settle_leg_against(
        resolve_leg("pid-ml-det", {"pid-ml-det": CATALOG["pid-ml-det"]}), GAMES[0])
    lost = settle_leg_against(
        resolve_leg("pid-ml-buf", {"pid-ml-buf": CATALOG["pid-ml-buf"]}), GAMES[0])
    assert fold_combo([won]) == ("SETTLED", 1.0)
    assert fold_combo([won, lost]) == ("SETTLED", 0.0)
    assert fold_combo([lost]) == ("SETTLED", 0.0)
    # BUF wins by exactly 7: DET +7 pushes.
    push_game = _game(home_score=31, away_score=24)
    pushed = settle_leg_against(
        resolve_leg("pid-spread-push", {"pid-spread-push": CATALOG["pid-spread-push"]}),
        push_game)
    assert pushed.void
    assert fold_combo([won, pushed]) == ("VOID", None)


def test_quotes_needing_settlement_skips_terminal_and_declines():
    store = QuoteSelectionStore()

    def priced(rfq_id, status="QUOTED"):
        return {"rfq_id": rfq_id, "trigger": "auto", "priced_at": "2026-09-14T01:00:00Z",
                "status": status, "reason_code": "QUOTED_OK",
                "fair": 0.4, "naive": 0.3, "bid": 0.35, "ask": 0.45,
                "legs": [{"position_id": "pid-ml-det"}]}

    store.record_priced_quote(priced("q-settled"))
    store.record_priced_quote(priced("q-pending"))
    store.record_priced_quote(priced("q-fresh"))
    store.record_priced_quote(priced("q-declined", status="DECLINED"))
    settled = _settle("q-settled", ["pid-ml-det"])
    pending = dict(_settle("q-fresh", ["pid-ml-det"]), status="PENDING")
    store.record_quote_settlement(settled)
    store.record_quote_settlement(dict(pending, rfq_id="q-pending"))

    due = {q["rfq_id"] for q in store.quotes_needing_settlement()}
    assert due == {"q-pending", "q-fresh"}  # settled done, declines never scored
    assert store.quotes_needing_settlement(since="2026-09-15T00:00:00Z") == []

"""One-week NFL RFQ replay (dashboard backtest) + NFL joint pricer."""
import dataclasses

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")

from combo_mm.nfl.estimate import EstimatorConfig  # noqa: E402
from combo_mm.nfl.joint import GameModel, home_cover, home_ml, over  # noqa: E402
from combo_mm.nfl.joint_pricer import CROSS_GAME, UNMODELED_LEG, NflJointPricer  # noqa: E402
from combo_mm.nfl.params_io import MatchupCovariance  # noqa: E402
from combo_mm.nfl.synthetic_backtest import COMBOS  # noqa: E402
from combo_mm.nfl.week_backtest import run_week_backtest  # noqa: E402
from combo_mm.pricing import QUOTED_OK, LegMarkInput, price_combo  # noqa: E402
from combo_mm.store import EventStore  # noqa: E402
from nfl_synthetic import make_games  # noqa: E402

SEASON, WEEK = 2014, 3
ESTIMATOR = EstimatorConfig(variance_model="league_constant", window_seasons=4, min_seasons=3)


@pytest.fixture(scope="module")
def games():
    return make_games(seasons=range(2010, 2015), weeks=6, seed=33)


@pytest.fixture(scope="module")
def run(games, tmp_path_factory):
    db = str(tmp_path_factory.mktemp("wk") / "bt.db")
    return run_week_backtest(games, season=SEASON, week=WEEK, estimator=ESTIMATOR, db_path=db)


def test_every_combo_becomes_an_rfq_and_is_decided(run, games):
    week_games = [g for g in games if (g.season, g.week) == (SEASON, WEEK) and g.spread_line != 0]
    assert run.meta["n_games"] == len(week_games)
    assert len(run.trades) == len(week_games) * len(COMBOS)
    assert run.result.rfqs_received == len(run.trades)
    assert run.result.rfqs_quoted + run.result.rfqs_rejected == len(run.trades)


def test_fills_executions_and_pnl_are_consistent(run):
    won = [t for t in run.trades if t["won"]]
    assert won, "the joint model should beat the naive maker on some combos"
    assert run.result.n_fills == len(won) == run.result.rfqs_executed
    assert run.result.realized_pnl == pytest.approx(sum(t["pnl"] or 0.0 for t in won))
    assert run.result.expected_pnl == pytest.approx(sum(t["expected_pnl"] for t in won))
    for t in run.trades:
        if t["outcome"] == "void" and t["won"]:
            assert t["pnl"] == 0.0


def test_trades_only_when_strictly_better_than_naive_maker(run):
    for t in run.trades:
        if t["requester_side"] == "BUY":
            better = t["our_buy"] and (t["comp_buy"] is None or t["our_buy"] < t["comp_buy"])
        else:
            better = t["our_sell"] and (t["comp_sell"] is None or t["our_sell"] > t["comp_sell"])
        assert t["won"] == bool(better), t["rfq_id"]


def test_rfq_lifecycle_and_settlement_in_store(run):
    store = EventStore(run.db_path)
    try:
        for t in run.trades[:40]:
            rfq = store.get_rfq(t["rfq_id"])
            assert rfq["status"] == ("EXECUTED" if t["won"] else "CLOSED")
            settled = [leg["settlement_price"] for leg in rfq["legs"]]
            if t["outcome"] == "void":
                assert None in settled
            else:
                assert None not in settled
                assert (all(s == 1.0 for s in settled)) == (t["outcome"] == "win")
    finally:
        store.close()


def test_deterministic(games, run, tmp_path):
    again = run_week_backtest(games, season=SEASON, week=WEEK, estimator=ESTIMATOR,
                              db_path=str(tmp_path / "again.db"))
    assert again.trades == run.trades
    first, second = EventStore(run.db_path), EventStore(again.db_path)
    try:
        assert first.state_digest() == second.state_digest()
    finally:
        first.close()
        second.close()


def test_quotes_do_not_depend_on_the_weeks_results(games, run):
    """No future information: rescoring the target week changes settlement, never prices."""
    flipped = [dataclasses.replace(g, home_score=g.away_score, away_score=g.home_score)
               if (g.season, g.week) == (SEASON, WEEK) else g for g in games]
    other = run_week_backtest(flipped, season=SEASON, week=WEEK, estimator=ESTIMATOR)
    for a, b in zip(run.trades, other.trades):
        for key in ("model_fair", "naive_fair", "our_buy", "our_sell", "comp_buy", "comp_sell", "won"):
            assert a[key] == b[key], (a["rfq_id"], key)


def test_no_week_raises(games):
    with pytest.raises(ValueError):
        run_week_backtest(games, season=2030, week=1, estimator=ESTIMATOR)


# ---------------------------------------------------------------------------
# Joint pricer + fair_override
# ---------------------------------------------------------------------------

def _book(symbol, p):
    return LegMarkInput(symbol=symbol, side="YES", bid=p - 0.005, ask=p + 0.005,
                        bid_size=1000, ask_size=1000)


@pytest.fixture()
def pricer():
    cov = MatchupCovariance(sigma_home=13.0, sigma_away=12.0, rho=0.05)
    pricer = NflJointPricer()
    pricer.register_game("G1", GameModel((25.0, 20.0), cov),
                         {"G1-ML": home_ml(), "G1-SPR": home_cover(5.0), "G1-O": over(44.5)})
    pricer.register_game("G2", GameModel((22.0, 22.0), cov), {"G2-O": over(43.5)})
    return pricer


def test_joint_pricer_centers_on_model_joint(pricer):
    legs = [_book("G1-ML", 0.62), _book("G1-SPR", 0.5)]
    res = pricer.price(legs, rfq_id="r1", qty_decimal="10")
    fair, _ = pricer.joint(["G1-ML", "G1-SPR"])
    assert res.quotable and res.fair_value == pytest.approx(fair)
    assert res.naive_product == pytest.approx(0.62 * 0.5, abs=1e-3)
    # Covering implies winning: the joint is far above the independent product.
    assert res.corr_adjustment_bps > 1000
    assert res.extra["sell_price"] <= fair <= res.extra["buy_price"]


def test_joint_pricer_declines_unmodeled_and_cross_game(pricer):
    assert pricer.price([_book("G1-ML", 0.6), _book("X", 0.5)], rfq_id="r",
                        qty_decimal="10").unquotable_reason == UNMODELED_LEG
    assert pricer.price([_book("G1-ML", 0.6), _book("G2-O", 0.5)], rfq_id="r",
                        qty_decimal="10").unquotable_reason == CROSS_GAME
    no_side = dataclasses.replace(_book("G1-ML", 0.6), side="NO")
    assert pricer.price([no_side, _book("G1-O", 0.5)], rfq_id="r",
                        qty_decimal="10").unquotable_reason == UNMODELED_LEG


def test_price_combo_fair_override_keeps_spread_and_records_naive():
    legs = [_book("A", 0.6), _book("B", 0.5)]
    base = price_combo(legs, rfq_id="r", qty_decimal="10")
    over_ = price_combo(legs, rfq_id="r", qty_decimal="10", fair_override=0.4)
    assert base.reason_code == over_.reason_code == QUOTED_OK
    assert over_.fair == 0.4 and over_.components["naive_fair"] == pytest.approx(base.fair)
    assert over_.half_spread == pytest.approx(base.half_spread)
    assert over_.sell_price <= 0.4 <= over_.buy_price

"""NFL leg registry (#15): slug grammar, joint-leg mapping, settlement."""
import pytest

from combo_mm.combo_markets import ComboMarketCatalog, parse_catalog_page
from combo_mm.nfl import joint
from combo_mm.nfl.markets import (
    ML,
    SPR,
    TOT,
    TT,
    parse_leg,
    parse_nfl_slug,
    to_joint_leg,
    unsupported_reason,
)
from tests.nfl_live_fixtures import GAME, catalog_payload, position

SLUG_ML = GAME
SLUG_SPR_HOME = f"{GAME}-spread-home-4pt5"       # Bills (home) -4.5
SLUG_SPR_AWAY = f"{GAME}-spread-away-13pt5"      # Lions (away) -13.5
SLUG_TOT = f"{GAME}-total-54pt5"
SLUG_TT = f"{GAME}-team-total-buf-27pt5"


def test_slug_parses_away_home_and_date():
    assert parse_nfl_slug(SLUG_SPR_HOME) == (GAME, "DET", "BUF", "spread-home-4pt5")
    assert parse_nfl_slug("nfl-nyg-la-2026-09-22")[1:3] == ("NYG", "LA")
    assert parse_nfl_slug("will-the-us-invade-iran-before-2027") is None


@pytest.mark.parametrize("slug, kind, line", [
    (SLUG_ML, ML, None),
    (SLUG_SPR_HOME, SPR, 4.5),
    (SLUG_SPR_AWAY, SPR, 13.5),
    (SLUG_TOT, TOT, 54.5),
    (SLUG_TT, TT, 27.5),
])
def test_kinds_and_lines(slug, kind, line):
    market = parse_leg(slug, 0)
    assert (market.kind, market.line) == (kind, line)
    assert (market.home, market.away) == ("BUF", "DET")


def test_unsupported_markets_are_named_not_guessed():
    for slug in (f"{GAME}-1h-total-28pt5", f"{GAME}-1q-spread-home-2pt5"):
        assert parse_leg(slug, 0) is None
        assert "period market" in unsupported_reason(slug)
    prop = f"{GAME}-anytime-td-josh-allen"
    assert parse_leg(prop, 0) is None
    assert "Phase B" in unsupported_reason(prop)
    assert unsupported_reason(SLUG_ML) is None
    assert unsupported_reason("will-the-us-invade-iran-before-2027") == "not an NFL game market"


@pytest.mark.parametrize("slug, outcome, name", [
    (SLUG_ML, 0, "away_ml"),                  # outcome 0 is the away team (slug order)
    (SLUG_ML, 1, "home_ml"),
    (SLUG_SPR_HOME, 0, "home_cover(4.5)"),    # Bills -4.5: home margin > 4.5
    (SLUG_SPR_HOME, 1, "away_cover(4.5)"),    # Lions +4.5
    (SLUG_SPR_AWAY, 0, "away_cover(-13.5)"),  # Lions -13.5: home margin < -13.5
    (SLUG_SPR_AWAY, 1, "home_cover(-13.5)"),  # Bills +13.5
    (SLUG_TOT, 0, "over(54.5)"),
    (SLUG_TOT, 1, "under(54.5)"),
    (SLUG_TT, 0, "home_team_over(27.5)"),     # BUF is the home team
    (SLUG_TT, 1, "home_team_under(27.5)"),
])
def test_joint_leg_mapping(slug, outcome, name):
    assert to_joint_leg(parse_leg(slug, outcome)).name == name


# BUF (home) 31, DET (away) 24: margin +7, total 55.
@pytest.mark.parametrize("slug, outcome, result", [
    (SLUG_ML, 0, joint.LOSE),                 # Lions lose
    (SLUG_ML, 1, joint.WIN),                  # Bills win
    (SLUG_SPR_HOME, 0, joint.WIN),            # Bills -4.5 covers (+7)
    (SLUG_SPR_HOME, 1, joint.LOSE),
    (SLUG_SPR_AWAY, 0, joint.LOSE),           # Lions -13.5 does not cover
    (SLUG_SPR_AWAY, 1, joint.WIN),            # Bills +13.5
    (SLUG_TOT, 0, joint.WIN),                 # over 54.5 (55)
    (SLUG_TOT, 1, joint.LOSE),
    (SLUG_TT, 0, joint.WIN),                  # Bills over 27.5 (31)
    (SLUG_TT, 1, joint.LOSE),
])
def test_settlement_against_a_hand_scored_game(slug, outcome, result):
    assert joint.settle_leg(to_joint_leg(parse_leg(slug, outcome)), 31, 24) == result


def test_moneyline_ties_push_and_integer_totals_push():
    assert joint.settle_leg(to_joint_leg(parse_leg(SLUG_ML, 1)), 20, 20) == joint.PUSH
    integer_total = parse_leg(f"{GAME}-total-54pt5", 0)
    assert not to_joint_leg(integer_total).pushable       # half-point line cannot push
    assert to_joint_leg(parse_leg(SLUG_ML, 0)).pushable   # moneyline can (a tie)


def test_catalog_indexes_markets_by_game():
    catalog = ComboMarketCatalog()
    catalog.merge(parse_catalog_page(catalog_payload()))
    on_game = catalog.markets_for_game(GAME)
    assert len(on_game) == 18                                   # 9 markets x 2 outcomes
    assert all(m.game == GAME for m in on_game)
    assert catalog.markets_for_game("nfl-nope-nope-2026-01-01") == []
    resolved = catalog.resolve([position(SLUG_TOT, 0), "unknown-position"])
    assert resolved[0].slug == SLUG_TOT and resolved[1] is None

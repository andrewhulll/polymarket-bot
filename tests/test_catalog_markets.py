"""Live Polymarket combo legs -> the NFL leg registry (#15 Part A) and the joint model."""
import pytest

from combo_mm.combo_markets import ComboMarketCatalog, parse_catalog_page
from combo_mm.nfl import joint
from combo_mm.nfl.catalog_markets import (
    describe_leg,
    parse_catalog_leg,
    parse_game_slug,
    season_of,
    unsupported_reason,
)
from combo_mm.nfl.markets import ML, SPR, TOT, TT, settlement_price, to_joint_leg
from tests.nfl_live_fixtures import GAME, catalog_payload, position

SLUG_ML = GAME                                   # DET (away) at BUF (home)
SLUG_SPR_HOME = f"{GAME}-spread-home-4pt5"       # Bills (home) -4.5
SLUG_SPR_AWAY = f"{GAME}-spread-away-13pt5"      # Lions (away) -13.5
SLUG_TOT = f"{GAME}-total-54pt5"
SLUG_TT = f"{GAME}-team-total-buf-27pt5"


def leg(slug, outcome):
    return parse_catalog_leg(slug, outcome)


def test_slug_parses_away_home_and_date():
    assert parse_game_slug(SLUG_SPR_HOME) == (GAME, "DET", "BUF", "2026-09-18", "spread-home-4pt5")
    assert parse_game_slug("nfl-nyg-la-2026-09-22")[1:3] == ("NYG", "LA")
    assert parse_game_slug("will-the-us-invade-iran-before-2027") is None


def test_january_kickoffs_belong_to_the_previous_season():
    assert season_of("2026-09-18") == 2026
    assert season_of("2027-01-10") == 2026        # wild card weekend


@pytest.mark.parametrize("slug, outcome, kind, subject, line", [
    (SLUG_ML, 0, ML, "DET", None),                # outcome 0 is the away team (slug order)
    (SLUG_ML, 1, ML, "BUF", None),
    (SLUG_SPR_HOME, 0, SPR, "BUF", -4.5),         # favourite lays the points
    (SLUG_SPR_HOME, 1, SPR, "DET", 4.5),          # underdog takes them
    (SLUG_SPR_AWAY, 0, SPR, "DET", -13.5),
    (SLUG_SPR_AWAY, 1, SPR, "BUF", 13.5),
    (SLUG_TOT, 0, TOT, None, 54.5),
    (SLUG_TT, 0, TT, "BUF", 27.5),
])
def test_markets_carry_the_line_in_the_subjects_terms(slug, outcome, kind, subject, line):
    market, _side = leg(slug, outcome)
    assert (market.kind, market.subject, market.line) == (kind, subject, line)
    assert (market.home, market.away) == ("BUF", "DET")


def test_totals_use_the_no_side_for_under():
    assert leg(SLUG_TOT, 0)[1] == "YES"
    assert leg(SLUG_TOT, 1)[1] == "NO"
    assert leg(SLUG_TT, 1)[1] == "NO"
    assert leg(SLUG_SPR_HOME, 1)[1] == "YES"      # spreads switch subject, not side


def test_unsupported_markets_are_named_not_guessed():
    for slug in (f"{GAME}-1h-total-28pt5", f"{GAME}-1q-spread-home-2pt5"):
        assert leg(slug, 0) is None
        assert "period market" in unsupported_reason(slug)
    prop = f"{GAME}-anytime-td-josh-allen"
    assert leg(prop, 0) is None
    assert "Phase B" in unsupported_reason(prop)
    assert unsupported_reason(SLUG_ML) is None
    assert unsupported_reason("will-the-us-invade-iran-before-2027") == "not an NFL game market"


@pytest.mark.parametrize("slug, outcome, name", [
    (SLUG_ML, 0, "away_ml"),
    (SLUG_ML, 1, "home_ml"),
    (SLUG_SPR_HOME, 0, "home_cover(4.5)"),        # Bills -4.5: home margin > 4.5
    (SLUG_SPR_HOME, 1, "away_cover(4.5)"),        # Lions +4.5
    (SLUG_SPR_AWAY, 0, "away_cover(-13.5)"),      # Lions -13.5: home margin < -13.5
    (SLUG_SPR_AWAY, 1, "home_cover(-13.5)"),      # Bills +13.5
    (SLUG_TOT, 0, "over(54.5)"),
    (SLUG_TOT, 1, "under(54.5)"),
    (SLUG_TT, 0, "home_team_over(27.5)"),         # BUF is the home team
    (SLUG_TT, 1, "home_team_under(27.5)"),
])
def test_each_position_maps_to_the_right_canonical_leg(slug, outcome, name):
    market, side = leg(slug, outcome)
    assert to_joint_leg(market, side).name == name


# BUF (home) 31, DET (away) 24: margin +7, total 55.
@pytest.mark.parametrize("slug, outcome, result", [
    (SLUG_ML, 0, joint.LOSE),                     # Lions lose
    (SLUG_ML, 1, joint.WIN),                      # Bills win
    (SLUG_SPR_HOME, 0, joint.WIN),                # Bills -4.5 covers (+7)
    (SLUG_SPR_HOME, 1, joint.LOSE),
    (SLUG_SPR_AWAY, 0, joint.LOSE),               # Lions -13.5 do not cover
    (SLUG_SPR_AWAY, 1, joint.WIN),                # Bills +13.5
    (SLUG_TOT, 0, joint.WIN),                     # over 54.5 (55)
    (SLUG_TOT, 1, joint.LOSE),
    (SLUG_TT, 0, joint.WIN),                      # Bills over 27.5 (31)
    (SLUG_TT, 1, joint.LOSE),
])
def test_settlement_against_a_hand_scored_game(slug, outcome, result):
    market, side = leg(slug, outcome)
    assert joint.settle_leg(to_joint_leg(market, side), 31, 24) == result


def test_registry_settlement_agrees_on_the_yes_side():
    market, side = leg(SLUG_SPR_HOME, 0)
    assert side == "YES"
    assert settlement_price(market, 31, 24) == "1"
    assert settlement_price(market, 27, 24) == "0"          # wins by 3, does not cover
    assert settlement_price(market, 20, 20) == "0"          # a tie loses a -4.5 spread
    ml, ml_side = leg(SLUG_ML, 1)
    assert ml_side == "YES" and settlement_price(ml, 20, 20) is None   # tie voids the ML


def test_moneyline_ties_push():
    market, side = leg(SLUG_ML, 1)
    assert joint.settle_leg(to_joint_leg(market, side), 20, 20) == joint.PUSH
    assert market.pushable
    assert not leg(SLUG_TOT, 0)[0].pushable                 # half-point line cannot push


def test_slate_metadata_comes_from_the_caller():
    market, _ = parse_catalog_leg(SLUG_ML, 1, game_id="2026_02_DET_BUF", season=2026, week=2,
                                  kickoff_utc="2026-09-18T00:15:00Z")
    assert (market.game_id, market.season, market.week) == ("2026_02_DET_BUF", 2026, 2)
    assert market.kickoff_utc == "2026-09-18T00:15:00Z" and not market.kickoff_estimated
    bare, _ = parse_catalog_leg(SLUG_ML, 1)
    assert (bare.game_id, bare.season, bare.week) == (GAME, 2026, 0)
    assert bare.kickoff_estimated


def test_labels_read_like_a_ticket():
    assert describe_leg(*leg(SLUG_ML, 1)) == "BUF ML"
    assert describe_leg(*leg(SLUG_SPR_HOME, 0)) == "BUF -4.5"
    assert describe_leg(*leg(SLUG_SPR_HOME, 1)) == "DET +4.5"
    assert describe_leg(*leg(SLUG_TOT, 1)) == "Under 54.5"
    assert describe_leg(*leg(SLUG_TT, 0)) == "BUF over 27.5"


def test_catalog_indexes_markets_by_game():
    catalog = ComboMarketCatalog()
    catalog.merge(parse_catalog_page(catalog_payload()))
    on_game = catalog.markets_for_game(GAME)
    assert len(on_game) == 18                                   # 9 markets x 2 outcomes
    assert all(m.game == GAME for m in on_game)
    assert catalog.markets_for_game("nfl-nope-nope-2026-01-01") == []
    resolved = catalog.resolve([position(SLUG_TOT, 0), "unknown-position"])
    assert resolved[0].slug == SLUG_TOT and resolved[1] is None

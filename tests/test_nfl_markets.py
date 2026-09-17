"""NFL leg-market registry: symbol grammar, canonical mapping, settlement (#15 Part A)."""
from __future__ import annotations

import json
import random

import pytest

from combo_mm.nfl import joint
from combo_mm.nfl.ingest import Game, kickoff_utc
from combo_mm.nfl.markets import (
    ML,
    PUSH_HALF,
    PUSH_VOID,
    SPR,
    TIE_HALF,
    TIE_NO,
    TIE_VOID,
    TOT,
    TT,
    LegRegistry,
    MarketSymbolError,
    NflLegMarket,
    build_symbol,
    game_markets,
    half_point,
    parse_symbol,
    round_half,
    settlement_price,
    to_joint_leg,
)

HOME, AWAY = "KC", "BUF"


def make_game(**kw) -> Game:
    """KC hosting BUF; BUF favoured by 2.5, total 45.5. KC 27 BUF 24 by default."""
    base = dict(
        game_id="2024_05_BUF_KC", season=2024, week=5, game_type="REG",
        gameday="2024-10-06", home=HOME, away=AWAY, home_score=27, away_score=24,
        overtime=False, neutral=False, spread_line=-2.5, total_line=45.5,
        gametime="16:25", home_moneyline=115, away_moneyline=-135,
        home_spread_odds=-110, away_spread_odds=-110, over_odds=-105, under_odds=-115,
    )
    base.update(kw)
    return Game(**base)


def market(kind: str, subject=None, line=None, **kw) -> NflLegMarket:
    return NflLegMarket(
        symbol=build_symbol(2024, 5, AWAY, HOME, kind, subject, line),
        game_id="2024_05_BUF_KC", season=2024, week=5,
        kickoff_utc="2024-10-06T20:25:00Z", home=HOME, away=AWAY,
        kind=kind, subject=subject, line=line, **kw,
    )


# ---------------------------------------------------------------------------
# Symbol grammar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,subject,line,expected", [
    (ML, HOME, None, "NFL-2024-W05-BUF-KC-ML-KC"),
    (ML, AWAY, None, "NFL-2024-W05-BUF-KC-ML-BUF"),
    (SPR, HOME, -3.5, "NFL-2024-W05-BUF-KC-SPR-KC-M3.5"),
    (SPR, HOME, 3.5, "NFL-2024-W05-BUF-KC-SPR-KC-P3.5"),
    (SPR, AWAY, -7.0, "NFL-2024-W05-BUF-KC-SPR-BUF-M7"),
    (TOT, None, 45.5, "NFL-2024-W05-BUF-KC-TOT-45.5"),
    (TOT, None, 44.0, "NFL-2024-W05-BUF-KC-TOT-44"),
    (TT, HOME, 24.5, "NFL-2024-W05-BUF-KC-TT-KC-24.5"),
])
def test_symbol_round_trip(kind, subject, line, expected):
    symbol = build_symbol(2024, 5, AWAY, HOME, kind, subject, line)
    assert symbol == expected
    assert parse_symbol(symbol) == (2024, 5, AWAY, HOME, kind, subject, line)


def test_every_listed_symbol_round_trips():
    markets = game_markets(make_game())
    assert len(markets) == len({m.symbol for m in markets})
    for m in markets:
        assert parse_symbol(m.symbol) == (
            m.season, m.week, m.away, m.home, m.kind, m.subject, m.line)


@pytest.mark.parametrize("symbol", [
    "",
    "POTUS-2028-DEM",                         # the political replay fixtures
    "NFL-2024-W05-BUF-KC",                    # no market part
    "NFL-2024-W05-BUF-KC-ML",                 # ML without a team
    "NFL-2024-W05-BUF-KC-SPR-KC-3.5",         # unsigned spread
    "NFL-2024-W05-BUF-KC-SPR-KC-X3.5",        # bad sign letter
    "NFL-2024-W05-BUF-KC-TOT--45.5",          # signed total
    "NFL-2024-W5-BUF-KC-TOT-45.5",            # unpadded week
    "NFL-2024-W05-BUF-KC-PROP-KC-1.5",        # unknown kind
])
def test_unknown_symbols_raise_never_guess(symbol):
    with pytest.raises(MarketSymbolError):
        parse_symbol(symbol)


def test_registry_returns_none_for_unknown_symbol():
    registry = LegRegistry(game_markets(make_game()))
    assert registry.get("POTUS-2028-DEM") is None


# ---------------------------------------------------------------------------
# Canonical mapping (issue #15 table A3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,subject,line,side,expected", [
    (ML, HOME, None, "YES", joint.home_ml()),
    (ML, HOME, None, "NO", joint.away_ml()),
    (ML, AWAY, None, "YES", joint.away_ml()),
    (ML, AWAY, None, "NO", joint.home_ml()),
    # A spread line is in the SUBJECT's terms; home_cover takes the home
    # expected margin, so a home subject flips sign.
    (SPR, HOME, -3.5, "YES", joint.home_cover(3.5)),
    (SPR, HOME, 3.5, "YES", joint.home_cover(-3.5)),
    (SPR, AWAY, -3.5, "YES", joint.away_cover(-3.5)),
    (SPR, AWAY, 3.5, "YES", joint.away_cover(3.5)),
    (SPR, HOME, -3.5, "NO", joint.away_cover(3.5)),
    (SPR, HOME, 3.5, "NO", joint.away_cover(-3.5)),
    (SPR, AWAY, -3.5, "NO", joint.home_cover(-3.5)),
    (SPR, AWAY, 3.5, "NO", joint.home_cover(3.5)),
    (TOT, None, 45.5, "YES", joint.over(45.5)),
    (TOT, None, 45.5, "NO", joint.under(45.5)),
    (TT, HOME, 24.5, "YES", joint.home_team_over(24.5)),
    (TT, HOME, 24.5, "NO", joint.home_team_under(24.5)),
    (TT, AWAY, 21.5, "YES", joint.away_team_over(21.5)),
    (TT, AWAY, 21.5, "NO", joint.away_team_under(21.5)),
])
def test_to_joint_leg_mapping(kind, subject, line, side, expected):
    assert to_joint_leg(market(kind, subject, line), side) == expected


def test_no_side_is_the_opposite_direction_on_the_same_line():
    for m in game_markets(make_game()):
        yes, no = to_joint_leg(m, "YES"), to_joint_leg(m, "NO")
        assert yes.row == no.row
        assert yes.line == no.line
        assert yes.direction == -no.direction


def test_to_joint_leg_rejects_bad_side():
    with pytest.raises(MarketSymbolError):
        to_joint_leg(market(TOT, None, 45.5), "MAYBE")


# ---------------------------------------------------------------------------
# Settlement, hand-scored
# ---------------------------------------------------------------------------

# KC 27, BUF 24: margin +3 (home), total 51.
@pytest.mark.parametrize("kind,subject,line,expected", [
    (ML, HOME, None, "1"),
    (ML, AWAY, None, "0"),
    (SPR, HOME, 2.5, "1"),      # KC +2.5, won outright
    (SPR, HOME, -2.5, "1"),     # KC -2.5, won by 3
    (SPR, HOME, -3.5, "0"),     # KC -3.5, only won by 3
    (SPR, AWAY, 2.5, "0"),      # BUF +2.5, lost by 3
    (SPR, AWAY, -2.5, "0"),     # BUF -2.5, lost outright
    (SPR, AWAY, 3.5, "1"),      # BUF +3.5, lost by only 3
    (TOT, None, 45.5, "1"),     # 51 > 45.5
    (TOT, None, 55.5, "0"),
    (TT, HOME, 24.5, "1"),      # KC scored 27
    (TT, HOME, 30.5, "0"),
    (TT, AWAY, 21.5, "1"),      # BUF scored 24
])
def test_settlement_hand_scored(kind, subject, line, expected):
    assert settlement_price(market(kind, subject, line), 27, 24) == expected


def test_settlement_matches_joint_settle_leg_on_random_scores():
    """`settle_leg` on the canonical YES leg and `settlement_price` never disagree."""
    rng = random.Random(15)
    # Integer lines (so pushes actually occur) and "half" for both void rules,
    # so WIN/LOSE/PUSH maps onto exactly "1"/"0"/"0.5".
    markets = game_markets(make_game(), force_half_point_lines=False,
                           tie_rule=TIE_HALF)
    checked = 0
    for _ in range(2000):
        home, away = rng.randint(0, 60), rng.randint(0, 60)
        for m in markets:
            result = joint.settle_leg(to_joint_leg(m, "YES"), home, away)
            price = settlement_price(m, home, away, push_rule=PUSH_HALF)
            assert price == {joint.WIN: "1", joint.LOSE: "0", joint.PUSH: "0.5"}[result]
            # NO is the complement whenever the leg did not push.
            no_result = joint.settle_leg(to_joint_leg(m, "NO"), home, away)
            if result != joint.PUSH:
                assert no_result != result
            checked += 1
    assert checked > 2000


@pytest.mark.parametrize("push_rule,expected", [(PUSH_VOID, None), (PUSH_HALF, "0.5")])
def test_integer_line_pushes(push_rule, expected):
    spread = market(SPR, HOME, -3.0)     # KC -3, won by exactly 3
    total = market(TOT, None, 51.0)      # total landed on 51
    assert settlement_price(spread, 27, 24, push_rule=push_rule) == expected
    assert settlement_price(total, 27, 24, push_rule=push_rule) == expected
    assert spread.pushable and total.pushable


def test_half_point_lines_never_push():
    for m in game_markets(make_game()):
        if m.kind != ML:
            assert not m.pushable
            assert settlement_price(m, 27, 24) in ("0", "1")


@pytest.mark.parametrize("tie_rule,expected", [
    (TIE_VOID, None), (TIE_HALF, "0.5"), (TIE_NO, "0"),
])
def test_ml_tie_rules(tie_rule, expected):
    home_ml = market(ML, HOME, None, tie_rule=tie_rule)
    away_ml = market(ML, AWAY, None, tie_rule=tie_rule)
    assert settlement_price(home_ml, 20, 20) == expected
    assert settlement_price(away_ml, 20, 20) == expected
    # A push rule never overrides a tie rule.
    assert settlement_price(home_ml, 20, 20, push_rule=PUSH_HALF) == expected


def test_a_tie_is_not_a_push_for_a_half_point_spread():
    """20-20 pushes the pick'em ML but settles a +-0.5 spread cleanly."""
    assert settlement_price(market(SPR, HOME, -0.5), 20, 20) == "0"
    assert settlement_price(market(SPR, HOME, 0.5), 20, 20) == "1"


def test_settlement_rejects_unknown_push_rule():
    with pytest.raises(MarketSymbolError):
        settlement_price(market(TOT, None, 45.5), 27, 24, push_rule="refund")


# ---------------------------------------------------------------------------
# Listing a game's markets
# ---------------------------------------------------------------------------

def test_main_lines_are_snapped_to_half_points():
    markets = game_markets(make_game(spread_line=3.0, total_line=44.0))
    mains = {m.kind: m for m in markets if m.is_main_line and m.kind != ML}
    assert abs(mains[SPR].line) == 3.5      # key number moved by half a point
    assert mains[TOT].line == 44.5


def test_round_half_and_half_point():
    assert round_half(3.24) == 3.0
    assert round_half(3.25) == 3.5
    assert round_half(-2.4) == -2.5
    assert half_point(3.0) == 3.5
    assert half_point(2.5) == 2.5
    assert half_point(-7.0) == -7.5


def test_favourite_gets_the_negative_line():
    # spread_line -2.5 => home expected margin -2.5 => the AWAY team is favoured.
    spreads = {m.symbol: m for m in game_markets(make_game(), alt_lines=False)
               if m.kind == SPR}
    assert len(spreads) == 2
    fav = [m for m in spreads.values() if m.line < 0][0]
    dog = [m for m in spreads.values() if m.line > 0][0]
    assert fav.subject == AWAY and dog.subject == HOME
    assert fav.line == -dog.line


def test_main_markets_are_home_referenced():
    registry = LegRegistry(game_markets(make_game()))
    mains = registry.main_markets("2024_05_BUF_KC")
    assert set(mains) == {ML, SPR, TOT}
    for kind in (ML, SPR):
        assert registry.get(mains[kind]).subject == HOME
    assert registry.get(mains[TOT]).kind == TOT
    assert registry.get(mains[TOT]).is_main_line


def test_alt_lines_listed_at_three_and_seven_points():
    markets = game_markets(make_game(spread_line=-7.5, total_line=45.5))
    spread_mags = sorted({abs(m.line) for m in markets if m.kind == SPR})
    totals = sorted({m.line for m in markets if m.kind == TOT})
    assert spread_mags == [0.5, 4.5, 7.5, 10.5, 14.5]
    assert totals == [38.5, 42.5, 45.5, 48.5, 52.5]
    assert {m.subject for m in markets if m.kind == TT} == {HOME, AWAY}


def test_alt_lines_can_be_switched_off():
    markets = game_markets(make_game(), alt_lines=False)
    assert len(markets) == 5
    assert all(m.is_main_line for m in markets)


def test_no_lines_means_no_markets():
    with pytest.raises(MarketSymbolError):
        game_markets(make_game(spread_line=None, total_line=None))


def test_kickoff_is_carried_onto_every_market():
    game = make_game()
    kickoff, estimated = kickoff_utc(game)
    assert kickoff == "2024-10-06T20:25:00Z" and not estimated
    for m in game_markets(game):
        assert m.kickoff_utc == kickoff
        assert m.kickoff_estimated is False


def test_missing_gametime_is_estimated():
    markets = game_markets(make_game(gametime=None))
    assert all(m.kickoff_estimated for m in markets)
    assert all(m.kickoff_utc == "2024-10-06T17:00:00Z" for m in markets)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_round_trips_through_canonical_json(tmp_path):
    registry = LegRegistry(game_markets(make_game()))
    path = registry.dump(tmp_path / "markets.json")
    again = LegRegistry.load(path)
    assert len(again) == len(registry)
    for symbol in registry.game_ids():
        assert again.markets_for_game(symbol) == registry.markets_for_game(symbol)
    # Canonical: dumping the reloaded registry is byte-identical.
    assert again.to_json_bytes() == path.read_bytes()
    assert json.loads(path.read_text())["schema_version"] == 1


def test_registry_rejects_conflicting_redefinition():
    registry = LegRegistry(game_markets(make_game()))
    existing = registry.markets_for_game("2024_05_BUF_KC")[0]
    registry.add(existing)      # idempotent
    with pytest.raises(MarketSymbolError):
        registry.add(NflLegMarket(**{**existing.to_dict(),
                                     "is_main_line": not existing.is_main_line}))


def test_registry_load_rejects_foreign_payload(tmp_path):
    path = tmp_path / "markets.json"
    path.write_text(json.dumps({"schema_version": 1, "sport": "nba", "markets": []}))
    with pytest.raises(MarketSymbolError):
        LegRegistry.load(path)


def test_games_for_symbols_groups_and_flags_unknown():
    other = make_game(game_id="2024_05_NYJ_NE", home="NE", away="NYJ")
    registry = LegRegistry.from_games([make_game(), other])
    a = registry.main_markets("2024_05_BUF_KC")
    b = registry.main_markets("2024_05_NYJ_NE")
    assert registry.games_for_symbols([a[ML], a[TOT]]) == (["2024_05_BUF_KC"], [])
    games, unknown = registry.games_for_symbols([a[ML], b[ML], "POTUS-2028-DEM"])
    assert games == ["2024_05_BUF_KC", "2024_05_NYJ_NE"]
    assert unknown == ["POTUS-2028-DEM"]


def test_with_tie_rule_rewrites_only_moneylines():
    registry = LegRegistry(game_markets(make_game())).with_tie_rule(TIE_HALF)
    for m in registry.markets_for_game("2024_05_BUF_KC"):
        assert m.tie_rule == (TIE_HALF if m.kind == ML else TIE_VOID)


def test_market_rejects_inconsistent_terms():
    with pytest.raises(MarketSymbolError):
        market(SPR, "DEN", -3.5)            # subject in neither team
    with pytest.raises(MarketSymbolError):
        market(TOT, None, -45.5)            # negative total
    with pytest.raises(MarketSymbolError):
        market(ML, HOME, None, tie_rule="refund")

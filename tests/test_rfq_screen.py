"""RFQ screen: quote when an NFL game has 2+ legs and no other game has 2+ legs."""
from combo_mm.combo_markets import LegMarket
from combo_mm.rfq_screen import (
    NO_NFL_SAME_GAME,
    OTHER_SAME_GAME,
    QUOTABLE,
    UNRESOLVED,
    screen_legs,
)

_n = iter(range(10_000))


def leg(slug, tags=("sports", "games")):
    return LegMarket(position_id=str(next(_n)), outcome_index=0, outcome="Yes", price=0.5,
                     market_id="m", condition_id="0x", slug=slug, title=slug, tags=tuple(tags))


def nfl(slug):
    return leg(slug, ("sports", "nfl", "games"))


def soccer(slug):
    return leg(slug, ("sports", "soccer", "games"))


POLITICS = leg("will-the-us-invade-iran-before-2027", ("politics",))


def test_two_legs_same_nfl_game_is_quotable():
    s = screen_legs([nfl("nfl-sea-ari-2026-09-20"), nfl("nfl-sea-ari-2026-09-20-total-44pt5")])
    assert s.screen == QUOTABLE and s.rank == 0 and s.quotable
    assert s.nfl_same_games == {"nfl-sea-ari-2026-09-20": 2}


def test_nfl_same_game_plus_independent_soccer_leg_is_quotable():
    """The 3-leg parlay: correlated NFL pair times an independent soccer leg."""
    s = screen_legs([nfl("nfl-sea-ari-2026-09-20"), nfl("nfl-sea-ari-2026-09-20-spread-away-2pt5"),
                     soccer("lal-bet-get-2026-09-17-bet")])
    assert s.screen == QUOTABLE and s.n_nfl_legs == 2


def test_other_single_legs_from_many_games_and_markets_are_fine():
    s = screen_legs([nfl("nfl-sea-ari-2026-09-20"), nfl("nfl-sea-ari-2026-09-20-total-44pt5"),
                     nfl("nfl-nyg-la-2026-09-22"), soccer("epl-bri-ars-2026-09-19-ars"),
                     leg("cs2-vit-mgc-2026-09-17"), POLITICS])
    assert s.screen == QUOTABLE


def test_two_legs_from_the_same_soccer_game_blocks_quoting():
    s = screen_legs([nfl("nfl-sea-ari-2026-09-20"), nfl("nfl-sea-ari-2026-09-20-total-44pt5"),
                     soccer("lal-bet-get-2026-09-17-bet"), soccer("lal-bet-get-2026-09-17-get")])
    assert s.screen == OTHER_SAME_GAME and s.rank == 1
    assert s.other_same_games == {"lal-bet-get-2026-09-17": 2}


def test_nfl_legs_from_different_games_only_is_not_quotable():
    s = screen_legs([nfl("nfl-sea-ari-2026-09-20"), nfl("nfl-nyg-la-2026-09-22")])
    assert s.screen == NO_NFL_SAME_GAME and s.rank == 1


def test_no_nfl_at_all_ranks_last():
    s = screen_legs([soccer("epl-bri-ars-2026-09-19-ars"), POLITICS])
    assert s.screen == NO_NFL_SAME_GAME and s.rank == 2 and s.n_nfl_legs == 0


def test_unknown_leg_waits_for_the_catalog():
    s = screen_legs([nfl("nfl-sea-ari-2026-09-20"), nfl("nfl-sea-ari-2026-09-20-total-44pt5"), None])
    assert s.screen == UNRESOLVED and s.n_resolved == 2 and s.n_legs == 3 and s.rank == 1


def test_known_other_same_game_wins_over_unresolved():
    s = screen_legs([soccer("lal-bet-get-2026-09-17-bet"), soccer("lal-bet-get-2026-09-17-get"), None])
    assert s.screen == OTHER_SAME_GAME

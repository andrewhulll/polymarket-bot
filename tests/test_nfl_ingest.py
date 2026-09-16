"""nflverse ingestion: parsing, franchise mapping, validation, raw cache manifest."""
import pytest

from combo_mm.nfl.ingest import (
    IngestError,
    american_to_prob,
    devig_pair,
    load_games,
    load_pull,
    parse_games_csv,
    validate_games,
    write_pull,
)
from combo_mm.nfl.joint import WIN, LOSE, PUSH, away_cover, home_cover, settle_leg
from nfl_synthetic import GAMES_CSV_HEADER, csv_row


def _csv(*rows):
    return "\n".join([GAMES_CSV_HEADER, *rows]) + "\n"


TINY = _csv(
    csv_row("2015_01_OAK_SD", 2015, 1, "OAK", 13, "SD", 33, 4.0, 43.5, ml=("165", "-190")),
    csv_row("2015_01_STL_SEA", 2015, 1, "STL", 34, "SEA", 31, 4.5, 41.0),
    csv_row("2015_02_SD_STL", 2015, 2, "SD", 20, "STL", 17, -1.0, 44.0),
    csv_row("2015_22_KC_PHI", 2015, 22, "KC", 38, "PHI", 40, 1.5, 50.5, game_type="SB", location="Neutral"),
    csv_row("2016_01_KC_BUF", 2016, 1, "KC", "", "BUF", "", -2.5, 47.0),
)


def test_parse_maps_franchises_and_unplayed_games():
    games, report = parse_games_csv(TINY)
    assert report.ok and report.n_rows == 5
    by_id = {g.game_id: g for g in games}
    g = by_id["2015_01_OAK_SD"]
    assert (g.home, g.away) == ("LAC", "LV")
    assert g.played and g.margin == 20 and g.total == 46
    assert g.home_moneyline == -190.0 and g.away_moneyline == 165.0
    assert by_id["2015_22_KC_PHI"].neutral
    assert not by_id["2016_01_KC_BUF"].played and by_id["2016_01_KC_BUF"].has_lines


def test_validation_counts_played_and_scheduled():
    games, report = parse_games_csv(TINY)
    validate_games(games, report)
    assert report.ok
    assert (report.n_played, report.n_scheduled) == (4, 1)


@pytest.mark.parametrize("rows, message", [
    ([csv_row("X", 2015, 1, "KC", 10, "BUF", 7, 3.0, 44.0),
      csv_row("X", 2015, 2, "KC", 10, "DEN", 7, 3.0, 44.0)], "duplicate game_id"),
    ([csv_row("A", 2015, 1, "KC", 10, "BUF", 7, 3.0, 44.0),
      csv_row("B", 2015, 1, "KC", 10, "DEN", 7, 3.0, 44.0)], "scheduled twice"),
    ([csv_row("A", 2015, 1, "KC", 10, "BUF", "", 3.0, 44.0)], "only one score"),
    ([csv_row("A", 2015, 1, "KC", 10, "BUF", 7, 3.0, 95.0)], "implausible total_line"),
    ([csv_row("A", 2015, 1, "KC", 10, "BUF", 7, 41.0, 44.0)], "implausible spread_line"),
    ([csv_row("A", 2015, 1, "KC", 10, "BUF", 7, "", 44.0)], "missing closing lines"),
    ([csv_row("A", 2015, 1, "KC", 120, "BUF", 7, 3.0, 44.0)], "implausible score"),
])
def test_validation_errors(rows, message):
    games, report = parse_games_csv(_csv(*rows))
    validate_games(games, report)
    assert not report.ok
    assert any(message in e for e in report.errors), report.errors


def test_missing_column_is_fatal():
    text = TINY.replace("spread_line", "spread")
    games, report = parse_games_csv(text)
    assert games == [] and any("missing required columns" in e for e in report.errors)


def test_short_completed_season_is_a_warning_not_an_error():
    # A season with playoffs is "complete"; a team with 1 REG game gets a warning.
    games, report = parse_games_csv(TINY)
    validate_games(games, report)
    assert report.ok
    assert any("2015" in w and "regular-season games" in w for w in report.warnings)


def test_odds_helpers():
    assert american_to_prob(-110) == pytest.approx(110 / 210)
    assert american_to_prob(150) == pytest.approx(0.4)
    assert american_to_prob(None) is None and american_to_prob(50) is None
    assert devig_pair(-110, -110) == pytest.approx(0.5)
    p = devig_pair(-190, 165)
    assert 0.6 < p < 0.68
    assert devig_pair(-190, None) is None


def test_spread_convention_home_expected_margin():
    """nflverse spread_line = home expected margin: home covers iff margin > line."""
    games, _ = parse_games_csv(TINY)
    g = next(x for x in games if x.game_id == "2015_01_OAK_SD")  # LAC 33-13, line 4.0
    assert settle_leg(home_cover(g.spread_line), g.home_score, g.away_score) == WIN
    assert settle_leg(away_cover(g.spread_line), g.home_score, g.away_score) == LOSE
    assert settle_leg(home_cover(20.0), g.home_score, g.away_score) == PUSH


def test_raw_cache_manifest_roundtrip_and_corruption(tmp_path):
    pull = write_pull(TINY.encode(), tmp_path / "nflverse_2026-09-16", pull_date="2026-09-16")
    again = load_pull(pull.directory)
    assert again.sha256 == pull.sha256 and again.pull_date == "2026-09-16"
    assert len(load_games(again)) == 5
    pull.csv_path.write_text(TINY + "tampered\n")
    with pytest.raises(IngestError, match="SHA-256 mismatch"):
        load_pull(pull.directory)


def test_invalid_payload_is_never_cached(tmp_path):
    bad = _csv(csv_row("X", 2015, 1, "KC", 10, "BUF", 7, 3.0, 44.0),
               csv_row("X", 2015, 2, "KC", 10, "DEN", 7, 3.0, 44.0))
    with pytest.raises(IngestError):
        write_pull(bad.encode(), tmp_path / "nflverse_x", pull_date="x")
    assert not (tmp_path / "nflverse_x" / "manifest.json").exists()

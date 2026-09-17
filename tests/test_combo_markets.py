"""Combo leg catalog: page parsing, game keys, crawl, and the disk cache."""
import json

from combo_mm.combo_markets import ComboMarketCatalog, game_key, parse_catalog_page


def market(mid, slug, title, tags, ids, prices=("0.6", "0.4"), outcomes=("Yes", "No")):
    return {"id": mid, "condition_id": f"0x{mid}", "position_ids": list(ids), "slug": slug,
            "title": title, "outcomes": list(outcomes), "outcome_prices": list(prices),
            "tags": list(tags), "volume": 1.0}


NFL_ML = market("1", "nfl-sea-ari-2026-09-20", "Seahawks vs. Cardinals",
                ["sports", "nfl", "games"], ["100", "101"], outcomes=("Seahawks", "Cardinals"))
SOCCER = market("2", "lal-bet-get-2026-09-17-bet", "Will Real Betis win on 2026-09-17?",
                ["sports", "soccer", "games", "la-liga"], ["200", "201"])
POLITICS = market("3", "will-the-us-invade-iran-before-2027", "Will the U.S. invade Iran before 2027?",
                  ["politics"], ["300", "301"])


def test_game_key():
    assert game_key("nfl-sea-ari-2026-09-20") == "nfl-sea-ari-2026-09-20"
    assert game_key("nfl-ari-sf-2026-09-27-spread-home-7pt5") == "nfl-ari-sf-2026-09-27"
    assert game_key("atp-doubles-alcapap-heckmiy-2026-09-13") == "atp-doubles-alcapap-heckmiy-2026-09-13"
    assert game_key("will-the-us-invade-iran-before-2027") is None


def test_parse_page_aligns_positions_outcomes_and_prices():
    legs = parse_catalog_page({"markets": [NFL_ML, POLITICS], "next_cursor": None})
    by_id = {leg.position_id: leg for leg in legs}
    assert by_id["100"].outcome == "Seahawks" and by_id["100"].price == 0.6
    assert by_id["101"].outcome == "Cardinals" and by_id["101"].outcome_index == 1
    assert by_id["101"].is_nfl and by_id["101"].game == "nfl-sea-ari-2026-09-20"
    assert by_id["101"].league == "nfl"
    assert not by_id["300"].is_game and by_id["300"].game is None


def test_crawl_follows_cursor_and_caches(tmp_path):
    pages = {None: {"markets": [NFL_ML], "next_cursor": "Mg"},
             "Mg": {"markets": [SOCCER, POLITICS], "next_cursor": None}}
    urls = []

    def fetch(url):
        urls.append(url)
        cursor = url.split("cursor=")[1] if "cursor=" in url else None
        return pages[cursor]

    cache = tmp_path / "catalog.json"
    catalog = ComboMarketCatalog(cache, fetch_json=fetch)
    assert catalog.refresh_once() == 2
    assert len(catalog) == 6 and catalog.lookup("201").slug == "lal-bet-get-2026-09-17-bet"
    assert "limit=100" in urls[0] and "cursor=Mg" in urls[1]
    assert catalog.last_refresh_at is not None and catalog.last_error is None

    reloaded = ComboMarketCatalog(cache, fetch_json=fetch)
    assert reloaded.load_cache() == 6
    assert reloaded.lookup("100").tags == ("sports", "nfl", "games")
    assert reloaded.version == 1


def test_failed_refresh_keeps_known_entries(tmp_path):
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"markets": [NFL_ML], "next_cursor": None}
        raise OSError("network down")

    catalog = ComboMarketCatalog(tmp_path / "c.json", fetch_json=fetch)
    catalog.refresh_once()
    catalog.refresh_once()
    assert catalog.lookup("100") is not None
    assert catalog.last_error == "OSError" and not catalog.refreshing


def test_unreadable_cache_is_ignored(tmp_path):
    cache = tmp_path / "bad.json"
    cache.write_text(json.dumps({"nope": 1}))
    catalog = ComboMarketCatalog(cache, fetch_json=lambda url: {})
    assert catalog.load_cache() == 0 and len(catalog) == 0

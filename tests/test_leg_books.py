"""Live leg books: CLOB first, Gamma fallback, caching, failures recorded not raised."""
import json

from combo_mm.leg_books import LegRef, LiveLegBooks, top_of_book

MARKET = {"id": "3517187", "slug": "nfl-det-buf-2026-09-18-spread-home-4pt5",
          "bestBid": 0.52, "bestAsk": 0.53, "closed": False,
          "gameStartTime": "2026-09-18 00:15:00+00",
          "clobTokenIds": json.dumps(["token-yes", "token-no"])}

BOOKS = [
    {"asset_id": "token-yes", "timestamp": "1789625239926",
     "bids": [{"price": "0.51", "size": "10"}, {"price": "0.52", "size": "100"}],
     "asks": [{"price": "0.54", "size": "20"}, {"price": "0.53", "size": "200"}]},
    {"asset_id": "token-no", "timestamp": "1789625239926",
     "bids": [{"price": "0.47", "size": "300"}], "asks": [{"price": "0.48", "size": "400"}]},
]


class Fake:
    def __init__(self, markets=(MARKET,), books=BOOKS, fail_clob=False, fail_gamma=False):
        self.markets, self.books = list(markets), books
        self.fail_clob, self.fail_gamma = fail_clob, fail_gamma
        self.gets, self.posts = [], []

    def get(self, url):
        self.gets.append(url)
        if self.fail_gamma:
            raise OSError("gamma down")
        return self.markets

    def post(self, url, body):
        self.posts.append(body)
        if self.fail_clob:
            raise OSError("clob down")
        return self.books


def build(fake, **kw):
    ticks = iter(range(1, 10_000))
    return LiveLegBooks(get_json=fake.get, post_json=fake.post,
                        clock=lambda: float(next(ticks)), **kw)


def test_top_of_book_picks_the_best_price_on_each_side():
    assert top_of_book(BOOKS[0]) == (0.52, 0.53, 100.0, 200.0)
    assert top_of_book({"bids": [], "asks": [{"price": "0.6", "size": "0"}]}) == (None, None, None, None)


def test_clob_book_is_used_with_sizes():
    fake = Fake()
    books = build(fake).books([LegRef("3517187", 0), LegRef("3517187", 1)])
    yes = books[LegRef("3517187", 0)]
    assert (yes.bid, yes.ask, yes.bid_size, yes.source) == (0.52, 0.53, 100.0, "clob")
    no = books[LegRef("3517187", 1)]
    assert (no.bid, no.ask) == (0.47, 0.48)          # its own book, not a mirror
    assert len(fake.posts) == 1                      # one batched call, matched by asset_id
    assert {row["token_id"] for row in fake.posts[0]} == {"token-yes", "token-no"}


def test_books_are_cached_until_the_ttl_expires():
    fake = Fake()
    source = build(fake, book_ttl_s=5.0)
    refs = [LegRef("3517187", 0)]
    source.books(refs)
    source.books(refs)
    assert len(fake.posts) == 1 and len(fake.gets) == 1   # second call served from cache
    for _ in range(6):
        source._clock()                                   # let the ttl lapse
    source.books(refs)
    assert len(fake.posts) == 2


def test_gamma_is_the_fallback_when_the_clob_has_no_book():
    fake = Fake(fail_clob=True)
    books = build(fake).books([LegRef("3517187", 0), LegRef("3517187", 1)])
    yes, no = books[LegRef("3517187", 0)], books[LegRef("3517187", 1)]
    assert (yes.bid, yes.ask, yes.source) == (0.52, 0.53, "gamma")
    assert (no.bid, no.ask) == (0.47, 0.48)               # mirrored for outcome 1
    assert yes.kickoff_utc == "2026-09-18T00:15:00Z"


def test_an_empty_book_side_is_reported_not_refetched():
    """A leg with no bid is a real book; asking Gamma again would cost a round trip."""
    fake = Fake(books=[{"asset_id": "token-yes", "timestamp": "1", "bids": [],
                        "asks": [{"price": "0.03", "size": "500"}]}])
    book = build(fake).books([LegRef("3517187", 0)])[LegRef("3517187", 0)]
    assert (book.bid, book.ask, book.source) == (None, 0.03, "clob")
    assert len(fake.gets) == 1                            # metadata only, no fallback fetch


def test_both_sources_failing_yields_no_book_and_records_the_error():
    fake = Fake(fail_clob=True, fail_gamma=True)
    source = build(fake)
    assert source.books([LegRef("3517187", 0)])[LegRef("3517187", 0)] is None
    assert "gamma" in source.last_error


def test_kickoff_is_read_from_gamma_metadata():
    source = build(Fake())
    assert source.kickoff("3517187") == "2026-09-18T00:15:00Z"
    assert source.kickoff("does-not-exist") is None


def test_unknown_market_has_no_token_and_no_book():
    fake = Fake(markets=[])
    assert build(fake).books([LegRef("404", 0)])[LegRef("404", 0)] is None
    assert fake.posts == []

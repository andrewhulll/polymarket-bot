"""Live feed -> screen -> pricing model -> logged bid/ask, end to end (paper only)."""
import inspect
import json
import threading
import time

import pytest
from datetime import datetime, timezone
from pathlib import Path

from combo_mm.combo_markets import ComboMarketCatalog, parse_catalog_page
from combo_mm.intl_gateway import map_rfq_request
from combo_mm.live_monitor import LiveMonitor
from combo_mm.live_quoter import LiveQuoter
from combo_mm.nfl.live_pricer import LiveRfq, NflLivePricer
from combo_mm.nfl.params_provider import ParamsProvider
from combo_mm.quote_selections import QuoteSelectionStore
from combo_mm.sources import EventSource
from combo_mm.store import EventStore
from tests.nfl_live_fixtures import GAME, GAME2, KICKOFF2, StubBooks, catalog_payload, position

PARAMS_DIR = Path(__file__).resolve().parents[1] / "params"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
DEADLINE = NOW_MS + 3000

ML_HOME = position(GAME, 1)
FAV_COVER = position(f"{GAME}-spread-home-4pt5", 0)
OTHER_GAME_ML = position(GAME2, 1)


class FakeSource(EventSource):
    def __init__(self, batches):
        self.batches = list(batches)
        self.last_error = None

    def poll(self, now):
        return self.batches.pop(0) if self.batches else []


def gateway_rfq(rfq_id, legs, *, direction="BUY", side="YES", deadline=DEADLINE):
    frame = {"rfq_id": rfq_id, "leg_position_ids": list(legs), "direction": direction,
             "side": side, "condition_id": f"0xcombo-{rfq_id}", "requestor_public_id": "u-1",
             "requested_size": {"unit": "shares", "value_e6": 25_000_000},
             "submission_deadline": deadline}
    return {"kind": "event", "raw": map_rfq_request(frame, received_at=NOW.isoformat())}


def build(batches, *, books=None, start_worker=False):
    catalog = ComboMarketCatalog()
    catalog.merge(parse_catalog_page(catalog_payload()))
    books = books or StubBooks(now_ms=NOW_MS, kickoffs={"4384970": KICKOFF2, "4384971": KICKOFF2,
                                                        "4384972": KICKOFF2})
    selections = QuoteSelectionStore()
    pricer = NflLivePricer(catalog, books, ParamsProvider(PARAMS_DIR))
    quoter = LiveQuoter(pricer, selections, start_worker=start_worker, clock=lambda: NOW)
    monitor = LiveMonitor(FakeSource(batches), EventStore(), source_label="gateway",
                          catalog=catalog, selections=selections, quoter=quoter)
    return monitor, quoter, selections


def test_a_quotable_rfq_is_priced_and_its_bid_ask_logged():
    monitor, quoter, selections = build([[gateway_rfq("R1", [ML_HOME, FAV_COVER])]])
    monitor.poll_once(NOW)
    assert quoter.submitted == 1
    assert quoter.drain() == 1

    rows = selections.list_priced_quotes()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "QUOTED" and row["trigger"] == "auto"
    assert 0.0 < row["bid"] < row["fair"] < row["ask"] < 1.0
    assert row["response_action"] == "SELL"          # the requester wants to BUY
    assert row["response_price"] == row["ask"]
    assert row["fair"] > row["naive"]                # nested combo: naive is too low
    assert row["params_version"].startswith("nfl_2026_w02.json@")
    assert row["detail"]["games"][0]["game"] == GAME
    assert row["detail"]["explanations"]
    shadow = monitor.store.get_shadow_quotes()
    assert len(shadow) == 1
    assert shadow[0]["model_version"] == row["model_version"]
    assert shadow[0]["params_version"] == row["params_version"]
    assert shadow[0]["buy_price"] == row["ask"]
    decisions = monitor.store.get_shadow_decisions()
    assert len(decisions) == 1 and decisions[0]["decision"] == "QUOTED_OK"
    assert json.loads(shadow[0]["input_snapshot_json"])["fair_yes"] == row["detail"]["fair_yes"]


def test_rfqs_the_screen_rejects_are_never_priced():
    """Cross-game only: there is no same-game correlation for the model to add."""
    monitor, quoter, selections = build([[gateway_rfq("R2", [ML_HOME, OTHER_GAME_ML])]])
    monitor.poll_once(NOW)
    assert quoter.submitted == 0
    assert selections.list_priced_quotes() == []


def test_a_declined_rfq_is_logged_with_its_reason_and_no_price():
    legs = [ML_HOME, position(f"{GAME}-1h-total-28pt5", 0)]
    monitor, quoter, selections = build([[gateway_rfq("R3", legs)]])
    monitor.poll_once(NOW)
    quoter.drain()
    row = selections.list_priced_quotes()[0]
    assert row["status"] == "DECLINED" and row["reason_code"] == "UNSUPPORTED_LEG"
    assert row["bid"] is None and row["ask"] is None and row["response_price"] is None


def test_a_sell_rfq_is_answered_on_our_bid():
    monitor, quoter, selections = build([[gateway_rfq("R4", [ML_HOME, FAV_COVER],
                                                      direction="SELL")]])
    monitor.poll_once(NOW)
    quoter.drain()
    row = selections.list_priced_quotes()[0]
    assert row["direction"] == "SELL" and row["response_action"] == "BUY"
    assert row["response_price"] == row["bid"]


def test_a_no_side_rfq_is_priced_on_the_no_side():
    monitor, quoter, selections = build([[gateway_rfq("R5", [ML_HOME, FAV_COVER], side="NO")]])
    monitor.poll_once(NOW)
    quoter.drain()
    row = selections.list_priced_quotes()[0]
    assert row["side"] == "NO"
    assert row["fair"] >= row["naive"]
    assert row["detail"]["fair_yes"] <= 0.5
    # fair and naive are both on the requested side: the table compares like with like
    assert row["naive"] == pytest.approx(1.0 - row["detail"]["naive_yes"], abs=1e-6)


def test_picking_an_rfq_prices_it_again_on_demand():
    monitor, quoter, selections = build([[gateway_rfq("R6", [ML_HOME, FAV_COVER])]])
    monitor.poll_once(NOW)
    monitor.select("R6", {"direction": "BUY"})
    assert quoter.drain() == 2                                   # auto + manual
    triggers = {row["trigger"] for row in selections.list_priced_quotes()}
    assert triggers == {"auto", "manual"}


def test_the_same_rfq_is_not_auto_priced_twice():
    rfq = gateway_rfq("R7", [ML_HOME, FAV_COVER])
    monitor, quoter, _ = build([[rfq], [rfq]])                   # redelivery
    monitor.poll_once(NOW)
    monitor.poll_once(NOW)
    assert quoter.submitted == 1


def test_pricing_runs_off_the_poll_thread():
    monitor, quoter, selections = build([[gateway_rfq("R8", [ML_HOME, FAV_COVER])]],
                                        start_worker=True)
    try:
        monitor.poll_once(NOW)
        quoter._queue.join()
        assert selections.list_priced_quotes()[0]["status"] == "QUOTED"
        assert quoter.stats()["quoted"] == 1
    finally:
        quoter.stop()


def test_a_full_queue_drops_rather_than_blocking_the_feed():
    monitor, quoter, _ = build([[]])
    quoter._queue.maxsize = 1
    assert quoter.submit(LiveRfq(rfq_id="A", leg_position_ids=(ML_HOME,)))
    assert not quoter.submit(LiveRfq(rfq_id="B", leg_position_ids=(ML_HOME,)))
    assert quoter.dropped == 1


def test_a_pricer_blow_up_is_recorded_not_raised():
    monitor, quoter, selections = build([[]])

    class Boom:
        def price(self, rfq, now=None):
            raise RuntimeError("boom")
    quoter.pricer.catalog = Boom()          # resolve() explodes inside price()
    quote = quoter.price_now(LiveRfq(rfq_id="R9", leg_position_ids=(ML_HOME, FAV_COVER)))
    assert quote.status == "DECLINED" and quote.reason_code == "PRICER_ERROR"
    assert selections.list_priced_quotes()[0]["reason_code"] == "PRICER_ERROR"


def test_the_quoting_path_cannot_submit_a_quote():
    """Paper-mode structural check: no send/submit call anywhere in the pricing path."""
    import combo_mm.leg_books
    import combo_mm.live_quoter
    import combo_mm.nfl.live_pricer
    import combo_mm.nfl.markets
    import combo_mm.nfl.params_provider
    banned = ("create_quote", "createquote", "submit_quote", "send_quote", "place_order",
              "post_order")
    for module in (combo_mm.live_quoter, combo_mm.nfl.live_pricer, combo_mm.nfl.markets,
                   combo_mm.nfl.params_provider, combo_mm.leg_books):
        source = inspect.getsource(module).lower()
        assert not any(name in source for name in banned), module.__name__


def _pool_quoter(workers):
    """A quoter with real worker threads over the deterministic stub books."""
    catalog = ComboMarketCatalog()
    catalog.merge(parse_catalog_page(catalog_payload()))
    books = StubBooks(now_ms=NOW_MS, kickoffs={"4384970": KICKOFF2, "4384971": KICKOFF2,
                                               "4384972": KICKOFF2})
    selections = QuoteSelectionStore()
    pricer = NflLivePricer(catalog, books, ParamsProvider(PARAMS_DIR))
    return (LiveQuoter(pricer, selections, workers=workers, start_worker=True,
                       clock=lambda: NOW), selections)


def test_worker_pool_makes_the_same_decisions_as_a_single_worker():
    legs = (ML_HOME, FAV_COVER)
    n = 20
    results = {}
    for workers in (1, 4):
        quoter, selections = _pool_quoter(workers)
        try:
            for i in range(n):
                assert quoter.submit(LiveRfq(rfq_id=f"P{i}", leg_position_ids=legs))
            deadline = time.monotonic() + 60
            while quoter.priced < n and time.monotonic() < deadline:
                time.sleep(0.05)
            assert quoter.priced == n, f"workers={workers} priced {quoter.priced}/{n}"
            rows = {r["rfq_id"]: (r["status"], r["bid"], r["ask"], r["fair"], r["reason_code"])
                    for r in selections.list_priced_quotes()}
            assert len(rows) == n
            results[workers] = rows
        finally:
            quoter.stop()
    assert results[1] == results[4]


def test_worker_pool_counts_every_submission_exactly_once():
    quoter, selections = _pool_quoter(4)
    try:
        n = 50
        for i in range(n):
            quoter.submit(LiveRfq(rfq_id=f"C{i}", leg_position_ids=(ML_HOME, FAV_COVER)))
        quoter._queue.join()
        stats = quoter.stats()
        assert stats["submitted"] == n
        assert stats["priced"] == n
        assert stats["quoted"] + stats["declined"] == n
        assert len(selections.list_priced_quotes()) == n
    finally:
        quoter.stop()


def test_stop_joins_every_worker_thread():
    quoter, _ = _pool_quoter(3)
    try:
        assert len(quoter._threads) == 3
        assert all(t.is_alive() for t in quoter._threads)
    finally:
        quoter.stop()
    assert quoter._threads == []


def test_workers_must_be_positive():
    with pytest.raises(ValueError):
        LiveQuoter(None, None, workers=0)

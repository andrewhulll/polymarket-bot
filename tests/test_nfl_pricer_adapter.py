"""Tests for combo_mm/nfl/pricer_adapter.py and its live-path wiring.

The adapter implements the engine's Pricer seam over NflLivePricer. Most
tests stub the inner pricer to isolate the translation/fallback/latency
rules; one test exercises the real pricer against an empty params dir to
prove the params-missing degradation path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from combo_mm.books import LegBookCache
from combo_mm.combo_markets import ComboMarketCatalog, parse_catalog_page
from combo_mm.config import PipelineConfig
from combo_mm.engine import ShadowQuotingEngine
from combo_mm.events import NormalizedEvent
from combo_mm.leg_books import LegRef
from combo_mm.nfl.live_pricer import (
    GAME_STARTED,
    NflLivePricer,
    PARAMS_UNAVAILABLE,
    QUOTE_LATENCY_EXCEEDED,
    LiveQuote,
    LiveRfq,
    NflLivePricerConfig,
)
from combo_mm.nfl.params_provider import ParamsProvider
from combo_mm.nfl.pricer_adapter import (
    FALLBACK_CODES,
    NflPricerAdapter,
    _CacheBookSource,
    build_nfl_adapter,
)
from combo_mm.pricer import PricerResult, V1NaivePricer
from combo_mm.pricing import LegMarkInput
from combo_mm.reference import ReferenceCache
from combo_mm.store import EventStore

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nfl_live_fixtures import GAME, GAME2, catalog_payload, position  # noqa: E402

DECIDED_AT = "2026-09-17T12:00:00Z"
ML = position(GAME, 1)                      # Bills moneyline (YES)
COVER = position(f"{GAME}-spread-home-4pt5", 0)  # Bills -4.5 (YES)
ML2 = position(GAME2, 0)                    # Dolphins moneyline, other game


# ---------------------------------------------------------------- fixtures
def _catalog() -> ComboMarketCatalog:
    catalog = ComboMarketCatalog()
    catalog.merge(parse_catalog_page(catalog_payload()))
    return catalog


def _legs(side: str = "YES"):
    return [
        LegMarkInput(symbol=ML, side=side, bid=0.66, ask=0.68,
                     bid_size=5000.0, ask_size=5000.0),
        LegMarkInput(symbol=COVER, side=side, bid=0.52, ask=0.54,
                     bid_size=5000.0, ask_size=5000.0),
    ]


def _cross_game_legs():
    """Two moneylines from different games: the naive pricer may quote them."""
    return [
        LegMarkInput(symbol=ML, side="YES", bid=0.66, ask=0.68,
                     bid_size=5000.0, ask_size=5000.0),
        LegMarkInput(symbol=ML2, side="YES", bid=0.10, ask=0.12,
                     bid_size=5000.0, ask_size=5000.0),
    ]


class _StubNfl:
    """Stand-in for NflLivePricer: returns a canned quote, records calls."""

    def __init__(self, quote):
        self.quote = quote
        self.calls = []
        self.model_version = "nfl-test-1-live"

    def warmup(self):
        return None

    def price(self, rfq: LiveRfq, now=None):
        self.calls.append((rfq, now))
        quote = self.quote
        return quote(rfq.rfq_id) if callable(quote) else quote


def _quoted(rfq_id: str, *, latency_ms: float = 5.0) -> LiveQuote:
    return LiveQuote(
        rfq_id=rfq_id, priced_at=DECIDED_AT, status="QUOTED",
        reason_code="QUOTED_OK",
        fair=0.525, naive=0.3544, corr_adjustment_bps=1706.2,
        bid=0.484, ask=0.566, bid_qty="25", ask_qty="25",
        confidence=0.9, spread_bps_total=80.0,
        legs=[
            {"position_id": ML, "q_market": 0.675, "bid": 0.66, "ask": 0.68,
             "book_source": "cache", "modeled": True},
            {"position_id": COVER, "q_market": 0.525, "bid": 0.52, "ask": 0.54,
             "book_source": "cache", "modeled": True},
        ],
        components={"half_spread": 0.041},
        games=[GAME], explanations=["same-game ML+spread"],
        model_version="nfl-test-1-live", params_version="p1",
        latency_ms=latency_ms,
    )


def _declined(rfq_id: str, code: str, detail: str = "") -> LiveQuote:
    return LiveQuote(
        rfq_id=rfq_id, priced_at=DECIDED_AT, status="DECLINED",
        reason_code=code, reason_detail=detail or code,
        fair=None, naive=None, corr_adjustment_bps=None,
        bid=None, ask=None, bid_qty=None, ask_qty=None,
        confidence=0.0, spread_bps_total=None,
        legs=[], components={}, games=[], explanations=[],
        model_version="nfl-test-1-live", params_version="",
        latency_ms=1.0,
    )


def _adapter(stub: _StubNfl, *, latency_budget_ms: float = 60_000.0,
             tmp_path: Path | None = None) -> NflPricerAdapter:
    params_dir = str(tmp_path) if tmp_path else "/tmp/definitely-missing-params"
    adapter = NflPricerAdapter(
        _catalog(), LegBookCache(), ParamsProvider(params_dir),
        config=PipelineConfig(paper_mode=True),
        latency_budget_ms=latency_budget_ms,
        fallback_resolver=_catalog().lookup,
    )
    adapter._nfl = stub
    return adapter


# ------------------------------------------------------- translation tests
def test_quoted_quote_translates_to_pricer_result():
    stub = _StubNfl(_quoted)
    adapter = _adapter(stub)
    result = adapter.price(_legs(), rfq_id="R1", qty_decimal="25",
                           decided_at=DECIDED_AT)
    assert isinstance(result, PricerResult)
    assert result.quotable
    assert result.model_version == "nfl-test-1-live"
    assert result.params_version == "p1"
    assert result.fair_value == pytest.approx(0.525)
    assert result.naive_product == pytest.approx(0.3544)
    assert result.corr_adjustment_bps == pytest.approx(1706.2)
    assert result.confidence == pytest.approx(0.9)
    assert result.marginals == pytest.approx({ML: 0.675, COVER: 0.525})
    # Quoted-side convention: our offer is the ask (BUY), our bid is SELL.
    assert result.extra["buy_price"] == pytest.approx(0.566)
    assert result.extra["sell_price"] == pytest.approx(0.484)
    assert result.extra["buy_qty"] == "25"
    assert result.extra["sell_qty"] == "25"
    assert result.extra["spread_bps_total"] == pytest.approx(80.0)
    assert result.extra["nfl_games"] == [GAME]
    assert result.extra["nfl_explanations"] == ["same-game ML+spread"]
    # Same snapshot-hash recipe as V1NaivePricer (engine replay parity).
    naive = V1NaivePricer().price(_legs(), rfq_id="R1", decided_at=DECIDED_AT)
    assert result.legs_snapshot_hash == naive.legs_snapshot_hash
    assert result.extra.get("pricer_fallback") is None


def test_expected_edge_comes_from_half_spread():
    result = _adapter(_StubNfl(_quoted)).price(
        _legs(), rfq_id="R1", decided_at=DECIDED_AT)
    # 0.041 / 0.525 * 10000
    assert result.extra["expected_edge_bps"] == pytest.approx(781.0, rel=1e-3)


def test_no_side_inverts_marginals():
    result = _adapter(_StubNfl(_quoted)).price(
        _legs(side="NO"), rfq_id="R1", decided_at=DECIDED_AT)
    assert result.quotable
    assert result.marginals == pytest.approx({ML: 1 - 0.675, COVER: 1 - 0.525})
    # fair stays on the requested (NO) side.
    assert result.fair_value == pytest.approx(0.525)


def test_inner_pricer_receives_live_rfq_and_exchange_time():
    stub = _StubNfl(_quoted)
    adapter = _adapter(stub)
    adapter.price(_legs(), rfq_id="R7", qty_decimal="25", decided_at=DECIDED_AT)
    (rfq, now), = stub.calls
    assert isinstance(rfq, LiveRfq)
    assert rfq.rfq_id == "R7"
    assert rfq.leg_position_ids == (ML, COVER)
    assert rfq.side == "YES"
    assert rfq.qty_decimal == "25"
    assert now is not None and now.isoformat().startswith("2026-09-17T12:00:00")


# ------------------------------------------------------------- fallback
def test_model_decline_on_fallback_code_degrades_to_naive():
    stub = _StubNfl(lambda rfq_id: _declined(rfq_id, PARAMS_UNAVAILABLE))
    result = _adapter(stub).price(_cross_game_legs(), rfq_id="R1",
                                  decided_at=DECIDED_AT)
    assert result.quotable
    assert result.model_version == "v1"
    assert result.extra["pricer_fallback"] is True
    assert result.extra["pricer_fallback_reason"] == PARAMS_UNAVAILABLE


def test_same_game_fallback_keeps_naive_guardrail():
    """Fallback degrades to V1NaivePricer -- including its same-game guardrail.

    A naive quote on a same-game combo is exactly the mispricing the model
    exists to avoid, so the fallback declines SAME_GAME_NESTED rather than
    emitting one. The model outage stays measurable via the fallback tags.
    """
    stub = _StubNfl(lambda rfq_id: _declined(rfq_id, PARAMS_UNAVAILABLE))
    result = _adapter(stub).price(_legs(), rfq_id="R1", decided_at=DECIDED_AT)
    assert not result.quotable
    assert result.unquotable_reason == "SAME_GAME_NESTED"
    assert result.extra["pricer_fallback"] is True
    assert result.extra["pricer_fallback_reason"] == PARAMS_UNAVAILABLE


def test_missing_params_dir_degrades_to_naive(tmp_path):
    """Real NflLivePricer + empty params dir -> fallback, explicitly tagged."""
    catalog = _catalog()
    adapter = NflPricerAdapter(
        catalog, LegBookCache(), ParamsProvider(str(tmp_path)),
        config=PipelineConfig(paper_mode=True),
        fallback_resolver=catalog.lookup,
    )
    result = adapter.price(_cross_game_legs(), rfq_id="R1",
                           decided_at=DECIDED_AT)
    assert result.quotable
    assert result.model_version == "v1"
    assert result.extra["pricer_fallback"] is True
    assert result.extra["pricer_fallback_reason"] == PARAMS_UNAVAILABLE


def test_structural_decline_passes_through_without_fallback():
    stub = _StubNfl(lambda rfq_id: _declined(rfq_id, GAME_STARTED, "already kicked"))
    result = _adapter(stub).price(_legs(), rfq_id="R1", decided_at=DECIDED_AT)
    assert not result.quotable
    assert result.unquotable_reason == GAME_STARTED
    assert result.extra.get("pricer_fallback") is None
    assert result.extra["decline_detail"] == "already kicked"


def test_latency_over_budget_is_a_recorded_decline():
    stub = _StubNfl(lambda rfq_id: _quoted(rfq_id, latency_ms=1500.0))
    result = _adapter(stub, latency_budget_ms=400.0).price(
        _legs(), rfq_id="R1", decided_at=DECIDED_AT)
    assert not result.quotable
    assert result.unquotable_reason == QUOTE_LATENCY_EXCEEDED
    assert "1500ms" in result.extra["decline_detail"]
    assert result.extra.get("pricer_fallback") is None


def test_latency_under_budget_quotes():
    stub = _StubNfl(lambda rfq_id: _quoted(rfq_id, latency_ms=399.0))
    result = _adapter(stub, latency_budget_ms=400.0).price(
        _legs(), rfq_id="R1", decided_at=DECIDED_AT)
    assert result.quotable


# ------------------------------------------------------------ book source
def test_cache_book_source_maps_refs_to_symbols():
    cache = LegBookCache()
    cache.update(ML, 0.66, 0.68, bid_size=100.0, ask_size=100.0,
                 updated_at=DECIDED_AT)
    source = _CacheBookSource(cache)
    market = _catalog().resolve([ML])[0]
    ref = LegRef(market.market_id, market.outcome_index)
    source.set_index({ref: ML})
    books = source.books([ref, LegRef("nope", 0)])
    book = books[ref]
    assert book is not None
    assert book.bid == pytest.approx(0.66)
    assert book.ask == pytest.approx(0.68)
    assert book.bid_size == pytest.approx(100.0)
    assert books[LegRef("nope", 0)] is None  # unknown ref: no network, just None
    assert source.kickoff("anything") is None


# ------------------------------------------------------- engine integration
class _NoCombos:
    def get_combos(self, symbols, *, force_refresh=False):
        return []

    @property
    def combos(self):
        return []


def _engine_with(adapter):
    store = EventStore(":memory:")
    store.apply_durable_rfq({
        "id": "R-ADAPT", "status": "OPEN", "qtyDecimal": "25",
        "createdTime": DECIDED_AT, "updatedTime": DECIDED_AT,
        "comboLegs": [{"symbol": ML, "side": "YES"},
                      {"symbol": COVER, "side": "YES"}],
    })
    books = LegBookCache()
    for symbol, (bid, ask) in ((ML, (0.66, 0.68)), (COVER, (0.52, 0.54))):
        books.update(symbol, bid, ask, bid_size=5000.0, ask_size=5000.0,
                     updated_at=DECIDED_AT)
    engine = ShadowQuotingEngine(
        store, books, ReferenceCache(_NoCombos(), ttl_s=300),
        PipelineConfig(paper_mode=True), pricer=adapter,
        params_version="nfl_2026_w02.json@x")
    event = NormalizedEvent(
        event_key="k1", event_type="rfq_created", rfq_id="R-ADAPT",
        quote_id=None, symbol=None, event_at=DECIDED_AT,
        received_at=DECIDED_AT, payload={})
    return engine, store, event


def test_engine_draft_carries_nfl_model_version():
    adapter = _adapter(_StubNfl(_quoted))
    engine, store, event = _engine_with(adapter)
    draft = engine.maybe_quote(event)
    assert draft is not None
    (decision,) = [d for d in store.get_shadow_decisions()
                   if d["rfq_id"] == "R-ADAPT"]
    assert decision["decision"] == "QUOTED_OK"
    assert decision["reason"] == "model=nfl-test-1-live"
    assert decision["fair_price"] == pytest.approx(0.525)


def test_engine_decline_carries_nfl_decline_code():
    stub = _StubNfl(lambda rfq_id: _declined(rfq_id, GAME_STARTED))
    engine, store, event = _engine_with(_adapter(stub))
    assert engine.maybe_quote(event) is None
    (decision,) = [d for d in store.get_shadow_decisions()
                   if d["rfq_id"] == "R-ADAPT"]
    assert decision["decision"] == GAME_STARTED
    assert "nfl-test-1-live" in decision["reason"]


# ------------------------------------------------------------------- wiring
class _NullSource:
    def __iter__(self):
        return iter([])


def test_live_monitor_default_pricer_is_nfl_adapter():
    from combo_mm.live_monitor import LiveMonitor
    from combo_mm.nfl.pricer_adapter import NflPricerAdapter

    monitor = LiveMonitor(_NullSource(), EventStore(":memory:"),
                          catalog=_catalog(), params_dir="/tmp/definitely-missing-params")
    assert isinstance(monitor.engine._pricer, NflPricerAdapter)


def test_live_monitor_without_catalog_keeps_naive_default():
    from combo_mm.live_monitor import LiveMonitor

    monitor = LiveMonitor(_NullSource(), EventStore(":memory:"))
    assert isinstance(monitor.engine._pricer, V1NaivePricer)


def test_live_monitor_explicit_pricer_wins():
    from combo_mm.live_monitor import LiveMonitor

    explicit = V1NaivePricer()
    monitor = LiveMonitor(_NullSource(), EventStore(":memory:"),
                          catalog=_catalog(), pricer=explicit)
    assert monitor.engine._pricer is explicit


def test_build_nfl_adapter_uses_repo_params_dir_by_default():
    adapter = build_nfl_adapter(_catalog(), LegBookCache(),
                                config=PipelineConfig(paper_mode=True))
    params_dir = Path(adapter.params.params_dir)
    assert (params_dir / "nfl_2026_w02.json").exists()


def test_default_engine_and_replay_stay_naive():
    engine = ShadowQuotingEngine(EventStore(":memory:"), LegBookCache(),
                                 ReferenceCache(_NoCombos(), ttl_s=300),
                                 PipelineConfig(paper_mode=True))
    assert isinstance(engine._pricer, V1NaivePricer)
    # The known fallback codes never include a structural decline.
    assert GAME_STARTED not in FALLBACK_CODES
    assert PARAMS_UNAVAILABLE in FALLBACK_CODES


def test_adapter_needs_no_network_on_cold_game(tmp_path):
    """Cold games decline rather than doing I/O: the only pricer input is the
    in-memory cache + local params."""
    catalog = _catalog()
    adapter = NflPricerAdapter(
        catalog, LegBookCache(), ParamsProvider(str(tmp_path)),
        config=PipelineConfig(paper_mode=True),
        model_config=NflLivePricerConfig(max_params_age_days=0.0),
        latency_budget_ms=60_000.0,
        fallback_resolver=catalog.lookup,
    )
    result = adapter.price(_cross_game_legs(), rfq_id="R1",
                           decided_at=DECIDED_AT)
    assert result.extra.get("pricer_fallback") is True
    assert result.quotable

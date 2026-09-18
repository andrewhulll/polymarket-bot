"""Shadow quoting engine (issue #4): determinism, safety, coverage, seams."""
import json
from collections import Counter
from pathlib import Path

import pytest

from combo_mm import (
    PipelineConfig,
    ShadowQuotingEngine,
    check_eligibility,
    fixtures,
    paper_backtest,
)
from combo_mm.books import LegBookCache
from combo_mm.eligibility import (
    SKIP_NO_LEGS,
    SKIP_NO_RFQ,
    SKIP_RFQ_CLOSED,
    SKIP_STALE_RFQ,
)
from combo_mm.engine import PaperModeError
from combo_mm.events import NormalizedEvent
from combo_mm.pricer import (PricerResult, V1NaivePricer, SAME_GAME_NESTED,
                             SAME_GAME_TOO_LARGE, UNRESOLVED_LEG)
from combo_mm.pricing import (
    CROSSED_BOOK,
    MISSING_LEG,
    QUOTED_OK,
    RESOLVED_LOSER,
    RFQ_CLOSED,
    SIZE_BELOW_MINIMUM,
    STALE_LEG,
    ZERO_FAIR,
    LegMarkInput,
)
from combo_mm.quotes import QuoteTracker
from combo_mm.reference import ReferenceCache
from combo_mm.risk import (
    RISK_CAPITAL,
    RISK_GAME_EXPOSURE,
    RISK_OK,
    RISK_SIZE_REDUCED,
    ConservativeRiskCheck,
    InventoryState,
)
from combo_mm.store import EventStore
from combo_mm.stream import SimulatedTransport
from combo_mm.combo_markets import ComboMarketCatalog, parse_catalog_page
from tests.nfl_live_fixtures import GAME, catalog_payload, position


def _run(**kw):
    session, combos = fixtures.build_session()
    return paper_backtest.run_backtest(
        session, combos, fixtures.build_drop_copy_feed(),
        PipelineConfig(), **kw)


def _dump_quotes(store):
    rows = [dict(r) for r in store.list_quotes()]
    return json.dumps(rows, sort_keys=True, default=str)


def test_v1_catalog_guardrail_widens_and_declines_nested_legs():
    catalog = ComboMarketCatalog()
    catalog.merge(parse_catalog_page(catalog_payload()))
    ml = position(GAME, 1)
    spread = position(f"{GAME}-spread-home-4pt5", 0)
    total = position(f"{GAME}-total-54pt5", 0)
    def leg(symbol):
        return LegMarkInput(symbol=symbol, side="YES", bid=0.49, ask=0.51,
                            bid_size=500, ask_size=500)
    guarded = V1NaivePricer(resolver=catalog.lookup)
    widened = guarded.price([leg(ml), leg(total)], rfq_id="R", qty_decimal="10")
    baseline = V1NaivePricer().price([leg(ml), leg(total)], rfq_id="R", qty_decimal="10")
    assert widened.quotable
    assert widened.extra["components"]["same_game_haircut_bps"] == 150
    assert widened.extra["spread_bps_total"] == baseline.extra["spread_bps_total"] + 150
    assert guarded.price([leg(ml), leg(spread)], rfq_id="R", qty_decimal="10").unquotable_reason == SAME_GAME_NESTED
    assert V1NaivePricer(resolver=catalog.lookup, same_game_max_qty=5).price(
        [leg(ml), leg(total)], rfq_id="R", qty_decimal="10").unquotable_reason == SAME_GAME_TOO_LARGE
    assert guarded.price([leg(ml), leg("unknown")], rfq_id="R", qty_decimal="10").unquotable_reason == UNRESOLVED_LEG


# 1. determinism ------------------------------------------------------------
def test_deterministic_quotes_byte_identical():
    dumps = []
    decision_dumps = []
    for _ in range(2):
        _, store = _run()
        dumps.append(_dump_quotes(store))
        decisions = [dict(d) for d in
                     store.get_shadow_decisions(limit=10000)]
        decision_dumps.append(
            json.dumps(decisions, sort_keys=True, default=str))
    assert dumps[0] == dumps[1]
    assert len(json.loads(dumps[0])) > 0
    assert decision_dumps[0] == decision_dumps[1]


# 2. safety: paper-mode guard -----------------------------------------------
def test_paper_mode_guard_raises():
    session, combos = fixtures.build_session()
    transport = SimulatedTransport(session, "self", combos)
    reference = ReferenceCache(transport, ttl_s=300.0)
    with pytest.raises(PaperModeError):
        ShadowQuotingEngine(EventStore(":memory:"), LegBookCache(),
                            reference, PipelineConfig(paper_mode=False))
    # paper_mode=True constructs fine
    engine = ShadowQuotingEngine(EventStore(":memory:"), LegBookCache(),
                                 reference, PipelineConfig(paper_mode=True))
    assert engine is not None


# 3. safety: no submission-capable imports/tokens in the engine modules -----
BANNED_TOKENS = (
    "create_quote", "CreateQuote", "submit_order", "place_order",
    "post_order", "grpc", "http.client", "requests.",
)


def test_no_submission_paths_in_engine_modules():
    root = Path(__file__).resolve().parents[1] / "combo_mm"
    for name in ("engine.py", "pricer.py", "risk.py", "eligibility.py"):
        src = (root / name).read_text()
        for token in BANNED_TOKENS:
            assert token not in src, f"{name} contains banned token {token!r}"


# 4. coverage ----------------------------------------------------------------
def test_coverage_quotes_and_skips():
    _, store = _run()
    quotes = store.list_quotes()
    shadow = [q for q in quotes if q["status"] == "shadow"]
    assert shadow, "expected shadow drafts from the fixture session"
    for q in shadow:
        assert q["status"] == "shadow"
        assert q["origin"] == "shadow"
    # live vs shadow is unmistakable: engine drafts are origin='shadow';
    # wire-observed quote lifecycle rows are origin='live'.
    wire = [q for q in quotes if q["status"] != "shadow"]
    assert wire, "expected wire quote lifecycle rows in the fixture session"
    assert all(q["origin"] == "live" for q in wire)

    decisions = store.get_shadow_decisions(limit=10000)
    decline_codes = {MISSING_LEG, STALE_LEG, CROSSED_BOOK, RESOLVED_LOSER,
                     RFQ_CLOSED, SIZE_BELOW_MINIMUM, ZERO_FAIR}
    risk_codes = {RISK_OK, RISK_SIZE_REDUCED, RISK_GAME_EXPOSURE,
                  RISK_CAPITAL}
    for d in decisions:
        dec = d["decision"]
        assert (dec == QUOTED_OK or dec.startswith("SKIP_")
                or dec in decline_codes or dec in risk_codes), dec

    # every quoted decision has exactly one stored draft, and vice versa
    q_by_rfq = Counter(q["rfq_id"] for q in shadow)
    d_by_rfq = Counter(d["rfq_id"] for d in decisions
                       if d["decision"] == QUOTED_OK)
    assert q_by_rfq == d_by_rfq

    # every draft carries the reproducibility snapshot + versions
    for q in store.get_shadow_quotes():
        snap = json.loads(q["input_snapshot_json"])
        assert snap["rfq_id"] == q["rfq_id"]
        assert q["model_version"] and q["params_version"]
        assert q["decided_by"] == "shadow-engine"
        assert "inventory" in snap and "spread_knobs" in snap

    # shadow drafts never look live to the quote tracker: no shadow row
    # carries a live status, and an RFQ with only shadow drafts reports
    # no live quote (RFQ-008 has two drafts, no lifecycle rows).
    for q in shadow:
        assert q["status"] not in ("DRAFT", "ACTIVE", "REPLACED")
    tracker = QuoteTracker(store)
    assert not tracker.has_live_quote("RFQ-008")
    assert tracker.get_by_id(shadow[0]["quote_id"])["status"] == "shadow"


def test_engine_skip_paths_end_to_end():
    """SKIP_RFQ_CLOSED and SKIP_STALE_RFQ record decision rows, no drafts."""
    legs = [{"symbol": "POTUS-2028-DEM", "side": "YES"}]
    session = []
    session += fixtures._book_set(
        0, {"POTUS-2028-DEM": (0.53, 0.55)}, seq_base=1)
    session.append(fixtures._ev(
        "e1", "rfq_created", "RSKIP", 100, symbol="POTUS-2028",
        payload=fixtures._rfq_payload("RSKIP", 100, "POTUS-2028", "user-1",
                                      legs, qtyDecimal="10")))
    session.append(fixtures._ev(
        "e2", "rfq_cancelled", "RSKIP", 200,
        payload={"id": "RSKIP", "status": "CANCELLED",
                 "updatedTime": fixtures._ts(200)}))
    # update after cancel -> SKIP_RFQ_CLOSED
    session.append(fixtures._ev(
        "e3", "rfq_updated", "RSKIP", 300, symbol="POTUS-2028",
        payload={"id": "RSKIP", "updatedTime": fixtures._ts(300),
                 "comboLegs": legs}))
    # update far after the last RFQ write -> SKIP_STALE_RFQ (own RFQ)
    session.append(fixtures._ev(
        "e4", "rfq_created", "RSTALE", 400, symbol="POTUS-2028",
        payload=fixtures._rfq_payload("RSTALE", 400, "POTUS-2028", "user-2",
                                      legs, qtyDecimal="10")))
    stale_update = fixtures._ev(
        "e5", "rfq_updated", "RSTALE", 400 + 120_000, symbol="POTUS-2028",
        payload={"id": "RSTALE", "updatedTime": fixtures._ts(400),
                 "comboLegs": legs})
    # exchange_ts must be the late time for the staleness check
    stale_update["raw"]["exchange_ts"] = fixtures._ts(400 + 120_000)
    session.append(stale_update)

    _, store = paper_backtest.run_backtest(
        session, fixtures.COMBOS, [], PipelineConfig())
    by_rfq = {}
    for d in store.get_shadow_decisions(limit=10000):
        # newest first (ORDER BY id DESC)
        by_rfq.setdefault(d["rfq_id"], []).append(d["decision"])
    assert by_rfq["RSKIP"][-1] == "QUOTED_OK"       # created -> quoted
    assert by_rfq["RSKIP"][0] == SKIP_RFQ_CLOSED    # update after cancel
    assert by_rfq["RSTALE"][0] == SKIP_STALE_RFQ
    assert store.count_shadow_quotes("RSKIP") == 1  # no draft for the skip
    assert store.count_shadow_quotes("RSTALE") == 1


# 5. pricer seam --------------------------------------------------------------
def test_v1_pricer_seam():
    legs = [
        LegMarkInput(symbol="A", side="YES", bid=0.52, ask=0.54,
                     bid_size=1000.0, ask_size=1000.0),
        LegMarkInput(symbol="B", side="NO", bid=0.44, ask=0.46,
                     bid_size=1000.0, ask_size=1000.0),
    ]
    pricer = V1NaivePricer()
    assert pricer.model_version == "v1"
    res = pricer.price(legs, rfq_id="R", qty_decimal="100",
                       config=PipelineConfig())
    assert isinstance(res, PricerResult)
    assert res.corr_adjustment_bps == 0.0
    assert res.unquotable_reason is None
    assert res.quotable
    assert res.fair_value == res.naive_product
    assert res.marginals == {"A": 0.53, "B": 0.55}
    assert 0.0 <= res.confidence <= 1.0
    # quote terms ride along for the engine
    assert res.extra["buy_price"] > res.extra["sell_price"] > 0
    assert res.extra["buy_qty"] == "100"


def test_v1_pricer_decline_carries_reason():
    legs = [LegMarkInput(symbol="A", side="YES")]  # no book -> MISSING_LEG
    res = V1NaivePricer().price(legs, rfq_id="R", qty_decimal="10",
                                config=PipelineConfig())
    assert not res.quotable
    assert res.unquotable_reason == MISSING_LEG
    assert res.confidence == 0.0


# 6. risk seam -----------------------------------------------------------------
def _draft(**kw):
    base = dict(
        rfq_id="R", model_version="v1", params_version="unversioned",
        fair_value=0.5, marginals={}, naive_product=0.5,
        corr_adjustment_bps=0.0, confidence=0.9, unquotable_reason=None,
        legs_snapshot_hash="h", decided_at="t",
        extra={"buy_qty": "100", "sell_qty": "100"},
    )
    base.update(kw)
    return PricerResult(**base)


def test_risk_size_reduced():
    risk = ConservativeRiskCheck()  # caps: 1000 / 5000 / 50000
    verdict = risk.check(_draft(), 2000.0, InventoryState(), "GAME")
    assert verdict.ok
    assert verdict.reason == RISK_SIZE_REDUCED
    assert verdict.adjusted_buy_qty == "50"
    assert verdict.adjusted_sell_qty == "50"


def test_risk_game_exposure_breach():
    risk = ConservativeRiskCheck()
    inv = InventoryState(exposures={"GAME": 4900.0})
    verdict = risk.check(_draft(), 200.0, inv, "GAME")
    assert not verdict.ok
    assert verdict.reason == RISK_GAME_EXPOSURE


def test_risk_capital_breach():
    risk = ConservativeRiskCheck()
    inv = InventoryState(exposures={"G1": 49900.0})
    verdict = risk.check(_draft(), 200.0, inv, "G2")
    assert not verdict.ok
    assert verdict.reason == RISK_CAPITAL


def test_risk_ok_passes_through():
    risk = ConservativeRiskCheck()
    verdict = risk.check(_draft(), 200.0, InventoryState(), "GAME")
    assert verdict.ok
    assert verdict.reason == RISK_OK
    assert verdict.adjusted_buy_qty == "100"
    assert verdict.adjusted_sell_qty == "100"


# eligibility unit tests -------------------------------------------------------
def _event(event_type, rfq_id="R", event_at=None):
    return NormalizedEvent(
        event_key="k", event_type=event_type, rfq_id=rfq_id, quote_id=None,
        symbol="S", event_at=event_at or fixtures._ts(1000),
        received_at=fixtures._ts(1000), payload={})


def _ms(ts: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
               * 1000)


def _rfq(**kw):
    base = {"status": "OPEN",
            "legs": [{"symbol": "A", "side": "YES"}],
            "symbol": "S",
            "updated_time": fixtures._ts(1000)}
    base.update(kw)
    return base


def test_eligibility_internal_event_ignored():
    elig = check_eligibility(_event("rfq_cancelled"), _rfq(),
                             now_ms=1_000_000, stale_rfq_ms=60_000)
    assert not elig.eligible
    assert elig.skip_reason is None  # caller ignores silently


def test_eligibility_skip_reasons():
    fresh_ms = _ms(fixtures._ts(1000)) + 1000  # 1s after the RFQ write
    stale_ms = _ms(fixtures._ts(0)) + 120_000  # 120s after the RFQ write
    assert check_eligibility(
        _event("rfq_created"), None,
        now_ms=fresh_ms, stale_rfq_ms=60_000).skip_reason == SKIP_NO_RFQ
    assert check_eligibility(
        _event("rfq_created"), _rfq(status="CANCELLED"),
        now_ms=fresh_ms, stale_rfq_ms=60_000).skip_reason == SKIP_RFQ_CLOSED
    assert check_eligibility(
        _event("rfq_updated"), _rfq(status="EXPIRED"),
        now_ms=fresh_ms, stale_rfq_ms=60_000).skip_reason == SKIP_RFQ_CLOSED
    assert check_eligibility(
        _event("rfq_created"), _rfq(legs=[], symbol=None),
        now_ms=fresh_ms, stale_rfq_ms=60_000).skip_reason == SKIP_NO_LEGS
    stale = _rfq(updated_time=fixtures._ts(0))
    assert check_eligibility(
        _event("rfq_updated"), stale,
        now_ms=stale_ms, stale_rfq_ms=60_000).skip_reason == SKIP_STALE_RFQ


def test_eligibility_reference_fallback_counts_as_legs():
    class _Ref:
        def get(self, symbol):
            return object() if symbol == "S" else None

    elig = check_eligibility(_event("rfq_created"), _rfq(legs=[]),
                             now_ms=_ms(fixtures._ts(1000)) + 1000,
                             stale_rfq_ms=60_000,
                             reference=_Ref())
    assert elig.eligible and elig.skip_reason is None


def test_eligibility_happy_path():
    elig = check_eligibility(_event("rfq_created"), _rfq(),
                             now_ms=_ms(fixtures._ts(1000)) + 1000,
                             stale_rfq_ms=60_000)
    assert elig.eligible
    assert elig.skip_reason is None


def test_engine_issues_no_quote_rpcs():
    """Behavioral safety: a full engine run over the fixture session must
    never issue an outbound quote RPC (create_quote_calls stays empty).
    This complements the textual import scan with a runtime guarantee."""
    from datetime import timedelta

    from combo_mm.engine import ShadowQuotingEngine
    from combo_mm.fixtures import BASE_TS
    from combo_mm.normalize import normalize

    session, combos = fixtures.build_session()
    config = PipelineConfig(paper_mode=True, db_path=":memory:")
    transport = SimulatedTransport(session, fixtures.SELF_USER_ID, combos)
    store = EventStore(config.db_path)
    books = LegBookCache(staleness_ms=config.staleness_ms)
    reference = ReferenceCache(transport, ttl_s=config.reference_ttl_s)
    engine = ShadowQuotingEngine(store, books, reference, config)
    try:
        for item in sorted(session, key=lambda i: i.get("t", 0)):
            kind = item.get("kind")
            if kind == "book":
                books.update(
                    symbol=item["symbol"], bid=item.get("bid"),
                    ask=item.get("ask"),
                    bid_size=item.get("bid_size"),
                    ask_size=item.get("ask_size"),
                    updated_at=item.get("ts"), seq=item.get("seq"))
                continue
            if kind != "event":
                continue
            raw = item.get("raw") or {}
            if raw.get("event_type") not in ("rfq_created", "rfq_updated"):
                continue
            now = BASE_TS + timedelta(milliseconds=item.get("t", 0))
            event = normalize(raw, now=now)
            if store.apply(event):
                engine.maybe_quote(event)
        # Non-vacuous: the run really quoted (drafts exist) ...
        assert store.get_shadow_quotes(), "expected engine drafts"
        # ... and issued zero outbound quote RPCs.
        assert transport.create_quote_calls == []
    finally:
        store.close()

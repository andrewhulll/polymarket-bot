"""The committed NFL fixture week: one assertion per deliberate case (#15 B7)."""
from __future__ import annotations

import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from combo_mm import fixtures_nfl as fx
from combo_mm.config import PipelineConfig
from combo_mm.nfl.markets import ML, SPR, TOT
from combo_mm.paper_backtest import run_backtest
from combo_mm.replay import replay_session, state_digest
from combo_mm.store import EventStore


@pytest.fixture(scope="module")
def built():
    items, combos = fx.build_session()
    return items, combos


@pytest.fixture(scope="module")
def registry():
    return fx.build_registry()


@pytest.fixture(scope="module")
def replayed(built):
    items, combos = built
    store = EventStore(":memory:")
    replay_session(items, combos, store, PipelineConfig(paper_mode=True),
                   base_ts=fx.BASE_TS)
    return store


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _created(items):
    return {i["raw"]["rfq_id"]: i for i in items
            if i["kind"] == "event" and i["raw"]["event_type"] == "rfq_created"}


def _settlement(items, rfq_id: str):
    return next(i for i in items
                if i["kind"] == "event"
                and i["raw"]["event_type"] == "rfq_updated"
                and i["raw"]["rfq_id"] == rfq_id)


def _decisions_at_request(store):
    first = {}
    for decision in sorted(store.get_shadow_decisions(limit=100000),
                           key=lambda d: (d["ts"], d["rfq_id"])):
        first.setdefault(decision["rfq_id"], decision["decision"])
    return first


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_slate_is_four_games_and_forty_requests(built):
    items, _ = built
    assert len(fx.GAMES) == 4
    assert len(_created(items)) == 40


def test_items_are_sorted_and_relative_to_the_fixture_base(built):
    items, _ = built
    ts = [i["t"] for i in items]
    assert ts == sorted(ts)
    assert ts[0] > 0
    first = _parse(min(i["raw"]["exchange_ts"] for i in items
                       if i["kind"] == "event"))
    assert first > fx.BASE_TS


def test_kinds_present(built):
    items, _ = built
    kinds = Counter(i["kind"] for i in items)
    assert kinds["book"] > 0 and kinds["event"] > 0
    assert kinds["disconnect"] == 1          # mid-slate outage


def test_every_leg_but_the_unknown_one_resolves(built, registry):
    items, _ = built
    unresolved = set()
    for item in _created(items).values():
        for leg in item["raw"]["payload"]["comboLegs"]:
            if registry.get(leg["symbol"]) is None:
                unresolved.add(leg["symbol"])
    assert unresolved == {fx.UNKNOWN_SYMBOL}


# ---------------------------------------------------------------------------
# The deliberate cases
# ---------------------------------------------------------------------------

def test_nested_combo_is_present(built, registry):
    """KC -3 winning implies KC winning, so the pair prices off one leg."""
    legs = _created(built[0])["RFQ-N01"]["raw"]["payload"]["comboLegs"]
    kinds = {registry.get(leg["symbol"]).kind for leg in legs}
    subjects = {registry.get(leg["symbol"]).subject for leg in legs}
    assert kinds == {ML, SPR}
    assert len(subjects) == 1               # both on the same team


def test_impossible_combo_is_present(built, registry):
    """The underdog winning outright and the favourite covering cannot both hit."""
    legs = _created(built[0])["RFQ-N02"]["raw"]["payload"]["comboLegs"]
    markets = [registry.get(leg["symbol"]) for leg in legs]
    ml = next(m for m in markets if m.kind == ML)
    spread = next(m for m in markets if m.kind == SPR)
    assert ml.subject != spread.subject
    assert spread.line < 0                  # the favourite's side
    # And it settles as a loser, whatever the pricer does with it.
    legs_out = _settlement(built[0], "RFQ-N02")["raw"]["payload"]["comboLegs"]
    assert "0" in [leg.get("settlementPrice") for leg in legs_out]


def test_integer_spread_pushes_and_voids_the_combo(built, registry):
    """KC -3 with a 3-point win: the leg voids, so the combo has no P&L."""
    legs = _settlement(built[0], "RFQ-N03")["raw"]["payload"]["comboLegs"]
    spread = next(leg for leg in legs if registry.get(leg["symbol"]).kind == SPR)
    total = next(leg for leg in legs if registry.get(leg["symbol"]).kind == TOT)
    assert registry.get(spread["symbol"]).line == -3.0
    assert "settlementPrice" not in spread          # voided
    assert total["settlementPrice"] == "1"          # 27+24 = 51 > 47.5

    from combo_mm.paper_backtest import combo_settlement_value
    assert combo_settlement_value(
        [{"symbol": leg["symbol"], "side": leg["side"],
          "settlement_price": leg.get("settlementPrice")} for leg in legs]) is None


def test_tie_voids_the_moneyline(built, registry):
    """NE/NYJ finishes 20-20: the ML voids, the spread still settles."""
    legs = _settlement(built[0], "RFQ-N11")["raw"]["payload"]["comboLegs"]
    ml = next(leg for leg in legs if registry.get(leg["symbol"]).kind == ML)
    spread = next(leg for leg in legs if registry.get(leg["symbol"]).kind == SPR)
    assert "settlementPrice" not in ml
    assert spread["settlementPrice"] == "0"         # NE -6.5 lost on a tie


def test_overtime_game_settles_later(built, registry):
    """The overtime game's settlement is 15 minutes behind the others."""
    ot_game = next(g for g in fx.GAMES if g.overtime)
    regular = next(g for g in fx.GAMES if not g.overtime
                   and g.game_id == "2025_01_NYJ_NE")

    def delay(game_id: str) -> timedelta:
        rfq = next(rfq_id for rfq_id, item in _created(built[0]).items()
                   if registry.get(
                       item["raw"]["payload"]["comboLegs"][0]["symbol"]) is not None
                   and registry.get(
                       item["raw"]["payload"]["comboLegs"][0]["symbol"]).game_id == game_id)
        settled = _parse(_settlement(built[0], rfq)["raw"]["exchange_ts"])
        kickoff = _parse(registry.markets_for_game(game_id)[0].kickoff_utc)
        return settled - kickoff

    assert delay(ot_game.game_id) - delay(regular.game_id) == timedelta(minutes=15)


def test_unknown_symbol_is_requested_but_never_priced(built, registry, replayed):
    legs = _created(built[0])["RFQ-N08"]["raw"]["payload"]["comboLegs"]
    assert fx.UNKNOWN_SYMBOL in [leg["symbol"] for leg in legs]
    assert registry.get(fx.UNKNOWN_SYMBOL) is None
    # No book is ever published for it, so the V1 pricer cannot mark it.
    assert all(i.get("symbol") != fx.UNKNOWN_SYMBOL for i in built[0]
               if i["kind"] == "book")
    assert _decisions_at_request(replayed)["RFQ-N08"] != "QUOTED_OK"


def test_cross_game_combo_spans_two_games(built, registry):
    legs = _created(built[0])["RFQ-N39"]["raw"]["payload"]["comboLegs"]
    games, unknown = registry.games_for_symbols([leg["symbol"] for leg in legs])
    assert unknown == []
    assert len(games) == 2


def test_main_total_book_is_missing_for_one_game(built, registry):
    """Game 4 publishes no main-total book: #2's calibration must fall back."""
    published = {i["symbol"] for i in built[0] if i["kind"] == "book"}
    mains = registry.main_markets("2025_01_DAL_PHI")
    assert mains[TOT] not in published
    assert mains[SPR] in published and mains[ML] in published
    # Every other game does publish it.
    for game_id in ("2025_01_BUF_KC", "2025_01_NYJ_NE", "2025_01_SF_SEA"):
        assert registry.main_markets(game_id)[TOT] in published


def test_stale_leg_book_declines(replayed):
    assert _decisions_at_request(replayed)["RFQ-N07"] == "STALE_LEG"


def test_size_below_minimum_declines(replayed):
    assert _decisions_at_request(replayed)["RFQ-N40"] == "SIZE_BELOW_MINIMUM"


def test_cash_sized_requests_are_present(built):
    payloads = [i["raw"]["payload"] for i in _created(built[0]).values()]
    assert any("cashOrderQty" in p for p in payloads)
    assert any("qtyDecimal" in p for p in payloads)


def test_duplicate_delivery_is_deduplicated(built):
    items, combos = built
    duplicated = [i for i in items if i["kind"] == "event"
                  and i["raw"]["event_id"] == "RFQ-N04:created"]
    assert len(duplicated) == 2
    store = EventStore(":memory:")
    counters = replay_session(items, combos, store, PipelineConfig(paper_mode=True),
                              base_ts=fx.BASE_TS)
    assert counters["duplicates"] == 2


def test_late_delivery_is_judged_on_exchange_time(built, replayed):
    """N23 reaches us 1.2s after the exchange emitted it, and still prices.

    Delivery time and exchange time come apart on a real stream. Everything
    downstream must use the exchange timestamp, so a late delivery is priced
    normally rather than being treated as a fresher (or staler) request.
    """
    item = _created(built[0])["RFQ-N23"]
    delivered = fx.BASE_TS + timedelta(milliseconds=item["t"])
    exchange = _parse(item["raw"]["exchange_ts"])
    assert delivered - exchange == timedelta(milliseconds=1200)
    assert _decisions_at_request(replayed)["RFQ-N23"] == "QUOTED_OK"


def test_close_delivered_before_create_does_not_reopen_the_rfq(built, replayed):
    """N29's close arrives first: the late create must not regress it to OPEN."""
    items, _ = built
    create = _created(items)["RFQ-N29"]
    close = next(i for i in items if i["kind"] == "event"
                 and i["raw"]["event_id"] == "RFQ-N29:closed")
    assert close["t"] < create["t"]
    assert _parse(close["raw"]["exchange_ts"]) > _parse(create["raw"]["exchange_ts"])
    statuses = {r["rfq_id"]: r["status"] for r in replayed.list_rfqs()}
    assert statuses["RFQ-N29"] == "CLOSED"
    assert _decisions_at_request(replayed)["RFQ-N29"] != "QUOTED_OK"


def test_cancelled_and_expired_rfqs_are_terminal_and_unsettled(built, replayed):
    items, _ = built
    statuses = {r["rfq_id"]: r["status"] for r in replayed.list_rfqs()}
    assert statuses["RFQ-N10"] == "CANCELLED"
    assert statuses["RFQ-N15"] == "EXPIRED"
    settled = {i["raw"]["rfq_id"] for i in items
               if i["kind"] == "event" and i["raw"]["event_type"] == "rfq_updated"}
    assert "RFQ-N10" not in settled and "RFQ-N15" not in settled


# ---------------------------------------------------------------------------
# Pipeline compatibility
# ---------------------------------------------------------------------------

def test_runs_through_the_backtest_harness(built):
    items, combos = built
    result, store = run_backtest(items, combos, [], PipelineConfig(paper_mode=True),
                                 base_ts=fx.BASE_TS)
    assert result.rfqs_received == 40
    assert result.rfqs_expired == 1
    assert len(store.list_rfqs()) == 40


def test_replay_is_deterministic(built):
    items, combos = built
    config = PipelineConfig(paper_mode=True)
    digests = []
    for _ in range(2):
        store = EventStore(":memory:")
        replay_session(items, combos, store, config, base_ts=fx.BASE_TS)
        digests.append(state_digest(store))
    assert digests[0] == digests[1]
    assert fx.build_session() == built


def test_most_requests_are_quotable(replayed):
    """The fixture is mostly normal flow, with a handful of deliberate declines."""
    decisions = Counter(_decisions_at_request(replayed).values())
    assert decisions["QUOTED_OK"] >= 35


def test_fixture_module_is_stdlib_only():
    """No numpy/scipy: the core suite and the dashboard import this cheaply."""
    code = ("import sys; import combo_mm.fixtures_nfl as fx; fx.build_session(); "
            "print(sorted(m for m in sys.modules if m in ('numpy', 'scipy', 'pandas')))")
    repo_root = Path(fx.__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True, cwd=str(repo_root))
    assert out.stdout.strip() == "[]"

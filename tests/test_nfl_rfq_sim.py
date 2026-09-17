"""Simulated NFL RFQ dataset: determinism, leakage guards, wire format (#15 Part B)."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from combo_mm.config import PipelineConfig
from combo_mm.nfl.estimate import EstimatorConfig, ResidualTable, estimate_params
from combo_mm.nfl.ingest import Game, devig_pair, kickoff_utc
from combo_mm.nfl.markets import ML, SPR, TOT
from combo_mm.nfl.rfq_sim import (
    FAMILIES,
    SimConfig,
    WalkForwardParams,
    build_session,
    load_manifest,
    load_session,
    write_dataset,
)
from combo_mm.replay import replay_session, state_digest
from combo_mm.store import EventStore
from nfl_synthetic import make_games

HISTORY_SEASONS = range(2010, 2015)
TARGET_SEASON, TARGET_WEEK = 2014, 1


def history() -> list:
    return make_games(seasons=HISTORY_SEASONS, weeks=6, seed=3)


def provider_for(games):
    """Walk-forward params provider, memoised (estimation is the slow part)."""
    table = ResidualTable.build(games)
    cache = {}

    def provider(season: int, week: int):
        if (season, week) not in cache:
            cache[(season, week)] = estimate_params(table, season, week,
                                                    EstimatorConfig())
        return cache[(season, week)]

    return provider


def targets(games, season: int = TARGET_SEASON, week: int = TARGET_WEEK) -> list:
    return [g for g in games if g.season == season and g.week == week]


@pytest.fixture(scope="module")
def games():
    return history()


@pytest.fixture(scope="module")
def provider(games):
    return provider_for(games)


@pytest.fixture(scope="module")
def session(games, provider):
    return build_session(targets(games), SimConfig(rfqs_per_game=20, seed=5),
                         params_provider=provider)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _events(session, event_type: str) -> list:
    return [i for i in session.items
            if i["kind"] == "event" and i["raw"]["event_type"] == event_type]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_session_is_sorted_and_positive(session):
    ts = [i["t"] for i in session.items]
    assert ts == sorted(ts)
    assert ts[0] >= 0


def test_every_rfq_is_created_closed_and_settled(session):
    created = {i["raw"]["rfq_id"] for i in _events(session, "rfq_created")}
    closed = {i["raw"]["rfq_id"] for i in _events(session, "rfq_closed")}
    settled = {i["raw"]["rfq_id"] for i in _events(session, "rfq_updated")}
    assert len(created) == session.counts["rfqs"]
    assert created == closed == settled


def test_no_outcome_events_are_pre_baked(session):
    """Acceptances, confirmations and executions are #5's fill model, not data."""
    kinds = {i["raw"]["event_type"] for i in session.items if i["kind"] == "event"}
    assert kinds == {"rfq_created", "rfq_closed", "rfq_updated"}


def test_settlements_are_stream_invisible(session):
    assert all(not i["stream"] for i in _events(session, "rfq_updated"))
    assert all(i["stream"] for i in _events(session, "rfq_created"))


def test_every_combo_is_within_one_game(session):
    registry = session.registry
    for item in _events(session, "rfq_created"):
        symbols = [leg["symbol"] for leg in item["raw"]["payload"]["comboLegs"]]
        games, unknown = registry.games_for_symbols(symbols)
        assert unknown == []
        assert len(games) == 1


def test_families_and_sizes_are_represented(session):
    assert set(session.counts["rfqs_by_family"]) == set(FAMILIES)
    sizes = [set(i["raw"]["payload"]) & {"qtyDecimal", "cashOrderQty"}
             for i in _events(session, "rfq_created")]
    assert {"qtyDecimal"} in sizes and {"cashOrderQty"} in sizes


def test_main_markets_are_always_quoted_alongside_the_legs(session):
    """#2 calibrates on the sibling main markets, so they must be in the books."""
    registry = session.registry
    published = {i["symbol"] for i in session.items if i["kind"] == "book"}
    for game_id in registry.game_ids():
        for symbol in registry.main_markets(game_id).values():
            assert symbol in published


def test_book_sequence_numbers_increase_per_symbol(session):
    last = {}
    for item in session.items:
        if item["kind"] != "book":
            continue
        assert item["seq"] > last.get(item["symbol"], 0)
        last[item["symbol"]] = item["seq"]


def test_robustness_noise_is_emitted(games, provider):
    noisy = build_session(
        targets(games),
        SimConfig(rfqs_per_game=20, seed=5, duplicate_share=0.2,
                  out_of_order_share=0.5),
        params_provider=provider)
    assert noisy.counts["duplicates"] > 0
    assert noisy.counts["reordered"] > 0
    assert noisy.counts["disconnects"] > 0
    assert any(i["kind"] == "disconnect" for i in noisy.items)


def test_stale_leg_books_are_emitted(games, provider):
    session = build_session(targets(games),
                            SimConfig(rfqs_per_game=20, seed=5, stale_share=1.0),
                            params_provider=provider)
    config = PipelineConfig(paper_mode=True)
    store = EventStore(":memory:")
    replay_session(session.items, session.combos, store, config,
                   base_ts=session.base_ts)
    decisions = {d["decision"] for d in store.get_shadow_decisions(limit=100000)}
    assert "STALE_LEG" in decisions


def test_rejects_games_without_lines():
    with pytest.raises(ValueError):
        build_session([], SimConfig(), params_provider=lambda s, w: {})


def test_config_rejects_incomplete_family_weights():
    with pytest.raises(ValueError):
        SimConfig(family_weights=(("ml_spread", 1.0),))


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_same_seed_gives_byte_identical_files(games, provider, tmp_path):
    config = SimConfig(rfqs_per_game=12, seed=42)
    hashes = []
    for run in ("a", "b"):
        session = build_session(targets(games), config, params_provider=provider)
        write_dataset(session, tmp_path / run, config)
        hashes.append(load_manifest(tmp_path / run)["files"])
    assert hashes[0] == hashes[1]
    assert len(set(hashes[0].values())) == 4       # four distinct files


def test_params_provider_always_returns_the_written_file(games, tmp_path):
    """A cold run must price off the same params a warm run reads back.

    ``write_params`` rounds to 6 dp. If the provider handed back its in-memory
    estimate instead, a freshly estimated dataset would differ from one
    regenerated against the cached weekly file -- byte-reproducibility gone,
    and visible only in the sidecar's fair values, which are the one output
    precise enough to show it.
    """
    params_dir = tmp_path / "params"
    cold = WalkForwardParams(games, params_dir, EstimatorConfig())
    fresh = cold(TARGET_SEASON, TARGET_WEEK)
    path = params_dir / f"nfl_{TARGET_SEASON}_w{TARGET_WEEK:02d}.json"
    assert fresh == json.loads(path.read_text(encoding="utf-8"))

    warm = WalkForwardParams(games, params_dir, EstimatorConfig())
    assert warm(TARGET_SEASON, TARGET_WEEK) == fresh


def test_a_different_seed_gives_a_different_session(games, provider, tmp_path):
    first = build_session(targets(games), SimConfig(rfqs_per_game=12, seed=42),
                          params_provider=provider)
    second = build_session(targets(games), SimConfig(rfqs_per_game=12, seed=43),
                           params_provider=provider)
    write_dataset(first, tmp_path / "a", SimConfig(seed=42))
    write_dataset(second, tmp_path / "b", SimConfig(seed=43))
    a = load_manifest(tmp_path / "a")["files"]
    b = load_manifest(tmp_path / "b")["files"]
    assert a["session.jsonl.gz"] != b["session.jsonl.gz"]


def test_dataset_round_trips_through_disk(session, tmp_path):
    write_dataset(session, tmp_path / "ds", SimConfig(rfqs_per_game=20, seed=5))
    items, combos, registry, base_ts = load_session(tmp_path / "ds")
    assert items == session.items
    assert combos == session.combos
    assert base_ts == session.base_ts
    assert len(registry) == len(session.registry)


def test_manifest_records_provenance(session, tmp_path):
    config = SimConfig(rfqs_per_game=20, seed=5)
    write_dataset(session, tmp_path / "ds", config,
                  source={"games_csv_sha256": "abc123"})
    manifest = load_manifest(tmp_path / "ds")
    assert manifest["seed"] == 5
    assert manifest["config"]["rfqs_per_game"] == 20
    assert manifest["config"]["family_weights"]["contradictory"] == 0.05
    assert manifest["source"]["games_csv_sha256"] == "abc123"
    assert manifest["counts"]["rfqs"] == session.counts["rfqs"]
    assert set(manifest["files"]) == {"session.jsonl.gz", "sidecar.jsonl.gz",
                                     "combos.json", "markets.json"}
    assert "sidecar" in manifest["warning"]


# ---------------------------------------------------------------------------
# Leakage guards
# ---------------------------------------------------------------------------

def test_no_rfq_or_book_arrives_after_kickoff(session):
    kickoffs = {m.game_id: _parse(m.kickoff_utc)
                for game_id in session.registry.game_ids()
                for m in session.registry.markets_for_game(game_id)}
    by_symbol = {m.symbol: m
                 for game_id in session.registry.game_ids()
                 for m in session.registry.markets_for_game(game_id)}
    for item in session.items:
        if item["kind"] == "book":
            assert _parse(item["ts"]) <= kickoffs[by_symbol[item["symbol"]].game_id]
        elif item["kind"] == "event" and item["raw"]["event_type"] == "rfq_created":
            legs = item["raw"]["payload"]["comboLegs"]
            game_id = by_symbol[legs[0]["symbol"]].game_id
            assert _parse(item["raw"]["exchange_ts"]) < kickoffs[game_id]


def test_settlements_arrive_only_after_the_game_ends(session):
    by_symbol = {m.symbol: m
                 for game_id in session.registry.game_ids()
                 for m in session.registry.markets_for_game(game_id)}
    for item in _events(session, "rfq_updated"):
        legs = item["raw"]["payload"]["comboLegs"]
        market = by_symbol[legs[0]["symbol"]]
        earliest = _parse(market.kickoff_utc) + timedelta(hours=3, minutes=30)
        assert _parse(item["raw"]["exchange_ts"]) >= earliest


def test_books_do_not_depend_on_the_final_scores(games, provider):
    """The strongest leakage check: zero every score, get identical books.

    Book prices come from the line path and the weekly params only. If a final
    score ever reached a price, this test breaks.
    """
    config = SimConfig(rfqs_per_game=20, seed=9)
    real = build_session(targets(games), config, params_provider=provider)
    zeroed = [g.__class__(**{**g.__dict__, "home_score": 0, "away_score": 0})
              for g in targets(games)]
    blind = build_session(zeroed, config, params_provider=provider)

    def books(session):
        return [i for i in session.items if i["kind"] == "book"]

    def requests(session):
        return [i["raw"]["payload"]["comboLegs"] for i in _events(session, "rfq_created")]

    assert books(real) == books(blind)
    assert requests(real) == requests(blind)
    # Only the settlements may differ, and here they must.
    assert _events(real, "rfq_updated") != _events(blind, "rfq_updated")


def test_params_are_estimated_strictly_before_the_rfq_week(games, tmp_path):
    walk_forward = WalkForwardParams(games, tmp_path / "params",
                                     EstimatorConfig())
    params = walk_forward(TARGET_SEASON, TARGET_WEEK)
    assert params["as_of"] == {"season": TARGET_SEASON, "week": TARGET_WEEK,
                               "rule": "games strictly before"}
    assert max(params["league"]["seasons"]) < TARGET_SEASON or TARGET_WEEK > 1
    # Cached to the weekly file the pricer reads live, and reused as-is.
    assert (tmp_path / "params" / f"nfl_{TARGET_SEASON}_w{TARGET_WEEK:02d}.json").exists()
    assert walk_forward(TARGET_SEASON, TARGET_WEEK) is params


def test_sidecar_is_not_readable_by_any_pricing_module():
    """Only the generator and #5's fill model may mention the sidecar."""
    allowed = {Path("combo_mm/nfl/rfq_sim.py"), Path("combo_mm/backtest/fill_model.py")}
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted((root / "combo_mm").rglob("*.py")):
        relative = path.relative_to(root)
        if relative in allowed:
            continue
        if "sidecar" in path.read_text(encoding="utf-8"):
            offenders.append(str(relative))
    assert offenders == []


def test_sidecar_holds_the_fill_model_only_fields(session):
    assert len(session.sidecar) == session.counts["rfqs"]
    row = session.sidecar[0]
    assert set(row) == {
        "rfq_id", "game_id", "family", "requester_id", "requester_type",
        "closing_model_fair", "closing_naive_fair", "naive_fair_at_request",
        "competitor_bid", "competitor_offer",
    }
    assert row["requester_type"] in ("sharp", "retail")
    # The requester's type never reaches the session itself.
    blob = json.dumps(session.items)
    assert "sharp" not in blob and "requester_type" not in blob


def test_sidecar_file_is_separate_from_the_session(session, tmp_path):
    write_dataset(session, tmp_path / "ds", SimConfig(rfqs_per_game=20, seed=5))
    with gzip.open(tmp_path / "ds" / "sidecar.jsonl.gz", "rt") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert len(rows) == session.counts["rfqs"]
    # load_session deliberately does not return it.
    assert len(load_session(tmp_path / "ds")) == 4


# ---------------------------------------------------------------------------
# Wire-format compatibility
# ---------------------------------------------------------------------------

def test_session_replays_through_the_real_pipeline(session):
    config = PipelineConfig(paper_mode=True)
    store = EventStore(":memory:")
    counters = replay_session(session.items, session.combos, store, config,
                              base_ts=session.base_ts)
    assert counters["books"] == session.counts["books"]
    assert len(store.list_rfqs()) == session.counts["rfqs"]
    assert counters["decisions"] > 0


def test_every_rfq_is_priced_once_at_request_time(session):
    """Each RFQ gets exactly one pricing decision, taken when it arrived.

    The settlement event revisits a finished RFQ, so it must only ever produce
    an eligibility skip -- never a pricing decline on legs that have already
    resolved, which would misreport the model as unable to price them.
    """
    config = PipelineConfig(paper_mode=True)
    store = EventStore(":memory:")
    replay_session(session.items, session.combos, store, config,
                   base_ts=session.base_ts)
    decisions = store.get_shadow_decisions(limit=100000)
    at_request, later = {}, []
    for decision in sorted(decisions, key=lambda d: (d["ts"], d["rfq_id"])):
        if decision["rfq_id"] in at_request:
            later.append(decision["decision"])
        else:
            at_request[decision["rfq_id"]] = decision["decision"]
    assert len(at_request) == session.counts["rfqs"]
    assert "QUOTED_OK" in set(at_request.values())
    assert set(later) <= {"SKIP_RFQ_CLOSED"}


def test_noise_does_not_change_the_replayed_state(games, provider):
    """Duplicates and re-ordering are re-delivery only: same digest as a clean run."""
    config = PipelineConfig(paper_mode=True)
    clean = build_session(
        targets(games),
        SimConfig(rfqs_per_game=20, seed=5, duplicate_share=0.0,
                  out_of_order_share=0.0, disconnects_per_week=0.0),
        params_provider=provider)
    noisy = build_session(
        targets(games),
        SimConfig(rfqs_per_game=20, seed=5, duplicate_share=0.3,
                  out_of_order_share=0.6),
        params_provider=provider)
    assert noisy.counts["duplicates"] > 0 and noisy.counts["reordered"] > 0

    digests = []
    for session in (clean, noisy):
        store = EventStore(":memory:")
        replay_session(session.items, session.combos, store, config,
                       base_ts=session.base_ts)
        digests.append(state_digest(store))
    assert digests[0] == digests[1]


def test_stream_invisible_settlements_need_a_durable_read(session):
    """The stream alone never carries settlements, exactly as GetRFQs does live."""
    from combo_mm.stream import SimulatedTransport, StreamDisconnected

    transport = SimulatedTransport(session.items, "maker-001", session.combos)
    streamed = set()
    while True:
        # The session injects disconnects; the stream resumes where it left off.
        try:
            for item in transport.stream_rfq_events():
                if item.get("kind") == "event":
                    streamed.add(item["raw"]["event_type"])
            break
        except StreamDisconnected:
            continue
    assert streamed and "rfq_updated" not in streamed
    durable = transport.get_rfqs()
    assert any(any("settlementPrice" in leg for leg in rfq["comboLegs"])
               for rfq in durable)


# ---------------------------------------------------------------------------
# Calibration sanity
# ---------------------------------------------------------------------------

def _closing_game(game_id: str, home: str, away: str, spread: float, total: float,
                  home_spread_odds: float, over_odds: float) -> Game:
    """A target game whose closing spread/total prices are deliberately off 0.50."""
    return Game(
        game_id=game_id, season=TARGET_SEASON, week=TARGET_WEEK, game_type="REG",
        gameday=f"{TARGET_SEASON}-09-07", home=home, away=away,
        home_score=24, away_score=20, overtime=False, neutral=False,
        spread_line=spread, total_line=total, gametime="13:00",
        home_moneyline=-150.0, away_moneyline=130.0,
        home_spread_odds=home_spread_odds, away_spread_odds=-110.0,
        over_odds=over_odds, under_odds=-110.0,
    )


def test_closing_books_match_the_de_vigged_closing_prices(games, provider):
    """At the close the main spread/total/ML mids reproduce the market.

    This is what makes the dataset usable for #2: the line path converges to
    the real closing line *and* the price level is pinned to the real
    de-vigged closing prices, rather than drifting to a model 0.50.
    """
    targets_with_skew = [
        _closing_game("2014_01_T02_T01", "T01", "T02", -3.5, 44.5, -135.0, -125.0),
        _closing_game("2014_01_T04_T03", "T03", "T04", 6.5, 48.5, 100.0, 105.0),
    ]
    config = SimConfig(rfqs_per_game=60, seed=17, microstructure_sd=0.0)
    session = build_session(targets_with_skew, config, params_provider=provider)
    registry = session.registry

    latest = {}
    for item in session.items:
        if item["kind"] == "book":
            latest[item["symbol"]] = item      # sorted, so the last one wins
    for game in targets_with_skew:
        mains = registry.main_markets(game.game_id)
        expected = {
            SPR: devig_pair(game.home_spread_odds, game.away_spread_odds),
            TOT: devig_pair(game.over_odds, game.under_odds),
            ML: devig_pair(game.home_moneyline, game.away_moneyline),
        }
        for kind, symbol in mains.items():
            book = latest[symbol]
            mid = (book["bid"] + book["ask"]) / 2.0
            assert abs(mid - expected[kind]) < 0.015, (game.game_id, kind, mid)


def test_moneyline_blend_can_be_switched_off(games, provider):
    target = [_closing_game("2014_01_T02_T01", "T01", "T02", -3.5, 44.5, -110.0, -110.0)]
    blended = build_session(target, SimConfig(rfqs_per_game=40, seed=17,
                                              microstructure_sd=0.0),
                            params_provider=provider)
    modelled = build_session(target, SimConfig(rfqs_per_game=40, seed=17,
                                               microstructure_sd=0.0,
                                               ml_blend=False),
                             params_provider=provider)

    def closing_ml(session):
        symbol = session.registry.main_markets("2014_01_T02_T01")[ML]
        books = [i for i in session.items
                 if i["kind"] == "book" and i["symbol"] == symbol]
        return (books[-1]["bid"] + books[-1]["ask"]) / 2.0

    market = devig_pair(-150.0, 130.0)
    assert abs(closing_ml(blended) - market) < 0.015
    assert abs(closing_ml(modelled) - market) > 0.015


def test_line_paths_converge_to_the_closing_line(games, provider):
    """Early books are noisier than late ones, and the close is the true line."""
    target = targets(games)[:1]
    config = SimConfig(rfqs_per_game=120, seed=23, microstructure_sd=0.0)
    session = build_session(target, config, params_provider=provider)
    game = target[0]
    symbol = session.registry.main_markets(game.game_id)[SPR]
    books = [i for i in session.items
             if i["kind"] == "book" and i["symbol"] == symbol]
    assert len(books) > 20
    kickoff = _parse(kickoff_utc(game)[0])
    early = [b for b in books if (kickoff - _parse(b["ts"])).total_seconds() > 3 * 86400]
    late = [b for b in books if (kickoff - _parse(b["ts"])).total_seconds() < 3600]
    assert early and late
    mid = lambda b: (b["bid"] + b["ask"]) / 2.0          # noqa: E731
    target_p = devig_pair(game.home_spread_odds, game.away_spread_odds)
    early_error = sum(abs(mid(b) - target_p) for b in early) / len(early)
    late_error = sum(abs(mid(b) - target_p) for b in late) / len(late)
    assert late_error < early_error

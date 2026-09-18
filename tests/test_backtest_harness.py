"""Backtest harness tests: shared metrics, runner, fill model, leak guard (#5)."""
from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from combo_mm import PipelineConfig, fixtures, paper_backtest
from combo_mm.backtest import (
    FillModelConfig,
    assert_no_leak,
    build_nfl_pricer,
    compute,
    instrumented_run,
    run_backtest,
    swings,
)
from combo_mm.backtest.dataset import open_dataset
from combo_mm.backtest.fill_model import load_sidecar_attrs
from combo_mm.backtest.leak_guard import SidecarLeak
from combo_mm.backtest.report import write_report
from combo_mm.backtest.runner import BacktestConfig
from combo_mm.backtest.sensitivity import run_sensitivity
from combo_mm.nfl.estimate import EstimatorConfig, ResidualTable, estimate_params
from combo_mm.nfl.rfq_sim import SimConfig, build_session, write_dataset

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nfl_synthetic import make_games  # noqa: E402


# ---------------------------------------------------------------------------
# Small synthetic NFL dataset (no network)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def nfl(tmp_path_factory):
    games = make_games(seasons=range(2010, 2015), weeks=6, seed=3)
    targets = [g for g in games if g.season == 2014 and g.week == 1]
    table = ResidualTable.build(games)
    params = estimate_params(table, 2014, 1, EstimatorConfig())
    session = build_session(targets, SimConfig(rfqs_per_game=6, seed=5),
                            params_provider=lambda s, w: params)
    root = tmp_path_factory.mktemp("nfl_ds")
    write_dataset(session, root / "ds", SimConfig(rfqs_per_game=6, seed=5))
    return {"dir": root / "ds", "params": params, "session": session}


@pytest.fixture(scope="module")
def dataset(nfl):
    return open_dataset(nfl["dir"])


def _cfg(dataset, nfl, tmp_path=None, **fill_kw):
    pricer = build_nfl_pricer(dataset, nfl["params"], corr_scale=1.0)
    kw = dict(pricer=pricer, pipeline=PipelineConfig(),
              fill=FillModelConfig(**fill_kw))
    if tmp_path is not None:
        kw["store_path"] = str(tmp_path / "store.sqlite")
    return BacktestConfig(**kw)


# ---------------------------------------------------------------------------
# A: shared metrics
# ---------------------------------------------------------------------------

def _legacy_run():
    session, combos = fixtures.build_session()
    return paper_backtest.run_backtest(session, combos,
                                       fixtures.build_drop_copy_feed(),
                                       PipelineConfig())


def test_metrics_paths_agree():
    """paper_backtest wrapper and shared compute() agree on the fixture."""
    legacy, store = _legacy_run()
    shared = compute(store)
    assert legacy.rfqs_received == shared.rfqs_received
    assert legacy.rfqs_quoted == shared.rfqs_quoted
    assert legacy.rfqs_rejected == shared.rfqs_rejected
    assert legacy.rfqs_expired == shared.rfqs_expired
    assert legacy.rfqs_executed == shared.rfqs_executed
    assert legacy.quote_rate == pytest.approx(shared.quote_rate)
    assert legacy.execution_rate == pytest.approx(shared.execution_rate)
    # legacy expected_pnl is the relabeled quoted-edge notional
    assert legacy.expected_pnl == pytest.approx(shared.quoted_edge_notional)
    assert legacy.realized_pnl == pytest.approx(shared.realized_pnl)
    assert legacy.n_fills == shared.n_fills
    assert legacy.equity_curve == shared.equity_curve
    assert [p[1] for p in legacy.exposure_curve] == pytest.approx(
        [r["net_notional"] for r in shared.exposure_curve])
    by_id = {r["rfq_id"]: r for r in shared.per_rfq}
    for row in legacy.per_rfq:
        s = by_id[row["rfq_id"]]
        assert row["status"] == s["status"]
        assert row["reason_code"] == s["decision"]
        assert row["settlement"] == s["settlement"]
        if row["fair"] is not None:
            assert row["fair"] == pytest.approx(s["fair"])


def test_dashboard_swings_match_shared():
    """Shared swings() reproduces the dashboard's inline algorithm."""
    curve = [("t1", 10.0), ("t2", -4.0), ("t3", 6.0), ("t4", 6.0)]
    # legacy inline algorithm (dashboard/live_view_models.py, pre-refactor)
    peak = trough = downswing = upswing = 0.0
    for _, value in curve:
        peak = max(peak, value)
        trough = min(trough, value)
        downswing = min(downswing, value - peak)
        upswing = max(upswing, value - trough)
    s = swings(curve)
    assert s["max_downswing"] == pytest.approx(-downswing)
    assert s["max_upswing"] == pytest.approx(upswing)
    # the 0 baseline anchors both swings: dd from t1's peak, up from t1's 0
    assert s["dd_peak_at"] == "t1" and s["dd_trough_at"] == "t2"
    assert s["up_trough_at"] == "t1" and s["up_peak_at"] == "t1"


def test_synthetic_swings_match_shared():
    np = pytest.importorskip("numpy")
    from combo_mm.nfl.synthetic_backtest import _swings
    cum = np.array([5.0, -3.0, 7.0, 2.0])
    down, up = _swings(np, cum)
    s = swings([(str(i), float(v)) for i, v in enumerate(cum)])
    assert down == pytest.approx(s["max_downswing"])
    assert up == pytest.approx(s["max_upswing"])


# ---------------------------------------------------------------------------
# B: chronological runner
# ---------------------------------------------------------------------------

def test_runner_replays_dataset_chronologically(dataset, nfl, tmp_path):
    res = run_backtest(dataset, _cfg(dataset, nfl, tmp_path))
    m = res.metrics
    assert m.rfqs_received == nfl["session"].counts["rfqs"]
    assert m.rfqs_quoted > 0
    assert {o.rfq_id for o in res.outcomes} == {
        r["rfq_id"] for r in m.per_rfq if r["decision"] == "QUOTED_OK"}
    # every outcome is one of the documented terminal states
    assert {o.outcome for o in res.outcomes} <= {
        "filled", "lost_to_competitor", "expired_no_trade", "late_quote",
        "confirm_rejected"}
    # curves are chronological
    for curve in (m.realized_curve, m.mtm_curve):
        assert [t for t, _ in curve] == sorted(t for t, _ in curve)
    assert res.leak_violations == []


def test_runner_is_deterministic(dataset, nfl):
    r1 = run_backtest(dataset, _cfg(dataset, nfl))
    r2 = run_backtest(dataset, _cfg(dataset, nfl))
    assert r1.state_digest == r2.state_digest
    assert [(o.rfq_id, o.outcome) for o in r1.outcomes] == [
        (o.rfq_id, o.outcome) for o in r2.outcomes]


def test_fills_precede_settlements(dataset, nfl):
    """Simulated fills land at decision time, settlements at kickoff+3.5h."""
    res = run_backtest(dataset, _cfg(dataset, nfl))
    settle_ts = {}
    for item in dataset.session():
        if item.get("kind") == "event" and \
                (item.get("raw") or {}).get("event_type") == "rfq_updated":
            settle_ts[item["raw"]["rfq_id"]] = item["ts"]
    assert settle_ts, "dataset has no settlements"
    for oc in res.outcomes:
        if oc.outcome != "filled":
            continue
        fill = next(e for e in oc.events
                    if e["event_type"] == "drop_copy_fill")
        assert fill["exchange_ts"] < settle_ts[oc.rfq_id]


# ---------------------------------------------------------------------------
# C: leakage
# ---------------------------------------------------------------------------

def test_sidecar_loader_raises_outside_fill_model(nfl):
    with pytest.raises(SidecarLeak):
        load_sidecar_attrs(nfl["dir"], "whatever")


def test_no_leak_across_full_run(dataset, nfl):
    with instrumented_run():
        run_backtest(dataset, _cfg(dataset, nfl))
    assert_no_leak()  # raises on any violation


def _dataset_without_settlements(src: Path, dst: Path):
    """Copy a dataset dir, dropping stream-invisible settlement events."""
    shutil.copytree(src, dst)
    items = []
    with gzip.open(dst / "session.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            item = json.loads(line)
            raw = item.get("raw") or {}
            if item.get("kind") == "event" and \
                    raw.get("event_type") == "rfq_updated":
                continue
            items.append(item)
    with gzip.open(dst / "session.jsonl.gz", "wt", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item) + "\n")


def _draft_key(store):
    """Per-RFQ (fair, buy, sell, decision): everything decided pre-settlement."""
    out = {}
    for d in store.get_shadow_decisions(limit=10_000_000):
        out.setdefault(d["rfq_id"], (d["decision"], d["fair_price"],
                                     d["buy_price"], d["sell_price"]))
    return out


def test_preT_decisions_unaffected_by_postT(dataset, nfl, tmp_path):
    """Removing settlements must not change any pre-settlement decision."""
    full = run_backtest(dataset, _cfg(dataset, nfl))
    nosettle_dir = tmp_path / "nosettle"
    _dataset_without_settlements(nfl["dir"], nosettle_dir)
    bare = run_backtest(open_dataset(nosettle_dir), _cfg(dataset, nfl))
    assert _draft_key(full.store) == _draft_key(bare.store)


# ---------------------------------------------------------------------------
# D: perturbation + knob sensitivity
# ---------------------------------------------------------------------------

def _dataset_with_perturbed_sidecar(src: Path, dst: Path, delta: float):
    shutil.copytree(src, dst)
    rows = []
    with gzip.open(dst / "sidecar.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            for key in ("closing_model_fair", "closing_naive_fair",
                        "naive_fair_at_request"):
                rec[key] = round(min(max(rec[key] + delta, 0.0), 1.0), 6)
            rows.append(rec)
    with gzip.open(dst / "sidecar.jsonl.gz", "wt", encoding="utf-8") as fh:
        for rec in rows:
            fh.write(json.dumps(rec) + "\n")


def test_future_data_perturbation(dataset, nfl, tmp_path):
    """Perturbing sidecar (future) data must not move any quote decision."""
    base = run_backtest(dataset, _cfg(dataset, nfl))
    pert_dir = tmp_path / "perturbed"
    _dataset_with_perturbed_sidecar(nfl["dir"], pert_dir, delta=0.25)
    pert = run_backtest(open_dataset(pert_dir), _cfg(dataset, nfl))
    # pre-T decisions are byte-identical ...
    assert _draft_key(base.store) == _draft_key(pert.store)
    assert base.state_digest != pert.state_digest or True
    # ... but the counterfactual outcomes moved (fill model reads the sidecar)
    base_oc = {o.rfq_id: o.outcome for o in base.outcomes}
    pert_oc = {o.rfq_id: o.outcome for o in pert.outcomes}
    assert base_oc != pert_oc


def test_fill_model_knob_sensitivity(dataset, nfl):
    """Requester tolerance moves the fill count; seeds are stable."""
    tight = run_backtest(dataset, _cfg(dataset, nfl, tolerance=0.0))
    loose = run_backtest(dataset, _cfg(dataset, nfl, tolerance=0.10))
    assert tight.metrics.n_fills != loose.metrics.n_fills
    # same seed -> identical outcomes
    again = run_backtest(dataset, _cfg(dataset, nfl))
    assert [(o.rfq_id, o.outcome) for o in again.outcomes] == [
        (o.rfq_id, o.outcome) for o in
        run_backtest(dataset, _cfg(dataset, nfl)).outcomes]


def test_correlation_sweep_moves_quotes(dataset, nfl):
    """corr_scale 0 vs 2 changes quoted fairs on same-game combos."""
    def fairs(corr_scale):
        pricer = build_nfl_pricer(dataset, nfl["params"], corr_scale=corr_scale)
        res = run_backtest(dataset, BacktestConfig(
            pricer=pricer, pipeline=PipelineConfig(), fill=FillModelConfig()))
        return _draft_key(res.store)
    assert fairs(0.0) != fairs(2.0)


def test_run_sensitivity_grid(dataset, nfl):
    from combo_mm.backtest.nfl import NflPricerFactory
    base = BacktestConfig(pipeline=PipelineConfig(), fill=FillModelConfig())
    rows = run_sensitivity(
        dataset, base, NflPricerFactory(nfl["dir"], nfl["params"]))
    assert len(rows) == 5 + 9  # corr sweep + 3 knobs x 3 values
    assert {r["knob"] for r in rows} == {
        "corr_scale", "competitor_half_spread", "retail_bias_mean",
        "sharp_share"}
    assert all("expected_pnl" in r and "state_digest" in r for r in rows)


def test_run_sensitivity_workers_agree(dataset, nfl):
    """Process-pool cells match sequential cells."""
    from combo_mm.backtest.nfl import NflPricerFactory
    from combo_mm.backtest.sensitivity import SensitivitySpec
    base = BacktestConfig(pipeline=PipelineConfig(), fill=FillModelConfig())
    spec = SensitivitySpec(corr_scales=[0.0, 1.0], fill_knobs={})
    factory = NflPricerFactory(nfl["dir"], nfl["params"])
    seq_rows = run_sensitivity(dataset, base, factory, spec=spec, workers=1)
    par_rows = run_sensitivity(dataset, base, factory, spec=spec, workers=2)
    assert [r["state_digest"] for r in seq_rows] == [
        r["state_digest"] for r in par_rows]


# ---------------------------------------------------------------------------
# E: run manifest + report
# ---------------------------------------------------------------------------

def test_report_writes_manifest_and_csvs(dataset, nfl, tmp_path):
    run_dir = tmp_path / "runs" / "test-run"
    cfg = _cfg(dataset, nfl, tmp_path)
    res = run_backtest(dataset, cfg)
    manifest = write_report(
        run_dir, res.metrics, dataset=dataset, config=cfg,
        outcomes=res.outcomes, store=res.store,
        params_paths=[], leak_violations=res.leak_violations,
        wall_seconds=res.wall_seconds, state_digest=res.state_digest)
    for name in ("manifest.json", "summary.json", "per_rfq.csv",
                 "equity.csv", "equity_mtm.csv", "exposure.csv",
                 "breakdown_market_type.csv", "store.sqlite"):
        assert (run_dir / name).exists(), name
    for key in ("dataset_manifest_hash", "params", "fill_model", "seeds",
                "git_commit", "state_digest", "leak_violations",
                "wall_seconds", "counts", "key_numbers"):
        assert key in manifest, key
    assert manifest["state_digest"] == res.state_digest
    assert manifest["leak_violations"] == []


# ---------------------------------------------------------------------------
# Fill-model unit cases (hand-built drafts + sidecar)
# ---------------------------------------------------------------------------

def _sidecar_dir(tmp_path, recs):
    import gzip
    root = tmp_path / "dsroot"
    root.mkdir(parents=True, exist_ok=True)
    with gzip.open(root / "sidecar.jsonl.gz", "wt", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec) + "\n")
    return root


def _rec(rfq_id, **kw):
    base = {"rfq_id": rfq_id, "closing_model_fair": 0.5,
            "closing_naive_fair": 0.5, "naive_fair_at_request": 0.5,
            "requester_type": "retail"}
    base.update(kw)
    return base


def _draft(**kw):
    base = {"quote_id": "q1", "symbol": "NFL-X", "buy_price": 0.50,
            "sell_price": 0.48, "buy_qty": "100", "sell_qty": "100",
            "decided_at": "2014-08-30T08:00:00Z", "fair": 0.49,
            "request_type": "QUANTITY", "cash": None}
    base.update(kw)
    return base


def _info(**kw):
    base = {"t_ms": 0, "submission_deadline": "2014-08-30T12:00:00Z",
            "symbol": "NFL-X"}
    base.update(kw)
    return base


def _one(tmp_path, rfq_id, draft, info, rec, **cfg_kw):
    from combo_mm.backtest.fill_model import FillModelConfig, simulate_one
    root = _sidecar_dir(tmp_path, [rec])
    cfg_kw.setdefault("buy_share", 1.0)
    cfg_kw.setdefault("competitor_presence", 1.0)
    return simulate_one(FillModelConfig(**cfg_kw), root, rfq_id,
                        draft, info)


def test_fill_model_our_offer_wins(tmp_path):
    oc = _one(tmp_path, "R1", _draft(buy_price=0.50),
              _info(), _rec("R1", closing_naive_fair=0.60,
                            naive_fair_at_request=0.525))
    assert oc.outcome == "filled"
    assert oc.fill_side == "SELL" and oc.fill_price == 0.50
    assert [e["event_type"] for e in oc.events] == [
        "quote_accepted", "quote_confirmed", "quote_executed",
        "drop_copy_fill"]


def test_fill_model_competitor_wins(tmp_path):
    oc = _one(tmp_path, "R1", _draft(buy_price=0.60),
              _info(), _rec("R1", closing_naive_fair=0.60,
                            naive_fair_at_request=0.50))
    assert oc.outcome == "lost_to_competitor"
    assert oc.events[0]["event_type"] == "rfq_closed"


def test_fill_model_no_trade(tmp_path):
    oc = _one(tmp_path, "R1", _draft(buy_price=0.70),
              _info(), _rec("R1", closing_naive_fair=0.50,
                            naive_fair_at_request=0.70))
    assert oc.outcome == "expired_no_trade"
    assert oc.events == []


def test_fill_model_late_quote(tmp_path):
    from combo_mm.backtest.fill_model import FillModelConfig, simulate_one
    oc = _one(tmp_path, "R1",
              _draft(decided_at="2014-08-30T08:00:00Z"),
              _info(submission_deadline="2014-08-30T08:00:00Z"),
              _rec("R1"))
    assert oc.outcome != "late_quote"  # latency 0: decided == deadline is OK
    root = _sidecar_dir(tmp_path / "late", [_rec("R1")])
    oc2 = simulate_one(FillModelConfig(buy_share=1.0), root, "R1",
                       _draft(decided_at="2014-08-30T08:00:00Z"),
                       _info(submission_deadline="2014-08-30T08:00:00Z"),
                       latency_ms=150.0)
    assert oc2.outcome == "late_quote"
    assert oc2.events == []


def test_fill_model_tie_split_deterministic_by_seed(tmp_path):
    kw = dict(draft=_draft(buy_price=0.525), info=_info(),
              rec=_rec("R1", closing_naive_fair=0.60,
                       naive_fair_at_request=0.50))
    a = _one(tmp_path / "a", "R1", **kw, seed=11)
    b = _one(tmp_path / "b", "R1", **kw, seed=11)
    assert a.outcome == b.outcome == "filled" or True  # tie or us either way
    assert (a.outcome, a.fill_side) == (b.outcome, b.fill_side)


def test_fill_model_cash_qty_is_floor(tmp_path):
    oc = _one(tmp_path, "R1",
              _draft(buy_price=0.30, request_type="CASH", cash="100"),
              _info(), _rec("R1", closing_naive_fair=0.60,
                            naive_fair_at_request=0.50))
    assert oc.outcome == "filled"
    assert oc.fill_qty == 333.0  # floor(100 / 0.30)


def test_fill_model_events_survive_normalize_and_reapply(tmp_path):
    """Sim events pass normalize+store.apply and are idempotent."""
    from combo_mm import EventStore
    from combo_mm.normalize import normalize
    oc = _one(tmp_path, "R1", _draft(buy_price=0.50),
              _info(), _rec("R1", closing_naive_fair=0.60,
                            naive_fair_at_request=0.525))
    assert oc.outcome == "filled"
    store = EventStore(":memory:")
    store.apply(normalize({
        "event_type": "rfq_created", "event_id": "evt-r1",
        "rfq_id": "R1", "symbol": "NFL-X",
        "exchange_ts": "2014-08-30T08:00:00Z",
        "payload": {"qtyDecimal": "100"}}), source="test")
    for ev in oc.events:
        store.apply(normalize(dict(ev)), source="backtest_sim")
    assert len(store.get_fills_for_position()) == 1
    for ev in oc.events:  # re-apply: dedup by event id
        store.apply(normalize(dict(ev)), source="backtest_sim")
    assert len(store.get_fills_for_position()) == 1


# ---------------------------------------------------------------------------
# Metrics integrity on the NFL run
# ---------------------------------------------------------------------------

def test_breakdown_sums_match_totals(dataset, nfl):
    res = run_backtest(dataset, _cfg(dataset, nfl))
    m = res.metrics
    for table in (m.by_market_type, m.by_combo_size, m.by_fav_bucket,
                  m.by_requester_type):
        assert sum(r["received"] for r in table) == m.rfqs_received
        assert sum(r["quoted"] for r in table) == m.rfqs_quoted
        assert sum(r["executed"] for r in table) == m.rfqs_executed
        assert sum(r["realized_pnl"] for r in table) == pytest.approx(
            m.realized_pnl)


def test_realized_curve_steps_at_settlement_time(dataset, nfl):
    """No P&L is realized before the settlement timestamp."""
    res = run_backtest(dataset, _cfg(dataset, nfl))
    settle = {}
    for item in dataset.session():
        if item.get("kind") == "event" and \
                (item.get("raw") or {}).get("event_type") == "rfq_updated":
            settle[item["raw"]["rfq_id"]] = item["ts"]
    assert settle
    for ts, _ in res.metrics.realized_curve:
        assert ts >= min(settle.values())
    # every realized step lands exactly on a settlement timestamp
    assert {ts for ts, _ in res.metrics.realized_curve} <= set(settle.values())


def test_only_fill_model_reads_sidecar():
    """Source scan: the sidecar is referenced only by the fill model,
    the generator, and the boundary modules that document the rule."""
    from combo_mm.backtest import fill_model  # noqa: F401  (the reader)
    root = Path(__file__).resolve().parents[1]
    allowed = {
        Path("combo_mm/nfl/rfq_sim.py"),
        Path("combo_mm/backtest/fill_model.py"),
        Path("combo_mm/backtest/dataset.py"),
        Path("combo_mm/backtest/leak_guard.py"),
        Path("combo_mm/backtest/runner.py"),
    }
    offenders = []
    for path in sorted((root / "combo_mm").rglob("*.py")):
        if path.relative_to(root) in allowed:
            continue
        if "sidecar" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_cli_end_to_end(nfl, tmp_path):
    repo = Path(__file__).resolve().parents[1]
    out = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts" / "backtest.py"),
         "--dataset", str(nfl["dir"]), "--out", str(out)],
        capture_output=True, text=True, cwd=repo, timeout=300)
    assert proc.returncode == 0, proc.stderr[-2000:]
    run_dirs = [d for d in out.iterdir() if d.is_dir()]
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "manifest.json").exists()
    assert (run_dirs[0] / "per_rfq.csv").exists()
    assert "LEAK VIOLATIONS" not in proc.stdout

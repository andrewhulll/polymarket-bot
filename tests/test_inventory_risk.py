"""Inventory rebuild, lifecycle and hard-limit properties."""
from __future__ import annotations

import random

import pytest

from combo_mm.inventory import InventoryProvider
from combo_mm.pricer import PricerResult
from combo_mm.risk import InventoryState
from combo_mm.risk_config import RiskConfig
from combo_mm.risk_policy import InventoryRiskCheck
from combo_mm.store import EventStore


def _draft(qty=100):
    return PricerResult(
        rfq_id="R", model_version="test", params_version="test",
        fair_value=0.5, marginals={}, naive_product=0.5,
        corr_adjustment_bps=0, confidence=1, unquotable_reason=None,
        legs_snapshot_hash="", decided_at="2026-09-17T00:00:00Z",
        extra={"buy_qty": str(qty), "sell_qty": str(qty),
               "buy_price": 0.52, "sell_price": 0.48,
               "symbol": "COMBO", "markets": ("LEG",), "teams": ("KC", "BUF")})


def test_pending_fill_release_and_rebuild():
    store = EventStore()
    with store._conn:
        store._conn.execute(
            "INSERT INTO rfq(rfq_id,symbol,status) VALUES ('R','COMBO','OPEN')")
        store._conn.execute(
            "INSERT INTO rfq_legs(rfq_id,symbol,side) VALUES ('R','LEG','YES')")
    store.record_shadow_draft(quote_id="Q", rfq_id="R", symbol="COMBO",
                              buy_price=.52, sell_price=.48,
                              buy_qty="100", sell_qty="100",
                              decided_at="2026-09-17T00:00:00Z")
    provider = InventoryProvider(store)
    first = provider("2026-09-17T00:00:01Z")
    assert first.pending == {"COMBO": 48.0}
    assert first.executed == {}
    assert first.buying_power == 49952.0
    assert store.record_fill(fill_id="F", rfq_id="R", quote_id="Q",
                             symbol="COMBO", side="BUY", price=.48, qty=25,
                             executed_time="2026-09-17T00:00:02Z")
    assert not store.record_fill(fill_id="F", rfq_id="R", quote_id="Q",
                                 symbol="COMBO", side="BUY", price=.48, qty=25,
                                 executed_time="2026-09-17T00:00:02Z")
    second = provider("2026-09-17T00:00:03Z")
    assert second.pending["COMBO"] == 48.0  # opposite side may still fill
    assert second.executed["COMBO"] == 12.0
    with store._conn:
        store._conn.execute("UPDATE rfq SET status='CLOSED' WHERE rfq_id='R'")
    final = provider("2026-09-17T00:00:04Z")
    assert final.pending == {}
    assert final.executed == {"COMBO": 12.0}
    assert final.buying_power == 49988.0
    provider.record("2026-09-17T00:00:04Z", "terminal:R")
    row = store._conn.execute(
        "SELECT total_wcl FROM exposure_snapshots WHERE source_id='terminal:R' "
        "AND level='portfolio'").fetchone()
    assert row["total_wcl"] == 12.0


def test_live_open_status_reserves_only_until_submission_deadline():
    store = EventStore()
    with store._conn:
        store._conn.execute(
            "INSERT INTO rfq(rfq_id,symbol,status) VALUES ('R','COMBO','RFQ_STATUS_OPEN')")
    store.upsert_rfq_screen("R", n_legs=1, n_resolved=1, n_nfl_legs=1,
                            screen="QUOTABLE", rank=0, catalog_version=1,
                            submission_deadline="1789716300000")
    store.record_shadow_draft(quote_id="Q", rfq_id="R", symbol="COMBO",
                              buy_price=.5, sell_price=.5,
                              buy_qty="100", sell_qty="100",
                              decided_at="2026-09-18T07:23:00Z")
    provider = InventoryProvider(store)
    assert provider("2026-09-18T07:24:00Z").pending == {"COMBO": 50.0}
    assert provider("2026-09-18T07:25:00Z").pending == {}


def test_policy_randomized_post_trade_caps_and_monotone_size():
    cfg = RiskConfig(policy="inventory", max_game_loss=500,
                     max_market_loss=500, max_team_loss=500,
                     max_portfolio_loss=500)
    check = InventoryRiskCheck(cfg)
    rng = random.Random(3)
    for _ in range(500):
        used = rng.uniform(0, 600)
        qty = rng.randint(1, 2000)
        inv = InventoryState(exposures={"G": used}, markets={"LEG": used},
                             teams={"KC": used, "BUF": used},
                             buying_power=50000-used, net_by_game={"G": used})
        verdict = check.check(_draft(qty), qty * .5, inv, "G")
        if verdict.ok:
            assert verdict.exposure_after["game"] <= cfg.max_game_loss + 1e-8
            assert verdict.exposure_after["market"] <= cfg.max_market_loss + 1e-8
            assert verdict.exposure_after["team"] <= cfg.max_team_loss + 1e-8
            assert verdict.exposure_after["portfolio"] <= cfg.max_portfolio_loss + 1e-8
            assert verdict.adjusted_buy_price >= .501
            assert verdict.adjusted_sell_price <= .499
    sizes = []
    for used in range(0, 501, 25):
        inv = InventoryState(exposures={"G": float(used)},
                             markets={"LEG": float(used)},
                             teams={"KC": float(used), "BUF": float(used)},
                             buying_power=50000-used)
        verdict = check.check(_draft(1000), 500, inv, "G")
        sizes.append(max(int(verdict.adjusted_buy_qty), int(verdict.adjusted_sell_qty)))
    assert sizes == sorted(sizes, reverse=True)


def test_kill_switch_latches_until_explicit_reset():
    store = EventStore()
    provider = InventoryProvider(store)
    store.set_kill_switch(True, ts="2026-09-17T00:00:00Z", reason="manual test")
    assert provider().kill_switch
    assert not InventoryRiskCheck(RiskConfig(policy="inventory")).check(
        _draft(), 50, provider(), "G").ok
    store.set_kill_switch(False, ts="2026-09-17T00:01:00Z", reason="reviewed")
    assert not provider().kill_switch


def test_widen_fires_above_soft_utilization():
    cfg = RiskConfig(policy="inventory", max_game_loss=5000.0)
    check = InventoryRiskCheck(cfg)
    inv = InventoryState(exposures={"G": 4500.0}, markets={"LEG": 0.0},
                         teams={}, buying_power=45500.0, net_by_game={"G": 0.0})
    verdict = check.check(_draft(100), 50.0, inv, "G")
    assert verdict.ok
    assert verdict.action == "widen"
    assert verdict.widen_bps > 0
    assert verdict.skew_bps == 0
    assert verdict.adjusted_buy_price > 0.52   # offer pushed up
    assert verdict.adjusted_sell_price < 0.48  # bid pushed down


def test_skew_tilts_against_net_position():
    cfg = RiskConfig(policy="inventory", max_game_loss=5000.0)
    check = InventoryRiskCheck(cfg)
    inv = InventoryState(exposures={"G": 1000.0}, markets={"LEG": 0.0},
                         teams={}, buying_power=49000.0, net_by_game={"G": 2000.0})
    verdict = check.check(_draft(100), 50.0, inv, "G")
    assert verdict.ok
    assert verdict.action == "skew"
    assert verdict.widen_bps == 0
    assert verdict.skew_bps > 0  # long inventory: skew positive
    assert verdict.adjusted_buy_price < 0.52   # discourage buying more
    assert verdict.adjusted_sell_price < 0.48  # encourage selling


def test_offsetting_fills_release_executed_risk_and_realize_pnl():
    store = EventStore()
    with store._conn:
        store._conn.execute(
            "INSERT INTO rfq(rfq_id,symbol,status) VALUES ('R','COMBO','CLOSED')")
    for fill_id, side, price, ts in (
        ("F1", "BUY", .4, "2026-09-17T00:00:00Z"),
        ("F2", "SELL", .6, "2026-09-17T00:01:00Z"),
    ):
        store.record_fill(fill_id=fill_id, rfq_id="R", quote_id=None,
                          symbol="COMBO", side=side, price=price, qty=100,
                          executed_time=ts)
    snapshot = InventoryProvider(store)()
    assert snapshot.executed == {}
    assert snapshot.realized_pnl == pytest.approx(20.0)
    assert snapshot.buying_power == pytest.approx(50020.0)

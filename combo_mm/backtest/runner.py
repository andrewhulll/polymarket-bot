"""Chronological backtest runner (issue #5 item B).

Replays a generated NFL RFQ dataset in strict chronological order through
the real shadow quoting engine, interleaved with the counterfactual fill
model: when the engine quotes an RFQ the fill-model decision is scheduled
for ``t_rfq + requester_think_ms``; as the clock advances past each due
decision the outcome's events (accept/confirm/execute/fill, or a
competitor close) are applied at their own exchange timestamps, merged in
order with the dataset's own events. Settlements land at their actual
timestamps; nothing is ever marked before it happens.

Paper/shadow only: the runner never submits RFQs, quotes, confirmations or
orders, and makes no network calls.
"""
from __future__ import annotations

import heapq
import itertools
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from combo_mm.books import LegBookCache
from combo_mm.engine import ShadowQuotingEngine
from combo_mm.config import PipelineConfig
from combo_mm.normalize import normalize
from combo_mm.reference import ReferenceCache
from combo_mm.store import EventStore

from combo_mm.backtest.dataset import Dataset
from combo_mm.backtest.fill_model import (
    FillModelConfig, FillModelOutcome, simulate_one)
from combo_mm.backtest.leak_guard import instrumented_run
from combo_mm.backtest.metrics import MetricsContext, BacktestMetrics, compute

__all__ = ["BacktestConfig", "RunResult", "run_backtest"]


@dataclass
class BacktestConfig:
    pricer: Any = None                       # engine pricer instance
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    fill: FillModelConfig = field(default_factory=FillModelConfig)
    quote_latency_ms: int = 150              # decided_at + latency <= deadline
    store_path: Optional[str] = None         # where report.py persists it
    params_version: Optional[str] = None


@dataclass
class RunResult:
    dataset: Dataset
    config: BacktestConfig
    store: EventStore
    metrics: BacktestMetrics
    outcomes: List[FillModelOutcome]
    wall_seconds: float
    state_digest: str
    leak_violations: List[str]


class _StaticComboTransport:
    """ReferenceCache transport backed by the dataset's combos.json."""

    def __init__(self, dataset: Dataset) -> None:
        self._dataset = dataset

    def get_combos(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._dataset.combos_for_symbol(symbol)


def _raw_for(item: Dict[str, Any]) -> Dict[str, Any]:
    raw = {k: v for k, v in item.items() if k != "kind"}
    raw["event_type"] = item["kind"]
    return raw


def _snapshot_draft(snapshot: Dict[str, Any], quote_row: Dict[str, Any],
                  rfq_row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    rfq_row = rfq_row or {}
    cash = rfq_row.get("cash_order_qty")
    return {
        "quote_id": quote_row.get("quote_id"),
        "symbol": quote_row.get("symbol"),
        "buy_price": snapshot.get("buy_price"),
        "sell_price": snapshot.get("sell_price"),
        "buy_qty": snapshot.get("buy_qty"),
        "sell_qty": snapshot.get("sell_qty"),
        "decided_at": snapshot.get("decided_at"),
        "fair": snapshot.get("fair_value"),
        "request_type": "CASH" if cash else "QUANTITY",
        "cash": cash,
    }


def _latest_draft(store: EventStore, rfq_id: str) -> Optional[Dict[str, Any]]:
    """Latest engine draft for an RFQ (quote rows are id-ordered)."""
    draft = None
    rfq_row = store.get_rfq(rfq_id)
    for q in store.get_quotes_for_rfq(rfq_id):
        snap_raw = q.get("input_snapshot_json")
        if not snap_raw:
            continue
        try:
            snap = json.loads(snap_raw)
        except (TypeError, ValueError):
            continue
        draft = _snapshot_draft(snap, q, rfq_row)
    return draft


def _iso_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00"))
               .timestamp() * 1000)


def run_backtest(dataset: Dataset, cfg: Optional[BacktestConfig] = None) -> RunResult:
    """Replay ``dataset`` chronologically; return store, metrics, outcomes.

    Single merged pass: dataset items stream in ``t`` order; each engine
    draft schedules a fill-model decision for ``t_rfq + think_ms``; due
    decisions are simulated and their events applied at their own exchange
    timestamps before the clock advances past them. The store therefore sees
    accepts/confirms/executes/fills strictly before later closes and
    settlements, exactly as a live feed would deliver them.
    """
    cfg = cfg or BacktestConfig()
    if cfg.pricer is None:
        raise ValueError("BacktestConfig.pricer is required")
    t0 = time.perf_counter()

    store = EventStore(cfg.store_path or ":memory:")
    reference = ReferenceCache(transport=_StaticComboTransport(dataset))
    books = LegBookCache(staleness_ms=cfg.pipeline.staleness_ms)
    engine = ShadowQuotingEngine(store, books, reference, cfg.pipeline,
                                 pricer=cfg.pricer,
                                 params_version=cfg.params_version)

    rfq_info: Dict[str, Dict[str, Any]] = {}
    settle_ts: Dict[str, str] = {}
    due: List[Tuple[int, int, str]] = []        # (decision_ms, seq, rfq_id)
    pending: List[Tuple[int, int, Dict[str, Any]]] = []  # (ts_ms, seq, event)
    outcomes: List[FillModelOutcome] = []
    seq = itertools.count()
    last_t = -1

    def drain_events(upto_ms: int) -> None:
        while pending and pending[0][0] <= upto_ms:
            _, _, raw = heapq.heappop(pending)
            store.apply(normalize(dict(raw)), source="backtest_sim")

    def settle_due(upto_ms: int) -> None:
        """Simulate every fill-model decision due by ``upto_ms``."""
        while due and due[0][0] <= upto_ms:
            _, _, rfq_id = heapq.heappop(due)
            draft = _latest_draft(store, rfq_id)
            if draft is None:
                continue
            outcome = simulate_one(
                cfg.fill, dataset.root, rfq_id, draft,
                rfq_info.get(rfq_id, {}),
                latency_ms=cfg.quote_latency_ms,
            )
            outcomes.append(outcome)
            for ev in outcome.events:
                heapq.heappush(pending,
                               (_iso_ms(ev["exchange_ts"]), next(seq), ev))

    with instrumented_run():
        for item in dataset.session():
            t = int(item.get("t", 0))
            assert t >= last_t, f"dataset not chronological: t={t} after {last_t}"
            last_t = t
            t_ms = dataset.base_ts_ms(t)
            settle_due(t_ms)
            drain_events(t_ms)

            kind = item.get("kind")
            if kind == "book":
                books.update(item["symbol"], item.get("bid"), item.get("ask"),
                             item.get("bid_size"), item.get("ask_size"),
                             updated_at=item["ts"], seq=item.get("seq"))
                continue
            if kind == "event":
                raw = item.get("raw") or {}
                kind = raw.get("event_type")
            else:
                raw = None
            if kind not in ("rfq_created", "rfq_closed", "rfq_updated"):
                continue  # accept/confirm/execute/fill come from the fill model
            event = normalize(raw if raw is not None else _raw_for(item))
            store.apply(event, source="backtest_sim")
            if kind == "rfq_created":
                # submissionDeadline is not part of the normalized payload;
                # read it from the raw wire shape for the fill-model late gate.
                raw_payload = (raw.get("payload") or {}) if raw else {}
                rfq_id = event.rfq_id or ""
                rfq_info[rfq_id] = {
                    "t_ms": t_ms,
                    "submission_deadline": raw_payload.get("submissionDeadline"),
                    "symbol": event.symbol,
                }
                engine.maybe_quote(event)
                if _latest_draft(store, rfq_id) is not None:
                    heapq.heappush(
                        due, (t_ms + cfg.fill.requester_think_ms,
                              next(seq), rfq_id))
            elif kind == "rfq_updated":
                settle_ts[event.rfq_id or ""] = item["ts"]

        # Flush: decisions due after the last dataset item, then all events.
        settle_due(1 << 62)
        drain_events(1 << 62)

    outcomes.sort(key=lambda oc: oc.rfq_id)
    digest = store.state_digest()

    # -- metrics context (registry + combos only: never the sidecar) ------------
    combo_legs: Dict[str, List[str]] = {}
    for combo in dataset.combos:
        combo_legs[combo["symbol"]] = [leg["symbol"] for leg in combo["legs"]]
    reg_markets = (dataset.markets.get("markets")
                   if isinstance(dataset.markets, dict) else None) or []
    leg_kind = {m["symbol"]: m.get("kind", "Unknown") for m in reg_markets}
    leg_game = {m["symbol"]: m.get("game_id", "") for m in reg_markets}
    main_spread = {m["game_id"]: abs(float(m["line"])) for m in reg_markets
                   if m.get("kind") == "SPR" and m.get("is_main_line")
                   and m.get("line") is not None}
    game_id, fav_abs = {}, {}
    for rfq_id, info in rfq_info.items():
        legs = combo_legs.get(info.get("symbol") or "", [])
        gid = leg_game.get(legs[0], "") if legs else ""
        game_id[rfq_id] = gid
        if gid in main_spread:
            fav_abs[rfq_id] = main_spread[gid]
    ctx = MetricsContext(
        leg_kind=leg_kind,
        fav_abs_spread=fav_abs,
        outcomes={oc.rfq_id: oc.outcome for oc in outcomes},
        requester_type={oc.rfq_id: oc.requester_type for oc in outcomes},
        game_id=game_id,
        settle_ts=settle_ts,
    )
    metrics = compute(store, ctx)

    from combo_mm.backtest.leak_guard import guard as _guard
    return RunResult(
        dataset=dataset, config=cfg, store=store, metrics=metrics,
        outcomes=outcomes, wall_seconds=time.perf_counter() - t0,
        state_digest=digest, leak_violations=list(_guard().violations),
    )

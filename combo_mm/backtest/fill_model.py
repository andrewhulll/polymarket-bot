"""Counterfactual fill model for the NFL backtest (issue #5 item B2).

For every RFQ the engine quoted, the fill model decides what a requester
would have done given our quote, a possible competitor, and the requester's
(private) valuation. This is the **only** module allowed to read the
dataset sidecar: valuations are derived from the sidecar's closing
fairs/competitor quotes, which are future information for everyone else.

Requester behavior (defaults from the issue #5 implementation plan):

- think time 1,000 ms, then a decision at ``t_rfq + think_ms``;
- competitor present with p=0.8, quoting
  ``naive_fair_at_request +/- 2.5c``;
- retail (95%) values the combo at ``closing_naive_fair + N(+1c, 1c)``;
- sharp (5%) values it at ``closing_model_fair + N(0, 0.5c)``;
- the requester lifts the best price inside ``valuation +/- 0.5c``
  tolerance (ties split 50/50);
- a BUY requester takes the lowest offer, a SELL requester the highest bid.

Outcomes per RFQ: ``filled`` | ``lost_to_competitor`` |
``expired_no_trade`` | ``late_quote`` (our quote would have missed the
submission deadline) | ``confirm_rejected`` (only when
``confirm_reject_prob > 0``).

A fill emits, deterministically: ``quote_accepted`` -> ``quote_confirmed``
-> ``quote_executed`` -> ``drop_copy_fill``. A competitor win emits
``rfq_closed``. No trade emits nothing (the dataset's neutral close still
lands at the deadline). All seeds are fixed; identical inputs give
identical event streams.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from datetime import datetime, timezone
from combo_mm.backtest.leak_guard import guard

__all__ = [
    "FillModelConfig",
    "FillModelOutcome",
    "load_sidecar_attrs",
    "simulate_fills",
    "simulate_one",
]

_EPS = 1e-12


@dataclass
class FillModelConfig:
    requester_think_ms: int = 1000
    competitor_presence: float = 0.8
    competitor_half_spread: float = 0.025
    retail_share: float = 0.95
    retail_bias_mean: float = 0.01
    retail_bias_sd: float = 0.01
    sharp_valuation_sd: float = 0.005
    sharp_share: Optional[float] = None  # None -> use the sidecar label
    tolerance: float = 0.005
    tie_share: float = 0.5
    buy_share: float = 0.9
    confirm_reject_prob: float = 0.0
    seed: int = 7


@dataclass
class FillModelOutcome:
    rfq_id: str
    outcome: str
    requester_type: str
    requester_side: str
    decision_ts: str
    our_price: Optional[float] = None
    competitor_bid: Optional[float] = None
    competitor_offer: Optional[float] = None
    valuation: Optional[float] = None
    fill_side: Optional[str] = None
    fill_qty: Optional[float] = None
    fill_price: Optional[float] = None
    events: List[Dict[str, Any]] = field(default_factory=list)


def _sidecar_path(dataset_root: Path) -> Path:
    return Path(dataset_root) / "sidecar.jsonl.gz"


def load_sidecar_attrs(dataset_root: Any, rfq_id: str) -> Dict[str, Any]:
    """Read one RFQ's sidecar attrs. ONLY the fill model may call this.

    Raises :class:`SidecarLeak` when called outside the fill-model scope.
    """
    guard().check(f"load_sidecar_attrs({rfq_id})")
    path = _sidecar_path(Path(dataset_root))
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("rfq_id") == rfq_id:
                return rec
    raise KeyError(f"no sidecar record for {rfq_id}")


def _iter_sidecar(dataset_root: Path) -> Iterator[Dict[str, Any]]:
    guard().check("iter_sidecar")
    with gzip.open(_sidecar_path(dataset_root), "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def _rng(seed: int, rfq_id: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{rfq_id}".encode()).digest()
    return random.Random(digest)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_to_ms(value: Any) -> Optional[int]:
    """Parse an ISO-8601 timestamp to epoch ms; None when unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


def _arrives_after_deadline(decided_at: Any, deadline: Any,
                            latency_ms: float) -> bool:
    """True when ``decided_at + latency_ms`` misses the submission deadline.

    Falls back to the legacy plain string comparison when either timestamp
    is unparseable (both are ISO-8601 ``Z`` in practice).
    """
    d_ms = _iso_to_ms(decided_at)
    dl_ms = _iso_to_ms(deadline)
    if d_ms is not None and dl_ms is not None:
        return d_ms + latency_ms > dl_ms
    return bool(decided_at and deadline and decided_at > deadline)


def _cash_qty(cash: Any, price: float) -> float:
    try:
        cash_f = float(cash)
    except (TypeError, ValueError):
        return 0.0
    if price <= 0:
        return 0.0
    return math.floor(cash_f / price)


def simulate_fills(dataset_root: Any,
                   drafts: Dict[str, Dict[str, Any]],
                   rfq_info: Dict[str, Dict[str, Any]],
                   cfg: Optional[FillModelConfig] = None,
                   *,
                   latency_ms: float = 0.0) -> List[FillModelOutcome]:
    """Run the counterfactual fill model over all quoted RFQs.

    ``drafts``: rfq_id -> draft snapshot dict (quote_id, buy_price,
    sell_price, buy_qty, sell_qty, decided_at (ISO), fair, request_type,
    cash). ``rfq_info``: rfq_id -> {t_ms, submission_deadline (ISO)}.
    ``latency_ms``: quote latency added to ``decided_at`` for the
    submission-deadline gate.
    """
    cfg = cfg or FillModelConfig()
    root = Path(dataset_root)
    outcomes: List[FillModelOutcome] = []
    with guard().allow():
        sidecars = {rec["rfq_id"]: rec for rec in _iter_sidecar(root)}
    for rfq_id, draft in drafts.items():
        info = rfq_info.get(rfq_id, {})
        rec = sidecars.get(rfq_id)
        outcomes.append(_simulate_one(cfg, root, rfq_id, draft, info, rec,
                                      latency_ms=latency_ms))
    return outcomes


def simulate_one(cfg: FillModelConfig,
                 dataset_root: Any,
                 rfq_id: str,
                 draft: Dict[str, Any],
                 info: Dict[str, Any],
                 *,
                 latency_ms: float = 0.0) -> FillModelOutcome:
    """Simulate a single RFQ (the interleaved runner's ``due(t)`` entry).

    Reads that RFQ's sidecar record inside the fill-model allow() scope;
    without a sidecar record (scripted/fixture mode) the outcome is
    ``expired_no_trade``. The per-RFQ RNG is seeded by ``(seed, rfq_id)``,
    so evaluation order never changes the outcome.
    """
    cfg = cfg or FillModelConfig()
    root = Path(dataset_root)
    with guard().allow():
        try:
            rec: Optional[Dict[str, Any]] = load_sidecar_attrs(root, rfq_id)
        except KeyError:
            rec = None
    return _simulate_one(cfg, root, rfq_id, draft, info, rec,
                         latency_ms=latency_ms)


def _simulate_one(cfg: FillModelConfig, root: Path, rfq_id: str,
                  draft: Dict[str, Any], info: Dict[str, Any],
                  rec: Optional[Dict[str, Any]],
                  *,
                  latency_ms: float = 0.0) -> FillModelOutcome:
    t_ms = int(info.get("t_ms", 0))
    decision_ms = t_ms + cfg.requester_think_ms
    decision_ts = _iso(decision_ms)

    # Late gate: our quote only counts if it would have reached the venue
    # in time: decided_at + quote_latency_ms <= submission_deadline.
    decided_at = draft.get("decided_at") or ""
    deadline = info.get("submission_deadline") or draft.get("submission_deadline") or ""
    late = _arrives_after_deadline(decided_at, deadline, latency_ms)
    rng = _rng(cfg.seed, rfq_id)

    if cfg.sharp_share is not None:
        rtype = "sharp" if rng.random() < cfg.sharp_share else "retail"
    elif rec is not None:
        rtype = rec.get("requester_type", "retail")
    else:
        rtype = "sharp" if rng.random() > cfg.retail_share else "retail"
    rside = "BUY" if rng.random() < cfg.buy_share else "SELL"

    outcome = FillModelOutcome(
        rfq_id=rfq_id, outcome="", requester_type=rtype,
        requester_side=rside, decision_ts=decision_ts)

    if late:
        outcome.outcome = "late_quote"
        return outcome
    if rec is None:                      # scripted/fixture mode: no sidecar
        outcome.outcome = "expired_no_trade"
        return outcome

    # --- requester valuation (future info: sidecar only) -------------------
    # NOTE: `rec` was loaded inside the fill-model allow() scope by
    # simulate_fills(); nothing outside this module may touch it.
    if rtype == "sharp":
        valuation = float(rec["closing_model_fair"]) + rng.gauss(0.0, cfg.sharp_valuation_sd)
    else:
        valuation = float(rec["closing_naive_fair"]) + rng.gauss(cfg.retail_bias_mean, cfg.retail_bias_sd)
    naive_at_req = float(rec["naive_fair_at_request"])
    outcome.valuation = valuation

    # --- competitor ---------------------------------------------------------
    comp_bid = comp_offer = None
    if rng.random() < cfg.competitor_presence:
        comp_offer = _clamp01(naive_at_req + cfg.competitor_half_spread)
        comp_bid = _clamp01(naive_at_req - cfg.competitor_half_spread)
    outcome.competitor_bid, outcome.competitor_offer = comp_bid, comp_offer

    # --- our price -----------------------------------------------------------
    # Pricing-module convention (combo_mm/pricing.py): buy_price is OUR OFFER
    # (the requester buys at this price), sell_price is OUR BID.
    if rside == "BUY":
        our_price = float(draft.get("buy_price") or 0.0)    # our offer
    else:
        our_price = float(draft.get("sell_price") or 0.0)   # our bid
    outcome.our_price = our_price or None

    # --- requester decision ---------------------------------------------------
    candidates = []  # (price, who)
    if our_price and our_price > 0:
        candidates.append((our_price, "us"))
    if rside == "BUY" and comp_offer is not None:
        candidates.append((comp_offer, "competitor"))
    if rside == "SELL" and comp_bid is not None:
        candidates.append((comp_bid, "competitor"))
    if not candidates:
        outcome.outcome = "expired_no_trade"
        return outcome

    if rside == "BUY":
        best = min(p for p, _ in candidates)
        in_tol = valuation + cfg.tolerance
        trades = best <= in_tol + _EPS
        winners = [w for p, w in candidates if abs(p - best) <= _EPS]
    else:
        best = max(p for p, _ in candidates)
        in_tol = valuation - cfg.tolerance
        trades = best >= in_tol - _EPS
        winners = [w for p, w in candidates if abs(p - best) <= _EPS]

    if not trades:
        outcome.outcome = "expired_no_trade"
        return outcome
    if "us" in winners and "competitor" in winners:
        winner = "us" if rng.random() < cfg.tie_share else "competitor"
    else:
        winner = winners[0]
    if winner != "us":
        outcome.outcome = "lost_to_competitor"
        outcome.events.append({
            "event_type": "rfq_closed",
            "event_id": f"sim-{rfq_id}-closed",
            "rfq_id": rfq_id,
            "symbol": draft.get("symbol"),
            "exchange_ts": decision_ts,
            "payload": {"reason": "competitor_fill"},
        })
        return outcome

    # --- our win --------------------------------------------------------------
    # Fill side is ours: the requester bought -> we sold (and vice versa).
    fill_side = "SELL" if rside == "BUY" else "BUY"
    if draft.get("request_type") == "CASH":
        qty = _cash_qty(draft.get("cash"), our_price)
    else:
        qty = float(draft.get("sell_qty") or 0.0) if fill_side == "SELL" \
            else float(draft.get("buy_qty") or 0.0)
    if qty <= 0:
        outcome.outcome = "expired_no_trade"
        return outcome
    outcome.fill_side, outcome.fill_qty, outcome.fill_price = fill_side, qty, our_price

    qid = draft["quote_id"]
    symbol = draft.get("symbol")
    accepted_side = "BUY" if rside == "BUY" else "SELL"
    if rng.random() < cfg.confirm_reject_prob:
        outcome.outcome = "confirm_rejected"
        outcome.events.append({
            "event_type": "quote_accepted",
            "event_id": f"sim-{rfq_id}-accept",
            "rfq_id": rfq_id, "quote_id": qid, "symbol": symbol,
            "exchange_ts": decision_ts,
            "payload": {"acceptedSide": accepted_side,
                        "acceptedTime": decision_ts},
        })
        outcome.events.append({
            "event_type": "quote_deleted",
            "event_id": f"sim-{rfq_id}-reject",
            "rfq_id": rfq_id, "quote_id": qid, "symbol": symbol,
            "exchange_ts": _iso(decision_ms + 250),
            "payload": {"reason": "confirm_reject"},
        })
        return outcome

    outcome.outcome = "filled"
    accept_ts = decision_ts
    confirm_ts = _iso(decision_ms + 250)
    exec_ts = _iso(decision_ms + 500)
    fill_ts = _iso(decision_ms + 750)
    outcome.events = [
        {"event_type": "quote_accepted", "event_id": f"sim-{rfq_id}-accept",
         "rfq_id": rfq_id, "quote_id": qid, "symbol": symbol,
         "exchange_ts": accept_ts,
         "payload": {"acceptedSide": accepted_side, "acceptedTime": accept_ts,
                     "confirmationDeadline": confirm_ts}},
        {"event_type": "quote_confirmed", "event_id": f"sim-{rfq_id}-confirm",
         "rfq_id": rfq_id, "quote_id": qid, "symbol": symbol,
         "exchange_ts": confirm_ts,
         "payload": {"confirmedTime": confirm_ts,
                     "executionDeadline": exec_ts}},
        {"event_type": "quote_executed", "event_id": f"sim-{rfq_id}-exec",
         "rfq_id": rfq_id, "quote_id": qid, "symbol": symbol,
         "exchange_ts": exec_ts,
         "payload": {"executedTime": exec_ts}},
        {"event_type": "drop_copy_fill", "event_id": f"sim-{rfq_id}-fill",
         "rfq_id": rfq_id, "quote_id": qid, "symbol": symbol,
         "exchange_ts": fill_ts,
         "payload": {"fillId": f"sim-fill-{rfq_id}", "side": fill_side,
                     "price": our_price, "qty": qty, "executedTime": fill_ts}},
    ]
    return outcome

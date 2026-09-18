"""Shared backtest metrics (issue #5 item A).

One implementation of every backtest number, used by the CLI runner, the
dashboard replay path (via :mod:`combo_mm.paper_backtest`) and the NFL
synthetic backtest. The pure helpers (:func:`swings`, :func:`brier_score`)
are also imported by ``dashboard/live_view_models.py`` so the dashboard's
Performance tab cannot drift from the CLI.

Conventions
-----------
- Downswings/upswings are **positive magnitudes** (peak-to-trough drop,
  trough-to-peak rise), each with the peak/trough timestamps.
- ``executed`` means "at least one fill recorded for the RFQ" (fills
  reconcile exclusively through Drop Copy / the fill model).
- Expected P&L is ex-ante **per fill**: ``(our price - model fair at
  decision) * qty`` signed by side. The legacy "quoted half-spread over all
  quotes" number is kept as ``quoted_edge_notional`` (relabeled, not removed).
- The mark-to-model curve values open positions at the latest draft fair
  known for that RFQ; it exists so downswing/upswing are meaningful before
  any settlement arrives.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "MetricsContext",
    "BacktestMetrics",
    "swings",
    "brier_score",
    "combo_settlement_value",
    "compute",
]

# ---------------------------------------------------------------------------
# Pure helpers (shared with dashboard + synthetic backtest)
# ---------------------------------------------------------------------------

def swings(points: Iterable[Tuple[str, float]]) -> Dict[str, Any]:
    """Max downswing/upswing of a ``(ts, value)`` curve.

    Curves are P&L curves starting from a flat 0 baseline, so the series is
    anchored at ``(first_ts, 0.0)`` before measuring (matches the legacy
    dashboard/paper-backtest convention). Returns positive magnitudes plus
    the peak/trough timestamps that bound each swing. Empty input -> all
    zeros / None timestamps.
    """
    pts = sorted(((ts, float(v)) for ts, v in points), key=lambda p: p[0])
    if not pts:
        return {"max_downswing": 0.0, "max_upswing": 0.0,
                "dd_peak_at": None, "dd_trough_at": None,
                "up_trough_at": None, "up_peak_at": None}
    pts = [(pts[0][0], 0.0)] + pts
    peak = trough = pts[0][1]
    peak_at = trough_at = pts[0][0]
    max_dd = max_up = 0.0
    dd_peak_at = dd_trough_at = up_trough_at = up_peak_at = pts[0][0]
    for ts, value in pts[1:]:
        if value > peak:
            peak, peak_at = value, ts
        if value < trough:
            trough, trough_at = value, ts
        dd = peak - value
        if dd > max_dd:
            max_dd, dd_peak_at, dd_trough_at = dd, peak_at, ts
        up = value - trough
        if up > max_up:
            max_up, up_trough_at, up_peak_at = up, trough_at, ts
    return {"max_downswing": max_dd, "max_upswing": max_up,
            "dd_peak_at": dd_peak_at, "dd_trough_at": dd_trough_at,
            "up_trough_at": up_trough_at, "up_peak_at": up_peak_at}


def brier_score(pairs: Iterable[Tuple[float, float]]) -> Optional[float]:
    """Mean squared error of probabilistic forecasts vs 0/1 outcomes."""
    vals = [(float(p), float(o)) for p, o in pairs]
    if not vals:
        return None
    return sum((p - o) ** 2 for p, o in vals) / len(vals)


def combo_settlement_value(legs: List[Dict[str, Any]]) -> Optional[float]:
    """Combo settlement from RAW leg settlements: product of q_i.

    ``settlement_price`` is the leg's own YES/LONG result in [0,1]; the
    YES/NO inversion happens here (and only here), never at ingest.
    Returns None when any leg lacks a settlement price (not settled yet).
    """
    value = 1.0
    for leg in legs:
        sp = leg.get("settlement_price")
        if sp is None:
            return None
        sp = float(sp)
        value *= sp if leg.get("side") == "YES" else (1.0 - sp)
    return value


# ---------------------------------------------------------------------------
# Context + result
# ---------------------------------------------------------------------------

@dataclass
class MetricsContext:
    """Optional per-run lookups the store alone cannot provide."""

    leg_kind: Dict[str, str] = field(default_factory=dict)
    """symbol -> leg kind (``ML``/``SPR``/``TOT``/``TT``); missing -> Unknown."""

    fav_abs_spread: Dict[str, float] = field(default_factory=dict)
    """rfq_id -> |spread| of the game's main line, for favourite buckets."""

    outcomes: Dict[str, str] = field(default_factory=dict)
    """rfq_id -> fill-model outcome: filled | lost_to_competitor |
    expired_no_trade | late_quote."""

    requester_type: Dict[str, str] = field(default_factory=dict)
    """rfq_id -> sharp | retail (from the fill-model outcomes, never from
    future-information files)."""

    game_id: Dict[str, str] = field(default_factory=dict)
    """rfq_id -> game id, for per-game exposure peaks."""

    settle_ts: Dict[str, str] = field(default_factory=dict)
    """rfq_id -> settlement exchange timestamp (ISO). Realized P&L steps
    the curve here; RFQs without one fall back to fill time."""


@dataclass
class BacktestMetrics:
    # -- counts / rates -----------------------------------------------------
    rfqs_received: int = 0
    rfqs_quoted: int = 0
    rfqs_rejected: int = 0
    rfqs_expired: int = 0
    rfqs_executed: int = 0
    lost_to_competitor: int = 0
    late_quotes: int = 0
    quote_rate: float = 0.0
    execution_rate: float = 0.0
    win_rate_vs_competitor: float = 0.0
    # -- P&L ----------------------------------------------------------------
    expected_pnl: float = 0.0            # ex-ante, per fill, vs model fair
    expected_pnl_naive_basis: float = 0.0  # ex-ante, per fill, vs naive product
    quoted_edge_notional: float = 0.0    # legacy: half-spread over all quotes
    realized_pnl: float = 0.0
    n_fills: int = 0
    voided: int = 0
    # -- swings (settlement-time realized curve + mark-to-model curve) ------
    max_downswing: float = 0.0
    max_upswing: float = 0.0
    dd_peak_at: Optional[str] = None
    dd_trough_at: Optional[str] = None
    up_trough_at: Optional[str] = None
    up_peak_at: Optional[str] = None
    max_downswing_mtm: float = 0.0
    max_upswing_mtm: float = 0.0
    mtm_dd_peak_at: Optional[str] = None
    mtm_dd_trough_at: Optional[str] = None
    mtm_up_trough_at: Optional[str] = None
    mtm_up_peak_at: Optional[str] = None
    # -- calibration --------------------------------------------------------
    brier_ours: Optional[float] = None
    brier_naive: Optional[float] = None
    n_brier: int = 0
    # -- curves ---------------------------------------------------------------
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    realized_curve: List[Tuple[str, float]] = field(default_factory=list)
    mtm_curve: List[Tuple[str, float]] = field(default_factory=list)
    exposure_curve: List[Dict[str, Any]] = field(default_factory=list)
    peak_additive_exposure: float = 0.0
    peak_correlated_wcl: Optional[float] = None
    correlated_wcl_series: List[Tuple[str, float]] = field(default_factory=list)
    # -- breakdowns -----------------------------------------------------------
    by_market_type: List[Dict[str, Any]] = field(default_factory=list)
    by_combo_size: List[Dict[str, Any]] = field(default_factory=list)
    by_fav_bucket: List[Dict[str, Any]] = field(default_factory=list)
    by_requester_type: List[Dict[str, Any]] = field(default_factory=list)
    by_decline_reason: List[Dict[str, Any]] = field(default_factory=list)
    top_games_by_wcl: List[Dict[str, Any]] = field(default_factory=list)
    # -- per RFQ --------------------------------------------------------------
    per_rfq: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_FAV_BUCKETS = [(0.0, 2.5, "0-2.5"), (2.5, 6.5, "3-6.5"),
                (6.5, 9.5, "7-9.5"), (9.5, math.inf, "10+")]


def _fav_bucket(abs_spread: Optional[float]) -> str:
    if abs_spread is None:
        return "unknown"
    for lo, hi, label in _FAV_BUCKETS:
        if lo <= abs_spread < hi:
            return label
    return "unknown"


def _market_type(symbols: List[str], leg_kind: Dict[str, str]) -> str:
    kinds = sorted({leg_kind.get(s, "Unknown") for s in symbols})
    return "+".join(kinds) if kinds else "Unknown"


def _size_bucket(n: int) -> str:
    return "2" if n <= 2 else "3" if n == 3 else "4+"


def _qty_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def compute(store: Any, ctx: Optional[MetricsContext] = None) -> BacktestMetrics:
    """Compute the full backtest report from a replayed :class:`EventStore`."""
    ctx = ctx or MetricsContext()
    res = BacktestMetrics()

    rfqs = store.list_rfqs()
    res.rfqs_received = len(rfqs)
    rfq_by_id = {r["rfq_id"]: r for r in rfqs}

    # Latest shadow decision per RFQ (DESC by id -> first seen is latest).
    latest: Dict[str, Dict[str, Any]] = {}
    for d in store.get_shadow_decisions(limit=10_000_000):
        latest.setdefault(d["rfq_id"], d)

    # Draft snapshots per quote (fair/naive live in the canonical snapshot).
    draft_fair: Dict[str, float] = {}
    draft_naive: Dict[str, float] = {}
    for r in rfqs:
        for q in store.get_quotes_for_rfq(r["rfq_id"]):
            snap_raw = q.get("input_snapshot_json")
            if not snap_raw:
                continue
            try:
                snap = json.loads(snap_raw)
            except (TypeError, ValueError):
                continue
            qid = q.get("quote_id")
            if qid and "fair_value" in snap:
                draft_fair[qid] = float(snap["fair_value"])
                draft_naive[qid] = float(snap.get("naive_product",
                                                 snap["fair_value"]))

    # Settlements per RFQ.
    settle: Dict[str, Optional[float]] = {}
    for r in rfqs:
        legs = (store.get_rfq(r["rfq_id"]) or {}).get("legs") or []
        settle[r["rfq_id"]] = combo_settlement_value(legs)

    fills = store.get_fills_for_position()  # ordered by executed_time
    res.n_fills = len(fills)
    fills_by_rfq: Dict[str, List[Dict[str, Any]]] = {}
    for f in fills:
        fills_by_rfq.setdefault(f.get("rfq_id") or "", []).append(f)

    # -- counts / rates -------------------------------------------------------
    for r in rfqs:
        rid = r["rfq_id"]
        d = latest.get(rid)
        if d is not None and d.get("decision") == "QUOTED_OK":
            res.rfqs_quoted += 1
        elif d is not None:
            res.rfqs_rejected += 1
        if r.get("status") == "EXPIRED":
            res.rfqs_expired += 1
        if fills_by_rfq.get(rid):
            res.rfqs_executed += 1
        outcome = ctx.outcomes.get(rid)
        if outcome == "lost_to_competitor":
            res.lost_to_competitor += 1
        elif outcome == "late_quote":
            res.late_quotes += 1
    if res.rfqs_received:
        res.quote_rate = res.rfqs_quoted / res.rfqs_received
    if res.rfqs_quoted:
        res.execution_rate = res.rfqs_executed / res.rfqs_quoted
    contested = res.rfqs_executed + res.lost_to_competitor
    if contested:
        res.win_rate_vs_competitor = res.rfqs_executed / contested

    # -- legacy quoted edge notional (half-spread over all quotes) ------------
    quoted_edge = 0.0
    for d in latest.values():
        if d.get("decision") != "QUOTED_OK":
            continue
        r = rfq_by_id.get(d["rfq_id"]) or {}
        qty = _qty_float(r.get("qty_decimal"))
        if not qty:
            sides = [_qty_float(s) for s in (d.get("buy_qty"), d.get("sell_qty"))]
            sides = [s for s in sides if s > 0]
            qty = min(sides) if sides else 0.0
        quoted_edge += _qty_float(d.get("spread_bps")) / 10000.0 * qty
    res.quoted_edge_notional = quoted_edge

    # -- per-fill P&L ----------------------------------------------------------
    # expected (ex-ante): (our price - model fair at decision) * qty, signed.
    exp = exp_naive = 0.0
    realized_events: List[Tuple[str, float, str]] = []  # (ts, pnl, rfq_id)
    for f in fills:
        qty = _qty_float(f.get("qty"))
        price = _qty_float(f.get("price"))
        fair = draft_fair.get(f.get("quote_id") or "", price)
        naive = draft_naive.get(f.get("quote_id") or "", price)
        if f.get("side") == "SELL":      # we sold: edge = price - fair
            exp += (price - fair) * qty
            exp_naive += (price - naive) * qty
        else:                            # we bought: edge = fair - price
            exp += (fair - price) * qty
            exp_naive += (naive - price) * qty
        s = settle.get(f.get("rfq_id") or "")
        if s is not None:
            pnl = ((s - price) * qty if f.get("side") == "BUY"
                   else (price - s) * qty)
            # Realized at settlement time (the settlement-time curve); the
            # legacy fill-time curve is built separately below.
            leg_rows = (store.get_rfq(f["rfq_id"]) or {}).get("legs") or []
            voided = any(leg.get("settlement_price") in (None, "0.5")
                         for leg in leg_rows)
            realized_events.append((f.get("executed_time") or "", pnl,
                                    f.get("rfq_id") or "", voided))
    res.expected_pnl = exp
    res.expected_pnl_naive_basis = exp_naive

    # realized curve stepped at SETTLEMENT time; voids contribute 0.
    # (Legacy dashboard parity kept the fill-time curve as equity_curve.)
    cum = 0.0
    realized_curve: List[Tuple[str, float]] = []
    for ts, pnl, rid, voided in sorted(realized_events, key=lambda e: e[0]):
        if voided:
            res.voided += 1
            continue
        cum += pnl
        realized_curve.append((ctx.settle_ts.get(rid, ts), cum))
    realized_curve.sort(key=lambda p: p[0])
    res.realized_pnl = cum
    res.realized_curve = realized_curve

    # legacy fill-time equity curve (kept for dashboard parity)
    cum = 0.0
    equity: List[Tuple[str, float]] = []
    for ts, pnl, rid, voided in sorted(realized_events, key=lambda e: e[0]):
        if not voided:
            cum += pnl
        equity.append((ts, cum))
    res.equity_curve = equity

    # mark-to-model curve: realized-to-date + open positions at latest fair
    open_pos: Dict[str, List[float]] = {}  # rfq_id -> [signed_qty, price]
    mtm_curve: List[Tuple[str, float]] = []
    cum_r = 0.0
    # latest fair per rfq (for marking)
    fair_by_rfq: Dict[str, float] = {}
    for rid, d in latest.items():
        if d.get("decision") == "QUOTED_OK" and d.get("fair_price") is not None:
            fair_by_rfq[rid] = _qty_float(d.get("fair_price"))
    for f in fills:
        rid = f.get("rfq_id") or ""
        qty = _qty_float(f.get("qty"))
        price = _qty_float(f.get("price"))
        signed = qty if f.get("side") == "BUY" else -qty
        pos = open_pos.setdefault(rid, [0.0, price])
        pos[0] += signed
        if pos[0] == 0.0:
            open_pos.pop(rid, None)
        s = settle.get(rid)
        # a fill whose RFQ already settled realizes immediately in MTM terms
        if s is not None:
            pnl = (s - price) * signed
            cum_r += pnl
            open_pos.pop(rid, None)
        mtm = cum_r + sum(
            q * (fair_by_rfq.get(r, p) - p)
            for r, (q, p) in open_pos.items()
        )
        mtm_curve.append((f.get("executed_time") or "", mtm))
    res.mtm_curve = mtm_curve

    # -- swings -----------------------------------------------------------------
    rs = swings(realized_curve)
    res.max_downswing = rs["max_downswing"]
    res.max_upswing = rs["max_upswing"]
    res.dd_peak_at, res.dd_trough_at = rs["dd_peak_at"], rs["dd_trough_at"]
    res.up_trough_at, res.up_peak_at = rs["up_trough_at"], rs["up_peak_at"]
    ms = swings(mtm_curve)
    res.max_downswing_mtm = ms["max_downswing"]
    res.max_upswing_mtm = ms["max_upswing"]
    res.mtm_dd_peak_at, res.mtm_dd_trough_at = ms["dd_peak_at"], ms["dd_trough_at"]
    res.mtm_up_trough_at, res.mtm_up_peak_at = ms["up_trough_at"], ms["up_peak_at"]

    # -- Brier (our fair vs naive, on settled non-void combos) -------------------
    ours, naive = [], []
    for r in rfqs:
        s = settle.get(r["rfq_id"])
        if s is None:
            continue
        legs = (store.get_rfq(r["rfq_id"]) or {}).get("legs") or []
        if any(leg.get("settlement_price") in (None, "0.5") for leg in legs):
            continue
        d = latest.get(r["rfq_id"])
        if d is None or d.get("decision") != "QUOTED_OK":
            continue
        fair = d.get("fair_price")
        if fair is None:
            continue
        snap_naive = None
        for q in store.get_quotes_for_rfq(r["rfq_id"]):
            snap_naive = draft_naive.get(q.get("quote_id") or "")
            if snap_naive is not None:
                break
        ours.append((float(fair), s))
        naive.append((float(snap_naive if snap_naive is not None else fair), s))
    res.brier_ours = brier_score(ours)
    res.brier_naive = brier_score(naive)
    res.n_brier = len(ours)

    # -- exposure ------------------------------------------------------------------
    # Fallback: additive max-loss from the fills ledger. NOT correlation-aware
    # (see docs/backtest.md); replaced by scenario WCL once #3 lands.
    # Notional uses the last fill price per symbol (matches the historical
    # dashboard definition pinned by tests).
    net: Dict[str, List[float]] = {}  # symbol -> [net_qty, last_price]
    exp_curve: List[Dict[str, Any]] = []
    peak_add = 0.0
    for f in fills:
        sym = f.get("symbol") or ""
        qty = _qty_float(f.get("qty"))
        price = _qty_float(f.get("price"))
        signed = qty if f.get("side") == "BUY" else -qty
        cur_qty, _ = net.get(sym, [0.0, price])
        net[sym] = [cur_qty + signed, price]
        notional = sum(abs(q) * p for q, p in net.values())
        add_loss = sum(abs(q) * (p if q > 0 else 1.0 - p)
                       for q, p in net.values())
        peak_add = max(peak_add, add_loss)
        exp_curve.append({"ts": f.get("executed_time") or "",
                          "net_notional": notional,
                          "additive_max_loss": add_loss,
                          "correlation_aware": False})
    res.exposure_curve = exp_curve
    res.peak_additive_exposure = peak_add

    # Correlated WCL from the inventory provider's snapshots, when present.
    try:
        rows = store._conn.execute(  # noqa: SLF001 - read-only introspection
            "SELECT ts, total_wcl FROM exposure_snapshots "
            "WHERE level='portfolio' AND key='ALL' ORDER BY ts").fetchall()
        wcls = [(r[0], float(r[1])) for r in rows]
        if wcls:
            res.peak_correlated_wcl = max(v for _, v in wcls)
        res.correlated_wcl_series = wcls
    except Exception:
        pass

    # top games by peak additive WCL
    game_peak: Dict[str, float] = {}
    game_of = ctx.game_id
    for f in fills:
        g = game_of.get(f.get("rfq_id") or "", "unknown")
        qty = _qty_float(f.get("qty"))
        price = _qty_float(f.get("price"))
        signed = qty if f.get("side") == "BUY" else -qty
        game_peak[g] = game_peak.get(g, 0.0) + abs(signed) * (
            price if signed > 0 else 1.0 - price)
    res.top_games_by_wcl = [
        {"game_id": g, "peak_additive_wcl": v}
        for g, v in sorted(game_peak.items(), key=lambda kv: -kv[1])[:10]
    ]

    # -- per-RFQ rows ---------------------------------------------------------------
    per_rfq: List[Dict[str, Any]] = []
    for r in rfqs:
        rid = r["rfq_id"]
        d = latest.get(rid)
        legs = (store.get_rfq(rid) or {}).get("legs") or []
        symbols = [str(leg.get("symbol")) for leg in legs]
        rfq_fills = fills_by_rfq.get(rid, [])
        rpnl = 0.0
        for f in rfq_fills:
            s = settle.get(rid)
            if s is None:
                continue
            qty = _qty_float(f.get("qty"))
            price = _qty_float(f.get("price"))
            rpnl += ((s - price) * qty if f.get("side") == "BUY"
                     else (price - s) * qty)
        fair = d.get("fair_price") if d else None
        snap_naive = None
        for q in store.get_quotes_for_rfq(rid):
            snap_naive = draft_naive.get(q.get("quote_id") or "")
            if snap_naive is not None:
                break
        per_rfq.append({
            "rfq_id": rid,
            "symbol": r.get("symbol"),
            "game_id": ctx.game_id.get(rid),
            "t": r.get("created_time"),
            "market_type": _market_type(symbols, ctx.leg_kind),
            "combo_size": len(symbols),
            "fair": fair,
            "naive": snap_naive,
            "lift": (float(fair) / snap_naive
                     if fair and snap_naive else None),
            "decision": d.get("decision") if d else None,
            "outcome": ctx.outcomes.get(rid),
            "requester_type": ctx.requester_type.get(rid),
            "n_fills": len(rfq_fills),
            "fill_side": rfq_fills[0].get("side") if rfq_fills else None,
            "fill_qty": sum(_qty_float(f.get("qty")) for f in rfq_fills) or None,
            "fill_price": (rfq_fills[0].get("price") if rfq_fills else None),
            "settlement": settle.get(rid),
            "realized_pnl": rpnl,
            "status": r.get("status"),
        })
    res.per_rfq = per_rfq

    # -- breakdowns --------------------------------------------------------------------
    def group(rows: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row[key], []).append(row)
        out = []
        for name in sorted(groups, key=str):
            g = groups[name]
            n_q = sum(1 for x in g if x["decision"] == "QUOTED_OK")
            n_e = sum(1 for x in g if x["n_fills"] > 0)
            edges = [float(x["fair"]) for x in g
                     if x["decision"] == "QUOTED_OK" and x["fair"] is not None]
            b_o = [(float(x["fair"]), float(x["settlement"])) for x in g
                   if x["settlement"] not in (None,)
                   and x["fair"] is not None]
            out.append({
                "group": name,
                "received": len(g),
                "quoted": n_q,
                "rejected": sum(1 for x in g
                               if x["decision"] not in (None, "QUOTED_OK")),
                "expired": sum(1 for x in g if x["status"] == "EXPIRED"),
                "executed": n_e,
                "quote_rate": n_q / len(g) if g else 0.0,
                "execution_rate": n_e / n_q if n_q else 0.0,
                "expected_pnl": sum(
                    (float(x["fill_price"]) - float(x["fair"]))
                    * (float(x["fill_qty"] or 0))
                    * (-1 if x["fill_side"] == "BUY" else 1)
                    for x in g
                    if x["fill_price"] is not None and x["fair"] is not None
                    and x["fill_side"] in ("BUY", "SELL")),
                "realized_pnl": sum(x["realized_pnl"] for x in g),
                "brier_ours": brier_score(b_o) if b_o else None,
                "n_brier": len(b_o),
            })
        return out

    res.by_market_type = group(per_rfq, "market_type")
    res.by_combo_size = group(
        [{**x, "combo_size": _size_bucket(x["combo_size"])} for x in per_rfq],
        "combo_size")
    res.by_fav_bucket = group(
        [{**x, "fav_bucket": _fav_bucket(ctx.fav_abs_spread.get(x["rfq_id"]))}
         for x in per_rfq], "fav_bucket")
    res.by_requester_type = group(
        [{**x, "requester_type": x["requester_type"] or "unknown"}
         for x in per_rfq], "requester_type")
    declines = [x for x in per_rfq if x["decision"] not in (None, "QUOTED_OK")]
    res.by_decline_reason = group(
        [{**x, "decline_reason": x["decision"]} for x in declines],
        "decline_reason")
    return res

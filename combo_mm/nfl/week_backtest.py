"""Replay one NFL week's same-game combos as RFQs through the shadow pipeline.

The dashboard's "Run backtest" button (default: 2026 Week 1). No Polymarket
RFQ history is available, so the RFQ flow is reconstructed from the week's
actual games and closing lines; everything downstream is the real pipeline
(normalizer, event store, shadow quoting engine, fills, settlement).

Per game (games with scores and closing lines; pick'ems skipped):

1. **Params, walk-forward.** Score covariance is estimated from games strictly
   before ``(season, week)`` with the frozen estimator
   (``params/estimator.json``). No future information.
2. **Leg books.** Each leg (favorite/underdog moneyline, spread, over/under)
   gets a one-cent-wide book centered on its de-vigged closing probability.
3. **Model.** Score means are calibrated to the spread/total prices
   (:func:`combo_mm.nfl.joint.calibrate_means`); the game is registered with
   :class:`combo_mm.nfl.joint_pricer.NflJointPricer`.
4. **RFQs.** One RFQ per same-game combo type
   (:data:`combo_mm.nfl.synthetic_backtest.COMBOS`, 2-3 legs), arriving in the
   three hours before kickoff, with a deterministic size and requester side
   (about 3 in 4 requesters buy, as parlay flow does).
5. **Quote.** The shadow engine prices each RFQ with the joint model.
6. **Competition.** A naive maker quotes the same RFQ with the V1
   independent-leg pricer on the same books and the same spread knobs. The
   requester trades with whoever shows the better price on their side (ties
   go to the competitor). So we trade exactly where dependence moves our
   price past the naive one -- the realistic adverse-selection test.
7. **Lifecycle + settlement.** Wins produce quote created / accepted /
   confirmed / executed events and a fill; losses close the RFQ. After the
   game, leg settlements arrive on an ``rfq_updated``. A pushed leg is left
   unsettled, which voids the combo (no P&L).

Deterministic for a fixed game list and estimator.
"""
from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from combo_mm.books import LegBookCache
from combo_mm.config import PipelineConfig
from combo_mm.engine import ShadowQuotingEngine
from combo_mm.fixtures import SELF_USER_ID
from combo_mm.nfl.estimate import EstimatorConfig, ResidualTable, estimate_params
from combo_mm.nfl.ingest import Game, RawPull, latest_pull, load_games
from combo_mm.nfl.joint import PUSH, WIN, GameModel, calibrate_means, settle_leg
from combo_mm.nfl.joint_pricer import NflJointPricer
from combo_mm.nfl.params_io import matchup_covariance
from combo_mm.nfl.synthetic_backtest import COMBOS, _legs_for, _market
from combo_mm.nfl.tuning import load_selection
from combo_mm.normalize import normalize
from combo_mm.paper_backtest import BacktestResult, compute_metrics
from combo_mm.pricing import LegMarkInput, price_combo
from combo_mm.reference import ReferenceCache
from combo_mm.store import EventStore
from combo_mm.stream import SimulatedTransport

__all__ = [
    "BACKTEST_SEASON",
    "BACKTEST_WEEK",
    "WeekBacktest",
    "run_week_backtest",
    "run_from_pull",
]

BACKTEST_SEASON = 2026
BACKTEST_WEEK = 1

BOOK_HALF_WIDTH = 0.005        # one-cent-wide leg books
BOOK_SIZE = 5000.0
RFQ_WINDOW = timedelta(hours=3)
GAME_DURATION = timedelta(hours=3, minutes=30)
QTY_CHOICES = ("10", "25", "50", "100", "250", "500")
BUY_SHARE = 0.75               # share of requesters buying the combo
SOURCE = "backtest"
COMPETITOR = "naive-maker"
_ET_OFFSET = timedelta(hours=4)  # nflverse gametime is US/Eastern (EDT in Sep-Oct)


@dataclass
class WeekBacktest:
    """One run: the SQLite store backing the dashboard + per-RFQ trade rows."""

    db_path: str
    result: BacktestResult
    trades: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _unit(*parts: Any) -> float:
    """Deterministic uniform [0, 1) from the parts."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def _line(x: float) -> str:
    return f"{x:g}"


def _leg_labels(game: Game, fav_is_home: bool) -> Dict[str, str]:
    fav, dog = (game.home, game.away) if fav_is_home else (game.away, game.home)
    s = abs(game.spread_line)
    return {
        "fav_ml": f"{fav} ML",
        "dog_ml": f"{dog} ML",
        "fav_cover": f"{fav} -{_line(s)}",
        "dog_cover": f"{dog} +{_line(s)}",
        "over": f"Over {_line(game.total_line)}",
        "under": f"Under {_line(game.total_line)}",
    }


def _symbol_prefix(game: Game) -> str:
    return f"NFL-{game.season % 100:02d}W{game.week:02d}-{game.away}@{game.home}"


def _leg_symbol(game: Game, label: str) -> str:
    return f"{_symbol_prefix(game)}-{label.replace(' ', '')}"


def kickoffs_from_csv(csv_path: Path | str, season: int, week: int) -> Dict[str, datetime]:
    """game_id -> kickoff (UTC) from the raw nflverse ``gameday`` + ``gametime``."""
    out: Dict[str, datetime] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("season") != str(season) or row.get("week") != str(week):
                continue
            if not row.get("gametime"):
                continue
            local = datetime.strptime(f"{row['gameday']} {row['gametime']}", "%Y-%m-%d %H:%M")
            out[row["game_id"]] = (local + _ET_OFFSET).replace(tzinfo=timezone.utc)
    return out


def _default_kickoff(game: Game) -> datetime:
    return datetime.strptime(game.gameday, "%Y-%m-%d").replace(hour=17, tzinfo=timezone.utc)


def _rfq_payload(rfq_id: str, at: str, symbol: str, creator: str,
                 legs: List[Dict[str, Any]], qty: str) -> Dict[str, Any]:
    return {
        "id": rfq_id, "symbol": symbol, "rfqCreatorUserId": creator,
        "createdTime": at, "updatedTime": at, "restRemainder": False,
        "status": "OPEN", "comboLegs": legs, "qtyDecimal": qty,
    }


def _quote_payload(quote_id: str, rfq_id: str, at: str, symbol: str, creator: str,
                   buy: float, sell: float, buy_qty: str, sell_qty: str,
                   status: str, **extra: Any) -> Dict[str, Any]:
    payload = {
        "id": quote_id, "rfqId": rfq_id, "creatorRfqUserId": SELF_USER_ID,
        "symbol": symbol, "status": status, "createdTime": at, "updatedTime": at,
        "buyPrice": f"{buy:.3f}", "sellPrice": f"{sell:.3f}",
        "restRemainder": False, "postOnly": True, "rfqCreatorUserId": creator,
        "buyQtyDecimal": buy_qty, "sellQtyDecimal": sell_qty,
    }
    payload.update(extra)
    return payload


class _Replay:
    """Applies wire-shaped events to the store and runs the engine."""

    def __init__(self, store: EventStore, engine: ShadowQuotingEngine, books: LegBookCache) -> None:
        self.store = store
        self.engine = engine
        self.books = books
        self._seq = 0

    def event(self, event_id: str, event_type: str, rfq_id: str, at: datetime,
              payload: Dict[str, Any], quote_id: Optional[str] = None,
              symbol: Optional[str] = None, quote: bool = False):
        raw: Dict[str, Any] = {"event_id": event_id, "event_type": event_type,
                               "rfq_id": rfq_id, "exchange_ts": _ts(at), "payload": payload}
        if quote_id:
            raw["quote_id"] = quote_id
        if symbol:
            raw["symbol"] = symbol
        event = normalize(raw, now=at)
        applied = self.store.apply(event, source=SOURCE)
        if applied and quote:
            return self.engine.maybe_quote(event)
        return None

    def book(self, symbol: str, bid: float, ask: float, at: datetime) -> None:
        self._seq += 1
        ts = _ts(at)
        self.books.update(symbol=symbol, bid=bid, ask=ask, bid_size=BOOK_SIZE,
                          ask_size=BOOK_SIZE, updated_at=ts, seq=self._seq)
        self.store.ingest_book(symbol, bid, ask, BOOK_SIZE, BOOK_SIZE, self._seq, ts)


def run_week_backtest(games: Sequence[Game], *, season: int = BACKTEST_SEASON,
                      week: int = BACKTEST_WEEK,
                      estimator: Optional[EstimatorConfig] = None,
                      config: Optional[PipelineConfig] = None,
                      kickoffs: Optional[Dict[str, datetime]] = None,
                      db_path: str = ":memory:") -> WeekBacktest:
    """Replay ``(season, week)``; see the module docstring for the method."""
    estimator = estimator or EstimatorConfig()
    config = config or PipelineConfig(paper_mode=True)
    kickoffs = kickoffs or {}
    targets = sorted(
        (g for g in games if g.season == season and g.week == week
         and g.played and g.has_lines and g.spread_line != 0),
        key=lambda g: (kickoffs.get(g.game_id) or _default_kickoff(g), g.game_id))
    if not targets:
        raise ValueError(f"no played games with closing lines for {season} week {week}")

    params = estimate_params(ResidualTable.build(games), season, week, estimator)
    params_version = f"nfl_{season}_w{week:02d}:{estimator.variance_model}"

    store = EventStore(db_path)
    books = LegBookCache(staleness_ms=config.staleness_ms)
    pricer = NflJointPricer()
    combos_meta: List[Dict[str, Any]] = []
    reference = ReferenceCache(SimulatedTransport([], SELF_USER_ID, combos_meta),
                               ttl_s=config.reference_ttl_s)
    engine = ShadowQuotingEngine(store, books, reference, config, pricer=pricer,
                                 params_version=params_version)
    replay = _Replay(store, engine, books)
    trades: List[Dict[str, Any]] = []
    n_pushed = 0

    for game in targets:
        mkt = _market(game)
        legs = _legs_for(game, mkt)
        labels = _leg_labels(game, mkt.fav_is_home)

        def cov_fn(mh: float, ma: float, game=game):
            return matchup_covariance(params, game.home, game.away, mh, ma)

        cal = calibrate_means(game.spread_line, mkt.p_home_cover, game.total_line, mkt.p_over, cov_fn)
        model = GameModel((cal.mu_home, cal.mu_away), cal.cov)
        symbols = {key: _leg_symbol(game, label) for key, label in labels.items()}
        pricer.register_game(game.game_id, model, {symbols[k]: legs[k][0] for k in labels})

        kickoff = kickoffs.get(game.game_id) or _default_kickoff(game)
        combo_names = [n for n, (keys, _, _) in COMBOS.items()
                       if all(legs[k][1] is not None for k in keys)]
        for i, name in enumerate(combo_names):
            keys, family, nested = COMBOS[name]
            rfq_id = f"{game.game_id}:{name}"
            combo_symbol = f"{_symbol_prefix(game)}:" + "+".join(labels[k].replace(" ", "") for k in keys)
            leg_rows = [{"symbol": symbols[k], "side": "YES"} for k in keys]
            combos_meta.append({"symbol": combo_symbol, "tick_size": config.tick_size,
                                "price_min": config.price_min, "price_max": config.price_max,
                                "min_qty": config.min_qty, "legs": leg_rows})
            # Arrivals spread across the pre-game window, deterministic jitter.
            offset = RFQ_WINDOW * ((i + _unit(rfq_id, "t")) / len(combo_names))
            at = kickoff - RFQ_WINDOW + offset
            qty = QTY_CHOICES[int(_unit(rfq_id, "qty") * len(QTY_CHOICES))]
            requester_side = "BUY" if _unit(rfq_id, "side") < BUY_SHARE else "SELL"
            creator = f"taker-{int(_unit(rfq_id, 'who') * 400):03d}"

            for k in keys:
                p = legs[k][1]
                bid = round(max(config.price_min, p - BOOK_HALF_WIDTH), 3)
                ask = round(min(config.price_max, p + BOOK_HALF_WIDTH), 3)
                replay.book(symbols[k], bid, ask, at - timedelta(milliseconds=500))

            draft = replay.event(f"{rfq_id}:created", "rfq_created", rfq_id, at,
                                 _rfq_payload(rfq_id, _ts(at), combo_symbol, creator, leg_rows, qty),
                                 symbol=combo_symbol, quote=True)

            # Competitor: V1 independent-leg maker on the same books and knobs.
            snaps = books.get([r["symbol"] for r in leg_rows], now_ms=int(at.timestamp() * 1000))
            comp_legs = [LegMarkInput(symbol=sn.symbol, side="YES", bid=sn.bid, ask=sn.ask,
                                      bid_size=sn.bid_size, ask_size=sn.ask_size,
                                      stale=sn.stale or sn.missing)
                         for sn in (snaps[r["symbol"]] for r in leg_rows)]
            comp = price_combo(
                comp_legs, rfq_id=rfq_id, qty_decimal=qty, model_version=COMPETITOR,
                leg_width_multiplier=config.leg_width_multiplier,
                max_half_spread_bps=config.max_half_spread_bps,
                max_leg_spread_bps=config.max_leg_spread_bps,
                tick_size=config.tick_size, price_min=config.price_min,
                price_max=config.price_max, min_qty=config.min_qty)

            naive_fair = math.prod(legs[k][1] for k in keys)
            row: Dict[str, Any] = {
                "rfq_id": rfq_id, "game_id": game.game_id,
                "game": f"{game.away} @ {game.home}", "combo": name,
                "combo_label": " + ".join(labels[k] for k in keys),
                "family": family, "n_legs": len(keys), "nested": nested,
                "requested_at": _ts(at), "requester_side": requester_side, "qty": float(qty),
                "naive_fair": naive_fair, "model_fair": draft.fair if draft else None,
                "our_buy": draft.buy_price if draft else None,
                "our_sell": draft.sell_price if draft else None,
                "comp_buy": comp.buy_price if comp.quoted else None,
                "comp_sell": comp.sell_price if comp.quoted else None,
                "won": False, "fill_side": None, "fill_price": None, "fill_qty": 0.0,
                "outcome": None, "pnl": None, "expected_pnl": None,
            }

            quote_id = f"{SELF_USER_ID}:{rfq_id}"
            if draft is not None:
                q_at = at + timedelta(seconds=2)
                replay.event(f"{rfq_id}:quote", "quote_created", rfq_id, q_at,
                             _quote_payload(quote_id, rfq_id, _ts(q_at), combo_symbol, creator,
                                            draft.buy_price, draft.sell_price,
                                            draft.buy_qty, draft.sell_qty, "ACTIVE"),
                             quote_id=quote_id)
                if requester_side == "BUY":
                    ours, theirs, our_qty = draft.buy_price, comp.buy_price, draft.buy_qty
                    won = ours > 0 and (not comp.quoted or theirs <= 0 or ours < theirs)
                else:
                    ours, theirs, our_qty = draft.sell_price, comp.sell_price, draft.sell_qty
                    won = ours > 0 and (not comp.quoted or ours > theirs)
                won = won and float(our_qty) > 0
            else:
                won = False

            if won:
                fill_side = "SELL" if requester_side == "BUY" else "BUY"
                terms = dict(buy=draft.buy_price, sell=draft.sell_price,
                             buy_qty=draft.buy_qty, sell_qty=draft.sell_qty)
                for dt_s, etype, status, extra in (
                    (5, "quote_accepted", "ACCEPTED", {"acceptedSide": requester_side}),
                    (6, "quote_confirmed", "CONFIRMED", {}),
                    (7, "quote_executed", "EXECUTED", {"orderId": f"ord-{rfq_id}"}),
                ):
                    e_at = at + timedelta(seconds=dt_s)
                    replay.event(f"{rfq_id}:{etype}", etype, rfq_id, e_at,
                                 _quote_payload(quote_id, rfq_id, _ts(e_at), combo_symbol, creator,
                                                status=status, **terms, **extra),
                                 quote_id=quote_id)
                exec_at = at + timedelta(seconds=7)
                store.record_fill(fill_id=f"bt-fill-{rfq_id}", rfq_id=rfq_id, quote_id=quote_id,
                                  symbol=combo_symbol, side=fill_side, price=ours,
                                  qty=float(our_qty), executed_time=_ts(exec_at), source=SOURCE)
                sign = 1.0 if fill_side == "BUY" else -1.0
                row.update(won=True, fill_side=fill_side, fill_price=ours, fill_qty=float(our_qty),
                           expected_pnl=sign * (draft.fair - ours) * float(our_qty))
            else:
                c_at = at + timedelta(seconds=5)
                replay.event(f"{rfq_id}:closed", "rfq_closed", rfq_id, c_at,
                             {"id": rfq_id, "status": "CLOSED", "updatedTime": _ts(c_at)})

            # Settlement after the final whistle; a pushed leg stays unsettled (void).
            s_at = kickoff + GAME_DURATION
            results = {k: settle_leg(legs[k][0], game.home_score, game.away_score) for k in keys}
            settled_legs = []
            for r, k in zip(leg_rows, keys):
                leg = dict(r)
                if results[k] != PUSH:
                    leg["settlementPrice"] = "1" if results[k] == WIN else "0"
                settled_legs.append(leg)
            replay.event(f"{rfq_id}:settled", "rfq_updated", rfq_id, s_at,
                         {"id": rfq_id, "updatedTime": _ts(s_at), "comboLegs": settled_legs},
                         symbol=combo_symbol)
            if PUSH in results.values():
                n_pushed += 1
                row["outcome"] = "void"
                if row["won"]:
                    row["pnl"] = 0.0
            else:
                won_combo = all(v == WIN for v in results.values())
                row["outcome"] = "win" if won_combo else "loss"
                if row["won"]:
                    value = 1.0 if won_combo else 0.0
                    sign = 1.0 if row["fill_side"] == "BUY" else -1.0
                    row["pnl"] = sign * (value - row["fill_price"]) * row["fill_qty"]
            trades.append(row)

    result = compute_metrics(store)
    # Expected P&L here is model edge on what actually traded, not half-spread on every quote.
    result.expected_pnl = sum(t["expected_pnl"] or 0.0 for t in trades)
    lg = params["league"]
    meta = {
        "season": season, "week": week, "n_games": len(targets),
        "n_rfqs": len(trades), "n_pushed_combos": n_pushed,
        "params_version": params_version, "estimator": estimator.to_dict(),
        "league_params": {k: lg.get(k) for k in ("sigma_at_mean_points", "rho", "mean_points", "n_games")},
        "buy_share": BUY_SHARE, "book_half_width": BOOK_HALF_WIDTH,
    }
    return WeekBacktest(db_path=db_path, result=result, trades=trades, meta=meta)


def run_from_pull(*, raw_root: Path | str, estimator_path: Path | str,
                  db_path: str, season: int = BACKTEST_SEASON,
                  week: int = BACKTEST_WEEK) -> WeekBacktest:
    """Dashboard entry point: latest cached nflverse pull + frozen estimator."""
    pull: Optional[RawPull] = latest_pull(raw_root)
    if pull is None:
        raise FileNotFoundError(
            f"no cached nflverse pull under {raw_root}; run `python scripts/nfl_tune.py --pull` "
            "or `python scripts/refresh_params.py --pull` first")
    selection = load_selection(estimator_path)
    estimator = selection[0] if selection else EstimatorConfig()
    out = run_week_backtest(load_games(pull), season=season, week=week, estimator=estimator,
                            kickoffs=kickoffs_from_csv(pull.csv_path, season, week),
                            db_path=db_path)
    out.meta["data_vintage"] = {"pull_date": pull.pull_date, "sha256": pull.sha256}
    out.meta["estimator_tuned"] = selection is not None
    return out

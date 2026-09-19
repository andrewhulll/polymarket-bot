"""Headless paper quoting engine and capture of live Polymarket RFQs.

The websocket adapter wakes this process on each frame. It screens RFQs and
prices eligible requests on a worker thread. Only quoted RFQs are stored. The dashboard
reads the resulting SQLite database. No order is submitted. Uses the
receive-only quoter-gateway adapter
(``combo_mm.intl_gateway.InternationalQuoterGatewayAdapter``). Run it
continuously (e.g. in a background process/tmux/systemd unit) to build a
paper quote dataset over time. There is no way to backfill RFQs from before
this script was running -- Polymarket has no historical RFQ endpoint (see
README "Historical RFQ data"); the gateway stream delivers only new events.

Writes, under ``--data-dir`` (default ``data/live``):

- ``rfq_raw.jsonl``     -- raw request and trade frames for RFQs we actually
  paper quote. The trade's accepted price/size also survive in ``live_trades``.
- ``rfq_capture.db``    -- quoted RFQs, their screens, paper drafts and trades,
  plus one live health row. Rejected and declined RFQs remain in memory only.
- ``combo_markets.json.gz`` -- compressed leg catalog cache (same cache the dashboard
  uses, so a restart resolves legs immediately either way).

Usage::

    export POLYMARKET_API_KEY=... POLYMARKET_SECRET=... \\
           POLYMARKET_PASSPHRASE=... POLYMARKET_ADDRESS=...
    python3 scripts/capture_live_rfqs.py --data-dir data/live

Stop with Ctrl+C (or SIGTERM); the catalog, JSONL file and DB all flush and
close cleanly so a restart resumes without losing anything already written.
"""
from __future__ import annotations

import argparse
import math
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.combo_markets import ComboMarketCatalog
from combo_mm.intl_gateway import GatewayCredentials, InternationalQuoterGatewayAdapter
from combo_mm.normalize import NormalizeError, normalize
from combo_mm.rfq_screen import screen_legs, screen_checks
from combo_mm.store import EventStore
from combo_mm.config import PipelineConfig
from combo_mm.leg_books import LiveLegBooks
from combo_mm.live_quoter import LiveQuoter
from combo_mm.nfl.live_pricer import NflLivePricer, NflLivePricerConfig, LiveRfq
from combo_mm.nfl.params_provider import ParamsProvider
from combo_mm.nfl.tuning import SELECTION_PATH, load_selection
from combo_mm.quote_selections import QuoteSelectionStore
from combo_mm.capture_process import CaptureLock
from combo_mm.inventory import InventoryProvider
from combo_mm.risk_config import RiskConfig
from combo_mm.risk_policy import InventoryRiskCheck
from combo_mm.transient_rfqs import TransientRfqs, TransientRfqServer

log = logging.getLogger("capture_live_rfqs")

def _live_pricer_config(repo: Path) -> NflLivePricerConfig:
    """``corr_scale`` from the frozen train-only tuning selection, default 1.0.

    ``corr_scale`` shrinks the fitted margin/total dependence at PRICING
    time (see ``combo_mm.nfl.tuning.tune_corr_scale``); it is not part of
    the weekly params file, so it is read here rather than from
    ``ParamsProvider``. Falls back to the untuned default (1.0, the raw fit,
    unshrunk) when no tuning selection exists yet.
    """
    selection = load_selection(repo / SELECTION_PATH)
    corr_scale = selection[1].get("pricer_corr_scale", 1.0) if selection is not None else 1.0
    return NflLivePricerConfig(corr_scale=corr_scale)


def _combo_side(raw: Dict[str, Any]) -> Optional[str]:
    """Gateway legs inherit the combo side, so the first leg's side is the combo's."""
    legs = raw.get("comboLegs") or []
    side = legs[0].get("side") if legs and isinstance(legs[0], dict) else None
    return str(side) if side else None


class RfqCapture:
    """Persist live events and, when enabled, make paper pricing decisions."""

    def __init__(self, data_dir: Path, *, price_live: bool = False,
                 quoter_workers: int = 1) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.raw_path = data_dir / "rfq_raw.jsonl"
        self._raw_file = self.raw_path.open("a", encoding="utf-8")
        self.store = EventStore(str(data_dir / "rfq_capture.db"), synchronous="NORMAL")
        self.selections = QuoteSelectionStore(data_dir / "rfq_capture.db") if price_live else None
        self.catalog = ComboMarketCatalog(cache_path=data_dir / "combo_markets.json.gz")
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.errors = 0
        self.quoter = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._quoted: set[str] = set()
        self._lock = threading.RLock()
        self.transient_rfqs = TransientRfqs()
        self._risk_config = PipelineConfig(risk=RiskConfig(policy="inventory"))
        self._risk = InventoryRiskCheck(self._risk_config.risk)
        self._inventory = InventoryProvider(self.store,
                                            capital=self._risk_config.initial_capital,
                                            game_resolver=self._resolve_game)
        # Only quoted RFQs are durable. Reload their ids so trade broadcasts
        # after a restart can still be joined to the saved quotes.
        with self.store._lock:
            self._quoted.update(row[0] for row in self.store._conn.execute(
                "SELECT rfq_id FROM quotes WHERE status = 'shadow'"))
        if price_live:
            repo = Path(__file__).resolve().parents[1]
            params = ParamsProvider(repo / "params")
            try:
                handle = params.current()
            except Exception as exc:
                handle = None
                log.warning("weekly parameters unreadable: %s", type(exc).__name__)
            if handle is None:
                log.warning("weekly parameters unavailable; quotable RFQs will be marked PRICING_UNAVAILABLE")
            else:
                pricer = NflLivePricer(self.catalog, LiveLegBooks(), params,
                                       config=PipelineConfig(paper_mode=True),
                                       model_config=_live_pricer_config(repo))
                self.quoter = LiveQuoter(pricer, self.selections, workers=quoter_workers,
                                         on_decision=self._record_decision,
                                         store_declines=False)
        self.rfqs_seen = 0
        self.trades_seen = 0
        self.nfl_rfqs_seen = 0

    def start(self) -> None:
        self.catalog.start()

    def stop(self) -> None:
        if self.quoter is not None:
            self.quoter.stop()
        self.catalog.stop()
        self._raw_file.close()
        if self.selections is not None:
            self.selections.close()
        self.store.close()

    def _record_decision(self, rfq: LiveRfq, quote: Any,
                         started: datetime, decided: datetime) -> None:
        with self._lock:
            pending = self._pending.pop(rfq.rfq_id, None)
            if pending is None:
                return
            if not quote.quoted:
                self.transient_rfqs.decision(rfq.rfq_id, quote)
                return
            if not self._check_quote_risk(rfq, quote, decided):
                self.transient_rfqs.decision(rfq.rfq_id, quote)
                return
            self.store.apply(pending["event"], source="live_capture",
                             record_inventory=False)
            screen = pending["screen"]
            self.store.upsert_rfq_screen(rfq.rfq_id, **screen)
            self._write_raw_frame(pending["now"], pending["raw"])
            self.store.record_shadow_decision(
                rfq_id=rfq.rfq_id, decision=quote.reason_code,
                reason=quote.reason_detail or "",
                fair_price=quote.fair, buy_price=quote.ask,
                sell_price=quote.bid, buy_qty=quote.ask_qty,
                sell_qty=quote.bid_qty, ts=decided.isoformat())
            self.store.record_shadow_draft(
                quote_id=f"paper:{rfq.rfq_id}:auto", rfq_id=rfq.rfq_id,
                fair=quote.fair, buy_price=quote.ask or 0,
                sell_price=quote.bid or 0,
                buy_qty=quote.ask_qty or "0", sell_qty=quote.bid_qty or "0",
                model_version=quote.model_version,
                params_version=quote.params_version,
                input_snapshot_json=json.dumps(quote.to_dict(), sort_keys=True),
                decided_by="headless-paper", decided_at=decided.isoformat())
            self._quoted.add(rfq.rfq_id)
            self.transient_rfqs.remove(rfq.rfq_id)
            if rfq.received_at:
                components = quote.components or {}
                self.store.record_live_latency(
                    rfq_id=rfq.rfq_id, posted_at=rfq.received_at,
                    started_at=started.isoformat(),
                    decided_at=decided.isoformat(), quoted=True,
                    fetch_ms=components.get("book_fetch_ms"),
                    solve_ms=components.get("solve_ms"),
                    local_received_at=rfq.local_received_at)
            if pending.get("trade"):
                trade, trade_event, trade_now = pending["trade"]
                self._save_trade(trade, trade_event, trade_now)

    def _resolve_game(self, position_id: str) -> Optional[str]:
        market = self.catalog.resolve((position_id,))[0]
        return market.game if market is not None else None

    def _check_quote_risk(self, rfq: LiveRfq, quote: Any,
                          decided: datetime) -> bool:
        """Reserve capacity before a paper quote becomes visible to other workers."""
        def decline(code: str, message: str, game_id: str = "") -> bool:
            quote.status, quote.reason_code, quote.reason_detail = "DECLINED", code, message
            quote.bid = quote.ask = quote.response_price = None
            quote.bid_qty = quote.ask_qty = None
            return False

        games = {self._resolve_game(position_id) for position_id in rfq.leg_position_ids}
        if None in games or len(games) != 1:
            return decline("RISK_GAME_UNRESOLVED", "a single game must resolve for inventory limits")
        game = next(iter(games))
        bid, ask = float(quote.bid or 0), float(quote.ask or 0)
        buy_qty = max(0, int(float(quote.ask_qty or 0)))
        sell_qty = max(0, int(float(quote.bid_qty or 0)))
        notional = max(buy_qty * ask, sell_qty * bid)
        if notional <= 0:
            return decline("RISK_SIZE_REDUCED", "quote has no positive notional", game)
        if notional > self._risk_config.max_per_rfq_notional:
            factor = self._risk_config.max_per_rfq_notional / notional
            buy_qty = math.floor(buy_qty * factor)
            sell_qty = math.floor(sell_qty * factor)
        if not buy_qty and not sell_qty:
            return decline("RISK_SIZE_REDUCED", "RFQ size falls below one share at the $1,000 limit", game)
        notional = max(buy_qty * ask, sell_qty * bid)
        inventory = self._inventory(decided.isoformat())
        used = inventory.notional_by_game.get(game, 0.0)
        if used + notional > self._risk_config.max_per_game_notional + 1e-9:
            return decline("RISK_GAME_EXPOSURE",
                           f"game quoted notional ${used + notional:.2f} exceeds "
                           f"${self._risk_config.max_per_game_notional:.2f}", game)
        draft = SimpleNamespace(fair_value=quote.fair,
                                extra={"buy_qty": str(buy_qty), "sell_qty": str(sell_qty),
                                       "buy_price": ask, "sell_price": bid,
                                       "markets": rfq.leg_position_ids})
        verdict = self._risk.check(draft, notional, inventory, game)
        if not verdict.ok:
            message = (f"game maximum loss ${inventory.exposures.get(game, 0):.2f} / "
                       f"${self._risk_config.risk.max_game_loss:.2f} leaves no quotable size"
                       if verdict.reason == "RISK_GAME_EXPOSURE" else
                       "no quotable size fits inventory limits")
            return decline(verdict.reason, message, game)
        buy_qty, sell_qty = int(verdict.adjusted_buy_qty), int(verdict.adjusted_sell_qty)
        quote.ask = verdict.adjusted_buy_price if buy_qty else None
        quote.bid = verdict.adjusted_sell_price if sell_qty else None
        final_notional = max(buy_qty * (quote.ask or 0),
                             sell_qty * (quote.bid or 0))
        if final_notional > self._risk_config.max_per_rfq_notional + 1e-9:
            factor = self._risk_config.max_per_rfq_notional / final_notional
            buy_qty, sell_qty = math.floor(buy_qty * factor), math.floor(sell_qty * factor)
            if not buy_qty and not sell_qty:
                return decline("RISK_SIZE_REDUCED", "adjusted RFQ size falls below one share", game)
            if not buy_qty:
                quote.ask = None
            if not sell_qty:
                quote.bid = None
            final_notional = max(buy_qty * (quote.ask or 0),
                                 sell_qty * (quote.bid or 0))
        if used + final_notional > self._risk_config.max_per_game_notional + 1e-9:
            return decline("RISK_GAME_EXPOSURE", "adjusted quote exceeds $5,000 game limit", game)
        quote.ask_qty, quote.bid_qty = str(buy_qty), str(sell_qty)
        quote.response_price = quote.ask if rfq.direction == "BUY" else quote.bid
        if quote.response_price is None:
            return decline("RISK_SIZE_REDUCED", "requested RFQ side has no capacity", game)
        quote.components["risk"] = {
            "game": game, "action": verdict.action, "notional_before": notional,
            "game_notional_before": used, "game_loss_before": inventory.exposures.get(game, 0),
            "game_loss_limit": self._risk_config.risk.max_game_loss,
        }
        return True

    def _save_trade(self, raw: Dict[str, Any], event: Any, now: datetime) -> None:
        if self.store.apply(event, source="live_capture", record_inventory=False):
            self.store.record_live_trade(event.rfq_id, raw.get("price"), raw.get("size"),
                                         raw.get("executed_at"))
            self._write_raw_frame(now, raw)

    def heartbeat(self, adapter: InternationalQuoterGatewayAdapter) -> None:
        stats = adapter.stats()
        self.store.update_live_health(
            started_at=self.started_at,
            messages_processed=self.rfqs_seen + self.trades_seen,
            errors=self.errors + (self.quoter.errors if self.quoter else 0),
            gateway_connected=adapter.connected,
            buffer_drops=stats["buffer_drops"])

    def _write_raw_frame(self, now: datetime, raw: Dict[str, Any]) -> None:
        """Append one raw gateway frame to the JSONL log."""
        self._raw_file.write(
            json.dumps({"received_at": now.isoformat(), "raw": raw}) + "\n")
        self._raw_file.flush()

    def handle(self, item: Dict[str, Any], now: datetime) -> None:
        if item.get("kind") != "event":
            return
        raw = dict(item["raw"])
        try:
            event = normalize(raw, now=now)
        except NormalizeError:
            self.errors += 1
            log.warning("dropping malformed frame", exc_info=True)
            return
        if event.event_type == "rfq_created" and event.rfq_id:
            self.rfqs_seen += 1
            self._screen(raw, event, now)
        elif event.event_type == "rfq_closed" and "price" in raw:
            # Only gateway RFQ_TRADE broadcasts carry price/size (see
            # combo_mm.intl_gateway.map_rfq_trade); a plain rfq_closed
            # (deleted, no quote accepted) does not.
            self.trades_seen += 1
            with self._lock:
                if event.rfq_id in self._quoted:
                    self._save_trade(raw, event, now)
                elif event.rfq_id in self._pending:
                    self._pending[event.rfq_id]["trade"] = (raw, event, now)
                else:
                    self.transient_rfqs.change(
                        event.rfq_id, status="CLOSED",
                        trade_price=raw.get("price"),
                        trade_executed_at=raw.get("executed_at"))

    def _screen(self, raw: Dict[str, Any], event: Any, now: datetime) -> None:
        rfq_id = event.rfq_id
        legs = [str(leg.get("symbol")) for leg in raw.get("comboLegs") or []
                if isinstance(leg, dict) and leg.get("symbol") is not None]
        result = screen_legs(self.catalog.resolve(legs))
        qty = raw.get("qtyDecimal")
        try:
            qty = float(qty) if qty is not None else None
        except (ValueError, TypeError):
            qty = None
        checks = screen_checks(result, qty_decimal=qty,
                               min_qty=PipelineConfig().min_qty)
        eligible = result.quotable and all(checks.values())
        if result.n_nfl_legs:
            self.nfl_rfqs_seen += 1
        if rfq_id not in self._quoted:
            row = {
                "rfq_id": rfq_id, "symbol": event.symbol,
                "created_time": raw.get("createdTime") or now.isoformat(),
                "updated_time": raw.get("updatedTime") or now.isoformat(),
                "qty_decimal": raw.get("qtyDecimal"),
                "cash_order_qty": raw.get("cashOrderQty"),
                "status": raw.get("status") or "RFQ_STATUS_OPEN",
                "screen": result.screen, "quotable": False,
                "n_legs": result.n_legs, "n_resolved": result.n_resolved,
                "n_nfl_legs": result.n_nfl_legs, "filters": checks,
                "side": _combo_side(raw), "direction": raw.get("direction"),
                "submission_deadline": raw.get("submission_deadline"),
                "trade_price": None, "trade_executed_at": None,
            }
            detail = {
                "rfq": dict(row),
                "screen": {"screen": result.screen, "n_legs": result.n_legs,
                           "n_resolved": result.n_resolved,
                           "n_nfl_legs": result.n_nfl_legs,
                           "side": row["side"], "direction": row["direction"],
                           "submission_deadline": row["submission_deadline"],
                           "checks": checks},
                "legs": [{"symbol": leg.get("symbol"), "side": leg.get("side")}
                         for leg in raw.get("comboLegs") or [] if isinstance(leg, dict)],
                "events": [], "pricing": None, "trade": None,
            }
            self.transient_rfqs.put(row, detail)
        if eligible and self.quoter is not None:
            with self._lock:
                if rfq_id in self._quoted or rfq_id in self._pending:
                    return
                self._pending[rfq_id] = {
                    "raw": raw, "event": event, "now": now,
                    "screen": dict(
                        n_legs=result.n_legs, n_resolved=result.n_resolved,
                        n_nfl_legs=result.n_nfl_legs, screen=result.screen,
                        rank=result.rank, catalog_version=self.catalog.version,
                        direction=raw.get("direction") or None, side=_combo_side(raw),
                        condition_id=raw.get("condition_id") or None,
                        submission_deadline=raw.get("submission_deadline") or None,
                        checks_json=json.dumps(checks))}
            try:
                rfq = LiveRfq(
                    rfq_id=rfq_id, leg_position_ids=tuple(legs),
                    side=_combo_side(raw) or "YES",
                    direction=str(raw.get("direction") or "BUY"),
                    qty_decimal=(str(raw["qtyDecimal"]) if raw.get("qtyDecimal") is not None else None),
                    cash_order_qty=(str(raw["cashOrderQty"]) if raw.get("cashOrderQty") is not None else None),
                    submission_deadline_ms=int(raw["submission_deadline"])
                    if raw.get("submission_deadline") else None,
                    received_at=raw.get("createdTime") or raw.get("exchange_ts"),
                    local_received_at=now.isoformat())
                if not self.quoter.submit(rfq):
                    with self._lock:
                        self._pending.pop(rfq_id, None)
                    self.transient_rfqs.change(rfq_id, status="DECLINED",
                                               reason_code="QUOTER_QUEUE_FULL")
            except Exception:
                with self._lock:
                    self._pending.pop(rfq_id, None)
                self.transient_rfqs.change(rfq_id, status="DECLINED",
                                           reason_code="PRICER_ERROR")
                raise

    def rescreen_unresolved(self) -> None:
        """Compatibility no-op: unquoted RFQs are intentionally not stored."""


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data/live",
                        help="output directory (default: data/live)")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="maximum seconds between health and catalog checks; frames wake immediately")
    parser.add_argument("--log-every", type=float, default=60.0,
                        help="progress log interval, seconds")
    parser.add_argument("--duration", type=float, default=None,
                        help="stop after N seconds (default: run until interrupted)")
    parser.add_argument("--quoter-workers", type=int, default=4,
                        help="pricing worker threads draining the RFQ queue (default: 4)")
    parser.add_argument("--screen-port", type=int, default=8765,
                        help="local session-only screener API port (default: 8765)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    try:
        creds = GatewayCredentials.from_env()
    except Exception as exc:
        log.error("%s", exc)
        return 1
    try:
        adapter = InternationalQuoterGatewayAdapter(creds)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    reader_lock = CaptureLock(Path(args.data_dir) / "rfq_capture.lock")
    if not reader_lock.acquire():
        log.info("RFQ capture is already running for %s", args.data_dir)
        return 0

    capture = None
    screen_server = None
    try:
        capture = RfqCapture(Path(args.data_dir), price_live=True,
                             quoter_workers=args.quoter_workers)
        screen_server = TransientRfqServer(capture.transient_rfqs,
                                           Path(args.data_dir), args.screen_port)
        screen_server.start()
        capture.start()
        adapter.start()
    except BaseException:
        if screen_server is not None:
            screen_server.stop()
        if capture is not None:
            capture.stop()
        reader_lock.release()
        raise

    stop = {"flag": False}

    def _handle_signal(signum: int, frame: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    start_time = time.monotonic()
    last_log = start_time
    log.info("capture started -> %s", args.data_dir)
    try:
        while not stop["flag"]:
            now = datetime.now(timezone.utc)
            for item in adapter.poll(now):
                capture.handle(item, now)
            capture.heartbeat(adapter)
            if time.monotonic() - last_log >= args.log_every:
                quoter_stats = capture.quoter.stats() if capture.quoter else {}
                log.info(
                    "rfqs=%d nfl=%d trades=%d catalog=%d gateway_connected=%s "
                    "quoter_submitted=%d quoter_priced=%d quoter_queued=%d "
                    "quoter_dropped=%d gateway_buffer_drops=%d "
                    "clob_fetches=%s gamma_fetches=%s book_last_error=%s",
                    capture.rfqs_seen, capture.nfl_rfqs_seen,
                    capture.trades_seen, len(capture.catalog), adapter.connected,
                    quoter_stats.get("submitted", 0), quoter_stats.get("priced", 0),
                    quoter_stats.get("queued", 0), quoter_stats.get("dropped", 0),
                    adapter.stats()["buffer_drops"],
                    quoter_stats.get("clob_fetches"), quoter_stats.get("gamma_fetches"),
                    quoter_stats.get("book_last_error"))
                last_log = time.monotonic()
            if args.duration is not None and time.monotonic() - start_time >= args.duration:
                break
            adapter.wait_for_items(args.poll_interval)
    finally:
        adapter.stop()
        screen_server.stop()
        capture.stop()
        reader_lock.release()
        log.info("capture stopped: rfqs=%d nfl=%d trades=%d",
                 capture.rfqs_seen, capture.nfl_rfqs_seen, capture.trades_seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Headless paper quoting engine and capture of live Polymarket RFQs.

The websocket adapter wakes this process on each frame. It stores RFQs,
screens them, and prices eligible requests on a worker thread. Streamlit only
reads the resulting SQLite database. No order is submitted. Uses the
receive-only quoter-gateway adapter
(``combo_mm.intl_gateway.InternationalQuoterGatewayAdapter``). Run it
continuously (e.g. in a background process/tmux/systemd unit) to build a
real RFQ dataset over time. There is no way to backfill RFQs from before
this script was running -- Polymarket has no historical RFQ endpoint (see
README "Historical RFQ data"); the gateway stream delivers only new events.

Writes, under ``--data-dir`` (default ``data/live``):

- ``rfq_raw.jsonl``     -- one JSON object per line, exactly what the gateway
  adapter emits, but only for RFQs that pass the screen (eligible to quote)
  and for trades on those RFQs. Everything else still lands in slim parsed
  form in ``rfq_capture.db`` below, so the tuning dataset (decisions,
  decline reasons, fair values) stays complete while the raw-frame log
  stops growing with every unrelated RFQ on the gateway. A trade's
  accepted price/size also survive in the ``live_trades`` table; the JSONL
  keeps any gateway-native trade extras ``normalize()`` does not recognize
  (see ``combo_mm/normalize.py``).
- ``rfq_capture.db``    -- the same events run through the existing
  normalize + ``EventStore`` + ``rfq_screen`` pipeline, giving queryable
  RFQ/leg/screen tables and NFL leg resolution via the combo catalog.
- ``combo_markets.json`` -- the leg catalog cache (same cache the dashboard
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
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
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
from combo_mm.nfl.live_pricer import NflLivePricer, LiveRfq
from combo_mm.nfl.params_provider import ParamsProvider
from combo_mm.quote_selections import QuoteSelectionStore
from combo_mm.capture_process import CaptureLock

log = logging.getLogger("capture_live_rfqs")

RESCREEN_BATCH = 5000


def _combo_side(raw: Dict[str, Any]) -> Optional[str]:
    """Gateway legs inherit the combo side, so the first leg's side is the combo's."""
    legs = raw.get("comboLegs") or []
    side = legs[0].get("side") if legs and isinstance(legs[0], dict) else None
    return str(side) if side else None


class RfqCapture:
    """Persist live events and, when enabled, make paper pricing decisions."""

    def __init__(self, data_dir: Path, *, price_live: bool = False) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.raw_path = data_dir / "rfq_raw.jsonl"
        self._raw_file = self.raw_path.open("a", encoding="utf-8")
        self.store = EventStore(str(data_dir / "rfq_capture.db"), synchronous="NORMAL")
        self.selections = QuoteSelectionStore(data_dir / "rfq_capture.db") if price_live else None
        self.catalog = ComboMarketCatalog(cache_path=data_dir / "combo_markets.json")
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.errors = 0
        self.quoter = None
        if price_live:
            params = ParamsProvider(Path(__file__).resolve().parents[1] / "params")
            try:
                handle = params.current()
            except Exception as exc:
                handle = None
                log.warning("weekly parameters unreadable: %s", type(exc).__name__)
            if handle is None:
                log.warning("weekly parameters unavailable; quotable RFQs will be marked PRICING_UNAVAILABLE")
            else:
                pricer = NflLivePricer(self.catalog, LiveLegBooks(), params,
                                       config=PipelineConfig(paper_mode=True))
                self.quoter = LiveQuoter(pricer, self.selections,
                                         on_decision=self._record_decision)
        self.rfqs_seen = 0
        self.trades_seen = 0
        self.nfl_rfqs_seen = 0
        self._rescreened_version = -1
        self._raw_eligible: set[str] = set()
        # RFQ ids whose raw gateway frames are worth keeping on disk. An id
        # lands here when its RFQ passes the screen; the raw JSONL then keeps
        # that RFQ's frame and any later trade frame for it. Everything else
        # is captured only in slim parsed form in the SQLite store.

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
        self.store.record_shadow_decision(
            rfq_id=rfq.rfq_id, decision=quote.reason_code,
            reason=quote.reason_detail or "",
            fair_price=quote.fair, buy_price=quote.ask,
            sell_price=quote.bid, buy_qty=quote.ask_qty,
            sell_qty=quote.bid_qty, ts=decided.isoformat())
        if quote.quoted:
            self.store.record_shadow_draft(
                quote_id=f"paper:{rfq.rfq_id}:auto", rfq_id=rfq.rfq_id,
                fair=quote.fair, buy_price=quote.ask or 0,
                sell_price=quote.bid or 0,
                buy_qty=quote.ask_qty or "0", sell_qty=quote.bid_qty or "0",
                model_version=quote.model_version,
                params_version=quote.params_version,
                input_snapshot_json=json.dumps({"fair_value": quote.fair,
                                                "naive": quote.naive,
                                                "components": quote.components}),
                decided_by="headless-paper", decided_at=decided.isoformat())
        if rfq.received_at:
            self.store.record_live_latency(
                rfq_id=rfq.rfq_id, posted_at=rfq.received_at,
                started_at=started.isoformat(),
                decided_at=datetime.now(timezone.utc).isoformat(),
                quoted=quote.quoted)

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
        applied = self.store.apply(event, source="live_capture")
        if not applied:
            return
        if event.event_type == "rfq_created" and event.rfq_id:
            self.rfqs_seen += 1
            self._screen(raw, event.rfq_id, now)
        elif event.event_type == "rfq_closed" and "price" in raw:
            # Only gateway RFQ_TRADE broadcasts carry price/size (see
            # combo_mm.intl_gateway.map_rfq_trade); a plain rfq_closed
            # (deleted, no quote accepted) does not.
            self.trades_seen += 1
            self.store.record_live_trade(event.rfq_id, raw.get("price"), raw.get("size"),
                                         raw.get("executed_at"))
            # Keep the raw trade frame only for RFQs we engaged with; the
            # price/size itself is already in the live_trades table.
            if event.rfq_id in self._raw_eligible:
                self._write_raw_frame(now, raw)

    def _screen(self, raw: Dict[str, Any], rfq_id: str, now: datetime) -> None:
        legs = [str(leg.get("symbol")) for leg in raw.get("comboLegs") or []
                if isinstance(leg, dict) and leg.get("symbol") is not None]
        if not legs:
            return
        result = screen_legs(self.catalog.resolve(legs))
        qty = raw.get("qtyDecimal")
        try:
            qty = float(qty) if qty is not None else None
        except (ValueError, TypeError):
            qty = None
        checks = screen_checks(result, qty_decimal=qty,
                               min_qty=PipelineConfig().min_qty)
        eligible = result.quotable and all(checks.values())
        if eligible:
            # This RFQ is worth quoting on: keep its raw gateway frame so a
            # later tuning/backtest pass has the full context, and remember
            # it so a later trade frame for it is kept too.
            self._raw_eligible.add(rfq_id)
            self._write_raw_frame(now, raw)
        if result.n_nfl_legs:
            self.nfl_rfqs_seen += 1
        self.store.upsert_rfq_screen(
            rfq_id, n_legs=result.n_legs, n_resolved=result.n_resolved,
            n_nfl_legs=result.n_nfl_legs,
            screen=result.screen if eligible or not result.quotable else "BELOW_MIN_SIZE",
            rank=result.rank if eligible or not result.quotable else 2,
            catalog_version=self.catalog.version,
            direction=raw.get("direction") or None, side=_combo_side(raw),
            condition_id=raw.get("condition_id") or None,
            submission_deadline=raw.get("submission_deadline") or None,
            checks_json=json.dumps(checks))
        if eligible and self.selections is not None:
            if self.quoter is None:
                self.selections.record_priced_quote({
                    "rfq_id": rfq_id, "priced_at": datetime.now(timezone.utc).isoformat(),
                    "status": "DECLINED", "reason_code": "PRICING_UNAVAILABLE"})
            else:
                rfq = LiveRfq(
                    rfq_id=rfq_id, leg_position_ids=tuple(legs),
                    side=_combo_side(raw) or "YES",
                    direction=str(raw.get("direction") or "BUY"),
                    qty_decimal=(str(raw["qtyDecimal"]) if raw.get("qtyDecimal") is not None else None),
                    cash_order_qty=(str(raw["cashOrderQty"]) if raw.get("cashOrderQty") is not None else None),
                    submission_deadline_ms=int(raw["submission_deadline"])
                    if raw.get("submission_deadline") else None,
                    received_at=raw.get("createdTime") or raw.get("exchange_ts"))
                if not self.quoter.submit(rfq):
                    self.selections.record_priced_quote({
                        "rfq_id": rfq_id, "priced_at": datetime.now(timezone.utc).isoformat(),
                        "status": "DECLINED", "reason_code": "QUEUE_FULL"})

    def rescreen_unresolved(self) -> None:
        """Re-screen RFQs whose legs were unknown, once per catalog growth."""
        version = self.catalog.version
        if version == self._rescreened_version:
            return
        rows = self.store.unresolved_screen_rfqs(version, limit=RESCREEN_BATCH)
        for row in rows:
            result = screen_legs(self.catalog.resolve(row["legs"]))
            rfq = self.store.get_rfq(row["rfq_id"]) or {}
            checks = screen_checks(result, qty_decimal=rfq.get("qty_decimal"),
                                   min_qty=PipelineConfig().min_qty)
            eligible = result.quotable and all(checks.values())
            if result.n_nfl_legs:
                self.nfl_rfqs_seen += 1
            self.store.upsert_rfq_screen(
                row["rfq_id"], n_legs=result.n_legs, n_resolved=result.n_resolved,
                n_nfl_legs=result.n_nfl_legs,
                screen=result.screen if eligible or not result.quotable else "BELOW_MIN_SIZE",
                rank=result.rank if eligible or not result.quotable else 2,
                catalog_version=version, checks_json=json.dumps(checks))
            if eligible and self.selections is not None:
                if self.quoter is None:
                    self.selections.record_priced_quote({
                        "rfq_id": row["rfq_id"],
                        "priced_at": datetime.now(timezone.utc).isoformat(),
                        "status": "DECLINED", "reason_code": "PRICING_UNAVAILABLE"})
                    continue
                accepted = self.quoter.submit(LiveRfq(
                    rfq_id=row["rfq_id"], leg_position_ids=tuple(row["legs"]),
                    side=(self.store.get_rfq_screen(row["rfq_id"]) or {}).get("side") or "YES",
                    direction=(self.store.get_rfq_screen(row["rfq_id"]) or {}).get("direction") or "BUY",
                    qty_decimal=str(rfq["qty_decimal"]) if rfq.get("qty_decimal") is not None else None,
                    cash_order_qty=str(rfq["cash_order_qty"]) if rfq.get("cash_order_qty") is not None else None,
                    received_at=rfq.get("created_time")))
                if not accepted:
                    self.selections.record_priced_quote({
                        "rfq_id": row["rfq_id"],
                        "priced_at": datetime.now(timezone.utc).isoformat(),
                        "status": "DECLINED", "reason_code": "QUEUE_FULL"})
        if len(rows) < RESCREEN_BATCH:  # backlog drained for this version
            self._rescreened_version = version


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

    try:
        capture = RfqCapture(Path(args.data_dir), price_live=True)
        capture.start()
        adapter.start()
    except BaseException:
        reader_lock.release()
        raise

    stop = {"flag": False}

    def _handle_signal(signum: int, frame: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    start_time = time.monotonic()
    last_log = start_time
    log.info("capture started -> %s", args.data_dir)
    try:
        while not stop["flag"]:
            now = datetime.now(timezone.utc)
            for item in adapter.poll(now):
                capture.handle(item, now)
            capture.rescreen_unresolved()
            capture.heartbeat(adapter)
            if time.monotonic() - last_log >= args.log_every:
                log.info(
                    "rfqs=%d nfl=%d trades=%d catalog=%d gateway_connected=%s",
                    capture.rfqs_seen, capture.nfl_rfqs_seen,
                    capture.trades_seen, len(capture.catalog), adapter.connected)
                last_log = time.monotonic()
            if args.duration is not None and time.monotonic() - start_time >= args.duration:
                break
            adapter.wait_for_items(args.poll_interval)
    finally:
        adapter.stop()
        capture.stop()
        reader_lock.release()
        log.info("capture stopped: rfqs=%d nfl=%d trades=%d",
                 capture.rfqs_seen, capture.nfl_rfqs_seen, capture.trades_seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())

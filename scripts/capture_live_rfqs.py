"""Standalone capture of live Polymarket RFQs to local storage.

No pricing, no dashboard wiring -- this only listens and saves, using the
existing receive-only quoter-gateway adapter
(``combo_mm.intl_gateway.InternationalQuoterGatewayAdapter``). Run it
continuously (e.g. in a background process/tmux/systemd unit) to build a
real RFQ dataset over time. There is no way to backfill RFQs from before
this script was running -- Polymarket has no historical RFQ endpoint (see
README "Historical RFQ data"); the gateway stream delivers only new events.

Writes, under ``--data-dir`` (default ``data/live``):

- ``rfq_raw.jsonl``     -- every RFQ_REQUEST / RFQ_TRADE event, one JSON
  object per line, exactly what the gateway adapter emits. This is the only
  place a trade's accepted price/size survive: the structured store below
  only keeps the fields ``normalize()`` recognizes (see
  ``combo_mm/normalize.py``), which does not include gateway-native trade
  extras.
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
from combo_mm.rfq_screen import screen_legs
from combo_mm.store import EventStore

log = logging.getLogger("capture_live_rfqs")

RESCREEN_BATCH = 5000


def _combo_side(raw: Dict[str, Any]) -> Optional[str]:
    """Gateway legs inherit the combo side, so the first leg's side is the combo's."""
    legs = raw.get("comboLegs") or []
    side = legs[0].get("side") if legs and isinstance(legs[0], dict) else None
    return str(side) if side else None


class RfqCapture:
    """Persists every live RFQ event to a raw JSONL log and a structured store.

    Deliberately does not run the shadow quoting engine: this is archival
    only, mirroring ``combo_mm.live_monitor.LiveMonitor`` minus pricing.
    """

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.raw_path = data_dir / "rfq_raw.jsonl"
        self._raw_file = self.raw_path.open("a", encoding="utf-8")
        self.store = EventStore(str(data_dir / "rfq_capture.db"), synchronous="NORMAL")
        self.catalog = ComboMarketCatalog(cache_path=data_dir / "combo_markets.json")
        self.rfqs_seen = 0
        self.trades_seen = 0
        self.nfl_rfqs_seen = 0
        self._rescreened_version = -1

    def start(self) -> None:
        self.catalog.start()

    def stop(self) -> None:
        self.catalog.stop()
        self._raw_file.close()
        self.store.close()

    def handle(self, item: Dict[str, Any], now: datetime) -> None:
        if item.get("kind") != "event":
            return
        raw = dict(item["raw"])
        # Write the raw frame FIRST and unconditionally: this is the
        # lossless record, independent of whether normalize()/apply()
        # accept it.
        self._raw_file.write(
            json.dumps({"received_at": now.isoformat(), "raw": raw}) + "\n")
        self._raw_file.flush()
        try:
            event = normalize(raw, now=now)
        except NormalizeError:
            log.warning("dropping malformed frame", exc_info=True)
            return
        applied = self.store.apply(event, source="live_capture")
        if not applied:
            return
        if event.event_type == "rfq_created" and event.rfq_id:
            self.rfqs_seen += 1
            self._screen(raw, event.rfq_id)
        elif event.event_type == "rfq_closed" and "price" in raw:
            # Only gateway RFQ_TRADE broadcasts carry price/size (see
            # combo_mm.intl_gateway.map_rfq_trade); a plain rfq_closed
            # (deleted, no quote accepted) does not.
            self.trades_seen += 1

    def _screen(self, raw: Dict[str, Any], rfq_id: str) -> None:
        legs = [str(leg.get("symbol")) for leg in raw.get("comboLegs") or []
                if isinstance(leg, dict) and leg.get("symbol") is not None]
        if not legs:
            return
        result = screen_legs(self.catalog.resolve(legs))
        if result.n_nfl_legs:
            self.nfl_rfqs_seen += 1
        self.store.upsert_rfq_screen(
            rfq_id, n_legs=result.n_legs, n_resolved=result.n_resolved,
            n_nfl_legs=result.n_nfl_legs, screen=result.screen, rank=result.rank,
            catalog_version=self.catalog.version,
            direction=raw.get("direction") or None, side=_combo_side(raw),
            condition_id=raw.get("condition_id") or None,
            submission_deadline=raw.get("submission_deadline") or None)

    def rescreen_unresolved(self) -> None:
        """Re-screen RFQs whose legs were unknown, once per catalog growth."""
        version = self.catalog.version
        if version == self._rescreened_version:
            return
        rows = self.store.unresolved_screen_rfqs(version, limit=RESCREEN_BATCH)
        for row in rows:
            result = screen_legs(self.catalog.resolve(row["legs"]))
            if result.n_nfl_legs:
                self.nfl_rfqs_seen += 1
            self.store.upsert_rfq_screen(
                row["rfq_id"], n_legs=result.n_legs, n_resolved=result.n_resolved,
                n_nfl_legs=result.n_nfl_legs, screen=result.screen, rank=result.rank,
                catalog_version=version)
        if len(rows) < RESCREEN_BATCH:  # backlog drained for this version
            self._rescreened_version = version


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data/live",
                        help="output directory (default: data/live)")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="seconds between drains of the adapter buffer")
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

    capture = RfqCapture(Path(args.data_dir))
    capture.start()
    adapter.start()

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
            if time.monotonic() - last_log >= args.log_every:
                log.info(
                    "rfqs=%d nfl=%d trades=%d catalog=%d gateway_connected=%s",
                    capture.rfqs_seen, capture.nfl_rfqs_seen,
                    capture.trades_seen, len(capture.catalog), adapter.connected)
                last_log = time.monotonic()
            if args.duration is not None and time.monotonic() - start_time >= args.duration:
                break
            time.sleep(args.poll_interval)
    finally:
        adapter.stop()
        capture.stop()
        log.info("capture stopped: rfqs=%d nfl=%d trades=%d",
                 capture.rfqs_seen, capture.nfl_rfqs_seen, capture.trades_seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())

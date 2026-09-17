#!/usr/bin/env python3
"""Demo: run the scripted session end-to-end through StreamConsumer.

Uses the simulated transport (no network), including a mid-stream
disconnect, reconnect with backoff, and recovery_sync (open stream ->
GetRFQs(open) -> GetQuotes(self) -> stream events). Shadow quotes are
priced in exchange time and recorded; no CreateQuote RPC is ever issued
(paper mode).

Prints a summary: items seen, reconnects, RFQs/quotes, fills/positions,
shadow decisions, and the transport's CreateQuote call count (must be 0).
"""
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm import (  # noqa: E402
    ConsumerConfig,
    EventStore,
    FillsLedger,
    LegBookCache,
    PipelineConfig,
    ReferenceCache,
    ShadowQuotingEngine,
    SimulatedDropCopyTransport,
    SimulatedTransport,
    StreamConsumer,
    drain_drop_copy,
    fixtures,
    normalize,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    session, combos = fixtures.build_session()
    config = PipelineConfig(paper_mode=True, db_path=":memory:")
    transport = SimulatedTransport(session, fixtures.SELF_USER_ID, combos)
    store = EventStore(config.db_path)
    books = LegBookCache(staleness_ms=config.staleness_ms)
    reference = ReferenceCache(transport, ttl_s=config.reference_ttl_s)
    engine = ShadowQuotingEngine(store, books, reference, config,
                                 params_version=config.params_version)

    # Dedupe on the stable event key: the dispatch below replays items for
    # every delivery (including redeliveries), and quoting the same event
    # twice would mint a spurious second draft.
    seen_keys = set()

    def on_event(item) -> None:
        kind = item.get("kind")
        if kind == "book":
            books.update(
                symbol=item["symbol"], bid=item.get("bid"), ask=item.get("ask"),
                bid_size=item.get("bid_size"), ask_size=item.get("ask_size"),
                updated_at=item.get("ts"), seq=item.get("seq"))
        elif kind == "event":
            raw = item.get("raw") or {}
            if raw.get("event_type") in ("rfq_created", "rfq_updated"):
                try:
                    event = normalize(raw)
                except Exception:
                    return
                if event.event_key in seen_keys:
                    return
                seen_keys.add(event.event_key)
                engine.maybe_quote(event)

    expected = len([i for i in session
                    if i.get("kind") in ("book", "event")
                    and i.get("stream", True)])
    consumer_cfg = ConsumerConfig(
        backoff_initial_ms=1,
        backoff_max_ms=10,
        watchdog_silence_s=60.0,
        on_event=on_event,
    )
    consumer = StreamConsumer(transport, store, consumer_cfg)
    consumer.start()
    deadline = time.monotonic() + 30.0
    while consumer.events_seen < expected and time.monotonic() < deadline:
        time.sleep(0.05)
    consumer.stop()
    if consumer.events_seen < expected:
        print(f"WARNING: only {consumer.events_seen}/{expected} items consumed")

    # Fills reconcile exclusively through Drop Copy.
    fills_before = store.get_fill_stats()["fills"]
    resume_token = drain_drop_copy(
        SimulatedDropCopyTransport(fixtures.build_drop_copy_feed()),
        store,
        now=fixtures.BASE_TS,
    )
    n_fills = store.get_fill_stats()["fills"] - fills_before
    print(f"drop copy: {n_fills} fills applied (resume_token={resume_token})")

    print("\n=== pipeline summary ===")
    print(f"items seen:           {consumer.events_seen}")
    print(f"events applied:       {consumer.events_applied}")
    print(f"reconnects:           {consumer.reconnects}")
    print(f"consumer state:       {consumer.state}")

    print("\n=== RFQs ===")
    for r in store.list_rfqs():
        print(f"  {r['rfq_id']}  {r['symbol']}  {r['status']}")

    print("\n=== quotes ===")
    for q in store.list_quotes():
        print(f"  {q['quote_id']}  rfq={q['rfq_id']}  {q['status']}")

    print("\n=== shadow quote decisions ===")
    for d in store.get_shadow_decisions(limit=100):
        print(f"  {d['rfq_id']}  {d['decision']}  "
              f"fair={d['fair_price']:.4f}  reason={d['reason']}")

    print("\n=== fills / positions ===")
    for f in store.get_fills_for_position():
        print(f"  {f['fill_id']}  {f['symbol']}  {f['side']}  "
              f"{f['qty']} @ {f['price']}")
    for p in FillsLedger(store).all_positions():
        print(f"  {p['symbol']}: net={p['net_qty']} avg={p['avg_price']:.4f}")

    print(f"\nrfq stats:   {store.get_rfq_stats()}")
    print(f"quote stats: {store.get_quote_stats()}")
    print(f"fill stats:  {store.get_fill_stats()}")
    print(f"shadow stats:{store.get_shadow_stats()}")
    print(f"transport CreateQuote calls (must be 0): "
          f"{len(transport.create_quote_calls)}")
    assert not transport.create_quote_calls, "paper mode violated!"


if __name__ == "__main__":
    main()

# Kairos RFQ integration — design

Status: draft for review · Date: 2026-10-01 · Source: <https://docs.kairos.trade/rfq/overview>,
[REST and streaming](https://docs.kairos.trade/rfq/rest-and-streaming), [FIX](https://docs.kairos.trade/rfq/fix)

## 0. Go/no-go gate (read first)

**Do not start implementation until Kairos confirms in writing that a third-party
maker can quote Polymarket combo RFQs through the Kairos RFQ Network.** As of
2026-10-01 the public docs do not support that:

- The RFQ Network overview lists only Kalshi (FIX, responder mode) as live, and
  maker quote relay is disabled pending venue conformance testing.
- The Polymarket combo endpoints (`guides/combos`, "buy a combo via the RFQ
  gateway") describe Kairos as the **requester**: it creates the RFQ, accepts
  the best quote, and settles on-chain. No maker/quoter path for Polymarket is
  documented, and the changelog has no RFQ Network or Polymarket-maker entry.

If the answer is "Kalshi only" or "no date", this integration does not serve the
Totalis NFL/Polymarket scope and should be shelved. The existing Polymarket
quoter-gateway adapter (`combo_mm/intl_gateway.py`) stays the primary route, and
the effort goes into getting its quoting side enabled. A Kalshi-only build would
be a separate project (new leg catalog, Kalshi settlement rules, different
liquidity) and needs its own go/no-go.

The rest of this document describes the design **conditional on the gate
passing**. Phase 0 in §9 is the gate.

## 1. Goal and scope

Add Kairos as a second RFQ venue for the paper/shadow quoting bot, as a
**market maker** (the "Provide Liquidity" mode): receive normalized venue RFQs
over REST + WebSocket, price them with the existing NFL pricer and inventory
risk, and eventually submit firm quotes and track acceptances to execution.

In scope: a Kairos transport (read path), a quote submission path with a
shadow → live ladder, acceptance/execution tracking, restart/reconnect safety,
a simulator for tests.

Out of scope: requester-side RFQs (`POST /v1/rfqs`; Kairos fanout is not live),
FIX (REST/WS covers our needs; revisit only if latency demands it), Kairos
order/execution APIs, and changing the pricing model.

## 2. What Kairos gives us (and doesn't)

### Known from the docs

| Item | Detail |
|---|---|
| Hosts | `https://rfq.kairos.trade` (global), `https://us-east-1.rfq.kairos.trade`; active-active |
| Auth | Headers `X-Client-Id`, `X-Api-Key`, `X-Api-Secret`, or RS256 JWT. Maker needs scope `rfq:quote` (+ `rfq:read` for the stream). A JWT with no `scopes` claim **cannot quote**. IP allowlist misses return **403**, not 401 |
| Discover | `GET /v1/exchange-rfqs?venue=&limit=&cursor=` (expiry ordered, `next_cursor`, `observed_at`) |
| Quote | `POST /v1/quotes`, `PUT /v1/quotes/{id}` (revision must be exactly `current+1`), `DELETE /v1/quotes/{id}`, `GET /v1/quotes/{id}`. There is **no list-my-quotes endpoint** |
| Stream | `GET /v1/stream?after=<sequence>`: server-push only, JSON frames `{sequence, event_id, resource_id, type, occurred_at, data}`; ~250 ms poll, ≤250 events/poll; reconnect may redeliver |
| Events | `rfq.*`, `quote.{created,submitted,revised,withdrawn,expired,disposition}`, `acceptance.{pending,venue_accepted,executed,rejected,expired,execution_unknown}`, `execution.normalized.v1`; `rfq:quote` holders also get a venue-RFQ maker broadcast |
| Close codes | 1000 reconnect with last `after`; 1008 auth revoked → new token; 1011 replay failed → reconnect with the **same** `after` |
| Mutations | Every POST/PUT/DELETE needs `Idempotency-Key`; a replay returns the original resource even with a different body. Decimals are strings; body ≤ 1 MiB |
| Limits | 25 rps, burst 50, per principal; `429` with `Retry-After: 1` |
| Lifecycles | RFQ `open → partially_filled → filled \| cancelled \| expired`; quote `live → partially_filled → filled \| withdrawn \| expired`; acceptance `pending_routing → venue_accepted → executed \| rejected \| expired \| execution_unknown` |
| Semantics | Acceptance is a **reservation, not a fill**. Cross-venue mapping is a routing hint, not a guarantee of equal contract rules |

### Blockers and unknowns — resolve before building on assumptions

1. **Maker quote relay is disabled** pending venue conformance testing, and Kalshi is
   the only live venue (FIX, responder mode). We can likely discover RFQs but
   **cannot yet get quotes to a venue**. Live submission is gated on Kairos
   enabling it; ask for a date and a sandbox.
2. **Venue ≠ Polymarket.** Kalshi RFQ legs are Kalshi tickers. Our catalog,
   `LiveLegBooks`, and `NflLivePricer` are keyed on Polymarket position ids.
   Ask Kairos whether Polymarket combos will appear on this network.
3. **Schemas are not public.** The doc shows only the create-RFQ body. The
   exchange-RFQ item, quote request/response, acceptance, execution, and
   `quote.disposition` shapes live in the RFQ service's
   `spec/openapi.yaml` / `spec/asyncapi.yaml` (not published). Request these
   files. Until then, Phase 0 captures raw frames and the mapping layer is
   written defensively (same approach as `map_rfq_request`).
4. No documented app-level heartbeat on the stream; no bulk-cancel; per-side
   expiry rules and tick grids are only described loosely (tick grids exist in
   the Market Data API).

## 3. Fit with the current architecture

The pipeline already has the right seams:

```text
EventSource.poll() ─▶ PollingConsumer ─▶ normalize() ─▶ EventStore.apply()
(intl_gateway.py)      (consumer.py)      (normalize.py)   idempotent on event_key
                                   └▶ RfqCapture ─▶ screen ─▶ NflLivePricer ─▶ risk ─▶ paper draft
```

Kairos slots in as a new `EventSource`, mirroring
`InternationalQuoterGatewayAdapter` (daemon thread owns an asyncio websocket,
buffers mapped raw dicts, `poll()` drains, reconnect with jittered backoff,
silence watchdog, `stats()` for the dashboard). New code is limited to the
transport, the mapping layer, a leg resolver, and the quote/acceptance path.

```text
                  ┌────────────── combo_mm/kairos/ ──────────────┐
 Kairos WS /v1/stream ─▶ KairosStreamSource (EventSource) ─┐     │
 Kairos REST exchange-rfqs ─▶ bootstrap/gap-fill ──────────┤     │
                                                           ▼     │
                              map_exchange_rfq / map_event()     │
                                                           │     │
                  KairosClient (REST, rate limit, retry) ◀─┼─ KairosQuoteSubmitter ◀─ approved draft
                  └───────────────────────────────────────┘     │
                                                           ▼
   existing: normalize → EventStore → screen → pricer → risk → draft ─▶ (flag) submitter
```

### Module layout

| File | Responsibility |
|---|---|
| `combo_mm/kairos/config.py` | `KairosCredentials.from_env()` (`KAIROS_CLIENT_ID/API_KEY/API_SECRET`, `KAIROS_BASE_URL`; same `.env` fallback and secret-free `__repr__` as `GatewayCredentials`) |
| `combo_mm/kairos/client.py` | Sync REST client: auth headers, `Idempotency-Key`, token-bucket limiter (≤ 20 rps to stay under 25), `Retry-After`, typed errors (§6), region failover on `503 exchange_rfq_index_unavailable` |
| `combo_mm/kairos/stream.py` | `KairosStreamSource(EventSource)`: WS reader, cursor persistence, backoff, close-code handling, `stats()` |
| `combo_mm/kairos/mapping.py` | Pure functions: Kairos event/RFQ → raw dict accepted by `normalize()`; leg ticker → internal leg |
| `combo_mm/kairos/legs.py` | `KairosLegResolver`: Kalshi ticker → `{game, market_type, line, side}`, feeds eligibility and `LiveLegBooks` |
| `combo_mm/kairos/quoter.py` | `KairosQuoteSubmitter`: draft → `POST/PUT/DELETE /v1/quotes`, revision tracking, withdraw-all |
| `combo_mm/kairos/acceptances.py` | Acceptance/execution tracker → inventory reservations and fills |
| `tests/kairos_sim.py` | In-process fake Kairos (REST + WS) for deterministic tests |

## 4. Read path (Phase 1–2)

### 4.1 Stream consumption

- Connect to `/v1/stream?after=<last_sequence>` with auth headers. Server-push
  only, so there is no subscribe frame; `after` is the only control.
- **Cursor**: persist `sequence` in a new table `stream_cursor(source TEXT PRIMARY KEY,
  sequence INTEGER, updated_at TEXT)` **after** the event is applied to the
  store (at-least-once; the store dedups). On start, resume from the stored
  cursor; with none, start live and bootstrap via REST.
- **Idempotency**: `event_key = "kairos:" + event_id`. `EventStore.apply` already
  ignores duplicate keys and applies per-entity monotonic `updatedTime`, so
  redelivery after a reconnect is safe by construction.
- **Reconnect**: backoff 1 s → 60 s with ±25 % jitter (reuse the intl gateway
  constants). Close 1000 → reconnect at last cursor; 1008 → re-read credentials
  and, for JWTs, fetch a fresh token before retrying; 1011 → reconnect at the
  **same** cursor (do not advance). Auth failure (`401`) backs off like the
  gateway adapter and surfaces in `stats()["auth"]`.
- **Liveness**: the stream has no documented heartbeat and may be legitimately
  quiet. Use websocket ping/pong (20 s) for transport health, and a slower
  REST probe (`GET /v1/exchange-rfqs?limit=1`, read `observed_at`) every 60 s
  for application health. Recycle the socket after 120 s with no frames *and*
  a failing probe, not on silence alone.
- **Backpressure**: ≤250 events per 250 ms poll is ~1000 ev/s worst case. Keep
  the bounded buffer (10 000, drop-oldest, counted in `stats()`) and drop only
  RFQ-created events under pressure, never acceptance/execution events.

### 4.2 Bootstrap and gap fill

On cold start and after any gap the cursor cannot cover (sequence too old /
1011 loop): page `GET /v1/exchange-rfqs` (follow `next_cursor`) and synthesize
`rfq_created` for open RFQs not in the store, then resume the stream. RFQs that
vanish from the listing without an event are closed by the existing deadline
sweeper (`client_derived=True`), same as the Polymarket path.

### 4.3 Mapping to the canonical event model

`normalize()` expects the Polymarket-shaped envelope. Rather than fork the
event model, `mapping.py` produces the same envelope, plus Kairos-native extras
that survive in raw persistence (the intl gateway does the same).

| Kairos | Canonical | Notes |
|---|---|---|
| `rfq.created` / venue RFQ | `rfq_created` | `comboLegs[i] = {symbol: "<venue>:<market_id>", side: YES\|NO}`; `ratio` kept in extras (reject non-1 ratios in eligibility until priced) |
| `rfq.cancelled` / `rfq.expired` | `rfq_closed` (+ extra `reason`) | Treat as "stop quoting" |
| `quote.created/submitted/revised` | `quote_created` | Our quotes only |
| `quote.withdrawn` / `quote.expired` | `quote_deleted` | |
| `acceptance.pending` | `quote_accepted` | Reservation, **not** a fill |
| `acceptance.venue_accepted` | `quote_confirmed` | |
| `acceptance.executed`, `execution.normalized.v1` | `quote_executed` | Fill booking happens in §5.4 |
| `acceptance.rejected/expired` | `quote_deleted` + release reservation | |
| `acceptance.execution_unknown` | new `quote_execution_unknown` | Hold reservation until reconciled |
| `quote.disposition` | stored raw, logged | Payload undocumented; capture first |

Add a `venue` column to `rfq` (default `polymarket`) and namespace `rfq_id` as
`kairos:<id>` so both sources can write to one store without collisions. The
dashboard gets a venue filter; no other UI change is needed for Phase 1.

### 4.4 Leg resolution and eligibility

This is the largest piece of real work. The pricer needs, per leg, a game, a
market type (moneyline/spread/total), a line, and a live price.

- `KairosLegResolver` maps Kalshi tickers to internal legs. Prefer Kairos'
  own catalog over scraping Kalshi: *Kalshi sports taxonomy*, *live Kalshi
  sports games (NFL)*, *matched markets*, and market metadata give team, line,
  and the cross-venue link to Polymarket markets. Cache to
  `data/live/kairos_markets.json.gz` the way `ComboMarketCatalog` caches.
- Leg prices: start with Kairos Market Data REST (marks / batch prices, ≤300
  markets per call) polled for the legs of an in-flight RFQ; move to the
  protobuf market-data websocket only if solve latency shows the poll is the
  bottleneck. Prices feed the existing `LiveLegBooks` interface.
- Where a Kalshi leg has a verified Polymarket match, the existing pricer and
  params apply unchanged. Where it does not, decline with a new reason code
  `LEG_UNMAPPED` (visible in the dashboard screen). **Never** price an unmapped
  leg by assuming equivalence; Kairos states mappings are routing hints only,
  and settlement rules can differ.
- Eligibility keeps today's NFL same-game filter (`eligibility.py`,
  `rfq_screen.py`), extended with: non-unit leg ratio, unsupported
  `requested_sides`, `expires_at` too close to solve time, and
  `minimum_partial` larger than our size cap.

## 5. Write path (Phase 3–4)

### 5.1 Quote shape

Kairos RFQs are two-way (`requested_sides ⊆ {bid, offer}`) with `package_quantity`
and `minimum_partial`; quotes carry per-side price/expiry. Our `LiveQuote`
already produces `bid`/`ask` with `bid_qty`/`ask_qty`. Mapping:

- Quote only the sides requested; omit a side rather than quoting zero size.
- Quantity ≤ `package_quantity`, ≥ `minimum_partial`, ≤ risk-adjusted size.
- Prices are decimal strings snapped to the market's tick grid (fetch once per
  market, cache; round **away** from fair so snapping never improves our price).
- Per-side expiry = `min(rfq.expires_at, now + quote_ttl)`; default
  `quote_ttl = 30 s` for NFL (configurable) so a stale model never rests.

Exact field names and the position of per-side expiry are TBD (§2 unknown 3);
isolate them in `quoter._build_body()` so a schema fix is one function.

### 5.2 Submission ladder

A single `quote_mode` setting, default `shadow`:

| Mode | Behavior |
|---|---|
| `shadow` (default) | Today's behavior: store the draft, never call Kairos mutations |
| `dryrun` | Build and validate the exact request body, log it, store it; no network write |
| `live` | Submit. Requires `rfq:quote` scope verified at startup, `KAIROS_LIVE_ENABLED=1`, venue capability check (`/v1/venues/capabilities`) showing maker relay on for the RFQ's venue, and a passing kill-switch check |

A draft is submitted only after `_check_quote_risk` passes, exactly as the
paper path reserves capacity today.

### 5.3 Revision and idempotency

- One live quote per RFQ (matches `deterministic_quote_id`). First submit =
  `POST`; re-price = `PUT` with `revision = current + 1`.
- `Idempotency-Key = sha256("quote" | rfq_id | revision | body_hash)[:32]`,
  derived deterministically, **persisted before the call**. After a crash, retry
  the same key; Kairos returns the original resource. Because Kairos ignores the
  body on replay, a changed price must use a new revision and therefore a new
  key. Never reuse a key for a different command.
- On `409 stale_quote_revision`: `GET` the quote, adopt its revision, re-evaluate
  whether to re-price, then retry once. Never blind-retry.
- On `409 quote_expired` or terminal `conflict`: stop quoting that RFQ.
- Persist `kairos_quotes(quote_id, rfq_id, revision, status, idem_key, body_json, updated_at)`.
  Because Kairos has no list-my-quotes endpoint, this table is the only
  restart index; on startup `GET` each non-terminal row to reconcile.

### 5.4 Acceptance, execution, inventory

The key invariant: **acceptance reserves, execution fills.**

```text
acceptance.pending ──▶ reserve exposure (inventory "pending", paper capital held)
acceptance.venue_accepted ──▶ keep reservation
acceptance.executed / execution.normalized.v1 ──▶ convert reservation → fill
acceptance.rejected / expired ──▶ release reservation
acceptance.execution_unknown ──▶ KEEP reservation, alert, poll GET /v1/executions/{id}
```

- Book a fill only from `execution.normalized.v1` (or `GET /v1/executions/{id}`
  once `executed`). Never from the `202` or from `pending_routing`.
- `execution_unknown` is treated as filled for risk (conservative) until
  resolved; it raises a dashboard alert and blocks further quoting in that game.
- Partial fills: quote and RFQ both pass through `partially_filled`; size the
  remaining quote from remaining unreserved quantity (`insufficient_remaining_quantity`
  is the server's backstop, not our sizing logic).
- Settlement tracking (`docs/settlement-tracking.md`) needs a Kalshi resolution
  source; Kairos exposes resolution lifecycle endpoints, so reuse them rather
  than a Kalshi-direct feed.

### 5.5 Kill switch

`scripts/risk_halt.py` today only stops the paper path. Extend it so a halt
(1) sets `quote_mode` to `shadow` in a shared flag the submitter checks before
every call and (2) withdraws every non-terminal row in `kairos_quotes` with
`DELETE /v1/quotes/{id}`, in parallel, within the rate limit. There is no bulk
cancel, so a large book takes `N / 20` seconds; this is why §5.1 uses short
quote TTLs and why `max_live_quotes` (default 50) is a hard cap.

## 6. Error handling

| Response | Action |
|---|---|
| `400 invalid_request / invalid_json` | Bug or schema drift: log body shape (not secrets), do not retry, count |
| `400 idempotency_key_required` | Bug; fail loudly in tests |
| `401` | Re-read credentials; backoff; surface `auth: rejected` |
| `403 forbidden` | Likely IP allowlist (check egress IP) before suspecting credentials |
| `403 insufficient_scope` | Read `required_scope`; halt live mode; do not retry |
| `404` | Treat quote as gone; reconcile |
| `409 stale_quote_revision` / `quote_expired` / `insufficient_remaining_quantity` / `conflict` | See §5.3; re-read, never blind retry |
| `429` | Sleep `Retry-After` (1 s) with jitter; limiter should prevent this |
| `500` | Retry with backoff, same idempotency key |
| `503 auth_dependency_unavailable` | Auth fails closed; retry with backoff; stop quoting meanwhile |
| `503 exchange_rfq_index_unavailable` | Fail over to the other regional host |

Network timeouts on a mutation have an uncertain outcome: retry with the **same**
idempotency key, then `GET` to confirm state before doing anything else.

## 7. Operations

- Credentials: add `KAIROS_*` to the gitignored `.env`; least privilege means
  request `rfq:read` + `rfq:quote` only (no `rfq:accept`/`rfq:create`/`*`).
  Never log headers; extend the secret-free `__repr__` pattern.
- Run it inside the existing capture process (`scripts/capture_live_rfqs.py
  --source kairos|gateway|both`), so the process lock, supervision units, and
  heartbeat row (`live_engine_health`) stay the single writer. Add per-source
  counters to the heartbeat: connects, reconnects, last frame, cursor,
  `observed_at` lag, live quotes, open acceptances.
- Latency: record `rfq_posted → decided → submitted → quote_ack` in
  `quote_latency` (add `submitted_at`, `ack_at`). The first question live mode
  must answer is whether our solve time beats typical RFQ lifetimes.
- Region: start on the global host; pin to `us-east-1` if co-located and add the
  other as failover.

## 8. Testing

- `tests/kairos_sim.py`: local fake serving `/v1/exchange-rfqs`, `/v1/quotes`,
  `/v1/stream` with sequence/`after` replay, scripted close codes, injected
  `429/409/503`, and the idempotency-replay rule (original resource on replay).
- Unit: mapping golden files from captured frames; limiter; revision/idempotency
  key derivation; reservation state machine including `execution_unknown`.
- Fault injection (extend `tests/test_fault_injection.py`): disconnect between
  apply and cursor write → redelivery is a no-op; 1011 loop does not advance the
  cursor; crash after persisting an idempotency key but before the POST.
- Contract test marked `@pytest.mark.live`, skipped without credentials like the
  existing signal integration test.

## 9. Phases

| Phase | Deliverable | Exit criteria |
|---|---|---|
| 0. Gate + access | **Written confirmation that Polymarket combos can be quoted by a third-party maker via Kairos (§0)**, then credentials, scopes, IP allowlist, schema files / sandbox | Confirmation received; authenticated `GET /v1/venues/capabilities` shows Polymarket with maker support, and `exchange-rfqs` returns Polymarket combo RFQs |
| 1. Capture | `KairosStreamSource` + raw logging only, cursor, reconnect, heartbeat | 24 h of frames captured; real schemas committed as golden fixtures |
| 2. Map + shadow | Mapping, leg resolver, eligibility, paper decisions on Kairos RFQs, `venue` in dashboard | Paper quotes saved; `LEG_UNMAPPED` rate known |
| 3. Dry run | `quote_mode=dryrun`, request bodies validated against schemas, tick snapping | Bodies pass Kairos validation in sandbox (or review) |
| 4. Live, tiny | `quote_mode=live`, 1 game, `max_live_quotes` and per-RFQ notional far below paper limits, acceptance/execution tracking | Reservations reconcile to executions with zero `execution_unknown` left unresolved; kill switch drill passes |
| 5. Scale | Raise limits per `docs/risk-model.md` after reviewing realized vs. paper P&L | Sign-off |

Phase 4 additionally depends on Kairos enabling maker quote relay.

## 10. Open questions for Kairos

1. Date for maker quote relay; is there a sandbox/test venue?
2. Will Polymarket combos be on this network, or Kalshi only?
3. `spec/openapi.yaml` and `spec/asyncapi.yaml`.
4. Per-side expiry rules, tick/size constraints, and whether a quote can be
   one-sided on a two-sided RFQ.
5. Is there a quote-list or bulk-withdraw endpoint, or a cancel-on-disconnect
   option?
6. Stream retention window for `after` replay, and the behavior when the cursor
   is older than retention.
7. Maker-side last-look: can we decline after `acceptance.pending`, or is it final?
8. Fee model for makers; rate-limit increase path beyond 25 rps.

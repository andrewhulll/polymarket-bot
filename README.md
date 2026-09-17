# polymarket-bot

Paper-trading combo RFQ pipeline for Polymarket, built for Totalis (a prediction-markets startup).
Listens for combo RFQs, stores raw messages and normalized records, prices combos with a minimal
V1 independent-leg model, shadow-quotes without ever submitting, and replays sessions
deterministically. The simulated feed is the default; Retail REST polling is supported for live
market data; the Exchange gRPC adapter is stubbed for later.

> Nothing in this repo places orders. Paper mode is the default and the pipeline refuses to
> submit quotes: draft quotes are computed, stored, and logged — never sent.

## The Totalis brief

The text below is the source assignment from Totalis, reproduced verbatim.

Totalis Trading Bot Project - we've been working on the trading bot on the polymarket-bot repo. Well I am doing this for Totalis a prediction markets startup and heres what they are looking for:

Build a paper-trading bot that listens for Polymarket RFQs, prices combos, manages inventory, and evaluates its performance through historical replay.

1. RFQ listener and storage

Listen for new RFQs and lifecycle updates.

Reconnect safely after a disconnect.

Store raw messages and normalized records.

Capture RFQs, legs, timestamps, draft quote revisions, cancellations, expirations, executions, and settlements.

Make processing idempotent

2. Pricing and correlation

Estimate a fair probability for every leg.

Model dependence between legs rather than assuming independence.

Explain how correlations are estimated.

Produce a fair combo price, quoted price, size, expected edge, and a short explanation of each pricing adjustment.

3. Inventory and risk management (assume we have $50k of initial capital).

Track pending and executed exposure across individual markets and events. and total portfolio risk.

The bot should widen, skew, reduce, or reject quotes as inventory grows.

4. Shadow quoting

For every eligible RFQ, generate and store a draft quote without submitting it live.

5. Backtest

Replay the collected RFQ dataset without using future information. Report:

RFQs received, quoted, rejected, expired, and executed,

Quote and execution rates,

Expected and realized P&L,

Maximum downswing, Maximum upswing,

Inventory and correlated exposure over time,

Results by market type and combo size,

Sensitivity to correlation.

## Scope of this PR

**Implemented in this PR**

- **Step 1 — RFQ listener and storage.** Full event model + normalizer, append-only SQLite event
  store (raw messages + normalized projections), stream consumer with safe reconnect and durable
  recovery reads, simulated feed, and deterministic replay fixtures proving idempotent recovery.
- **Dashboard (observability, Streamlit).** Three views: RFQs (expandable full detail), Pricing &
  quoting (V1 fair price, quoted buy/sell, size, expected edge, per-adjustment explanations), and
  Performance (a step-5-style replay tab over the fixture dataset — RFQs received/quoted/rejected/
  expired/executed, quote and execution rates, expected vs realized P&L, max downswing/upswing,
  inventory/exposure over time). Clearly labeled PAPER/SHADOW.
- **Minimal V1 pricer + shadow quoting** — just enough to make the dashboard real (independent-leg
  product model, spread components, tick rounding, structured reason codes; drafts stored, never
  submitted).
- **Retail live-data adapter** — polling the Polymarket US Retail REST API for RFQ state and leg
  books, with beta-gate handling (see below). Simulated feed remains the default.

**Parked for later**

- **Step 2 (full)** — correlation model beyond the V1 independent-leg baseline (same-game
  dependence, estimation methodology).
- **Step 3** — inventory & risk management on $50k capital (widen/skew/reduce/reject as inventory
  grows; hard limits; kill switch).
- **Step 5 (full)** — formal backtest report (results by market type and combo size, sensitivity to
  correlation). The dashboard Performance tab is a lightweight replay summary, not the full report.

## Components (`combo_mm/`)

| Module | Role |
|---|---|
| `events.py` | Canonical event model + RFQ/quote state machines, mirroring the Polymarket US gRPC contract (`polymarket.v1`). Exact wire field names (`qtyDecimal`, `buyPrice`, …). |
| `normalize.py` | Pure validation/coercion of raw messages into `NormalizedEvent`s. Idempotency key = `event_id` when present, else a stable hash of the payload. |
| `store.py` | Append-only SQLite store: raw events first, then normalized projections. Tables: `raw_events`, `rfq`, `rfq_legs`, `quotes`, `fills`, `dropcopy_state`, `books`, `shadow_decisions`. Exposes `state_digest()` (SHA-256 over the canonical read model) for determinism checks. |
| `stream.py` | `RfqTransport` interface mirroring `RFQAPI` (`StreamRFQEvents` with EMPTY request, `GetRFQs`, `GetQuotes`, `GetCombos`); `SimulatedTransport` (scripted sessions, injectable disconnects); `GrpcTransport` stub (`NotImplementedError` until creds + protos exist). |
| `consumer.py` | Stream consumer: non-blocking dispatch, watchdog (force reconnect when silent), exponential-backoff reconnect with jitter. |
| `recovery.py` | Reconnect recovery in contract order: reopen stream → `GetRFQs(open)` → `GetQuotes(self)` → idempotent apply keyed by entity ID + `updatedTime`. |
| `sources.py` | `EventSource` abstraction (`poll(now)`); `SimulatedEventSource` wraps the scripted feed so polling and streaming share one downstream path. |
| `books.py` | In-memory leg book cache. `get(symbols)` is synchronous, pure in-memory, never blocks and never raises — unknown symbols come back flagged `missing`; per-symbol staleness flags (default 2000 ms). |
| `reference.py` | Combo reference metadata (tick size, price limits, min quantity, fallback leg list), lazy-loaded with TTL. Control-plane only. |
| `quotes.py` | `QuoteTracker`: read model over the `quotes` table exposing current quote state per RFQ. Handles the `rfq_closed`-before-`quote_accepted` race (stop quoting, retain state). |
| `fills.py` | `FillsLedger`: positions (net qty, avg price) derived from the fills table — no separate position table, so a duplicate fill can never double-count. |
| `dropcopy.py` | `DropCopyTransport` interface (streaming only, `resume_token`, scope `read:dropcopy`) + simulated feed + stub. Drop Copy is the source of truth for fills. |
| `auth.py` | Private-Key-JWT → Auth0 structure for the Exchange API (RS256, 3-minute refresh, key rotation, gRPC error mapping). Stubbed — no network, no credentials. |
| `retail.py` | `RetailPollingSource`: Retail REST polling adapter (RFQ list/detail diffing, leg book/BBO refresh, beta-gate fallback). See “Retail live data”. |
| `pricing.py` | Minimal V1 independent-leg pricer (pure, no I/O): bounded microprice / midpoint leg marks, `fair = product(q_i)`, spread = base edge + uncertainty + depth + event risk + buffer, tick rounding, side `"0"` suppression, structured reason codes. |
| `pricer.py` | Pricer seam (issue #2): `Pricer` protocol + `PricerResult`; `V1NaivePricer` adapts `price_combo`. The MVN pricer implements the same interface with zero engine changes. |
| `risk.py` | Risk seam (issue #3): `RiskCheck` protocol + `InventoryState` / `RiskVerdict`; `ConservativeRiskCheck` enforces per-RFQ / per-game / capital hard caps (shrink or reject). The full risk module replaces it behind the same interface. |
| `eligibility.py` | Pure pre-pricing eligibility filter: event type → RFQ present → status terminal → legs present → exchange-time staleness. Skip reasons `SKIP_NO_RFQ` / `SKIP_RFQ_CLOSED` / `SKIP_NO_LEGS` / `SKIP_STALE_RFQ`. |
| `engine.py` | Shadow quoting engine (issue #4): eligibility → pricer → risk → two-sided draft quote, stored in `quotes` (`status='shadow'`, `origin='shadow'`) with a full reproducible input snapshot. Paper-only: no call path to any outbound RPC, `PaperModeError` unless `paper_mode=True`. |
| `paper_backtest.py` | Replay-based paper backtest over fixture/simulated sessions: counts, rates, expected vs realized P&L, swings, exposure over time. No future information (books filtered to `updated_at <= event time`). |
| `fixtures.py` | Scripted sessions: full lifecycle flows, cancelled/expired RFQs, the `rfq_closed` race, duplicate + out-of-order deliveries, a mid-stream disconnect, and a missed `rfq_closed` only recovery can catch. |
| `replay.py` | Deterministic replay harness (virtual clock) producing state digests for byte-for-byte comparison. |
| `config.py` | `PipelineConfig`: paper mode (default true, with startup banner), staleness, reconnect, retail polling, and V1 pricer knobs. Unknown fields are fatal. |

## Event lifecycle

Normalized event types: `rfq_created`, `rfq_updated` (legs/timestamps), `quote_draft_revised`,
`rfq_cancelled`, `rfq_expired` (**client-derived** from deadlines — the stream never emits it),
`rfq_closed`, `quote_created`, `quote_deleted`, `quote_accepted`, `quote_confirmed`,
`quote_executed`, `settlement`, `drop_copy_fill`.

State machines (enforced on apply):

- **RFQ:** `OPEN → QUOTED → ACCEPTED → CONFIRMED → EXECUTED`, with terminal `CANCELLED`,
  `EXPIRED`, `CLOSED` (`rfq_closed` may arrive straight from `OPEN`). No transitions out of
  terminal states — late/out-of-order events that would regress state are logged and ignored.
- **Quote:** `DRAFT → ACTIVE → ACCEPTED → CONFIRMED → EXECUTED`, terminal `DELETED`, `EXPIRED`.
  Documented exception: a draft revision / replacement on a `REPLACED` quote moves it back to
  `ACTIVE` — the one allowed non-monotonic transition.
- **Deterministic quote id:** `{maker_user_id}:{rfq_id}` — one per maker per RFQ; re-calling
  replaces economics under the same id.

Key semantics (per the real contract):

- `settlementPrice` is the raw YES/LONG result in [0,1] — **never inverted** for SELL legs.
  `"0"` is a valid settled price; an absent field means no valid settlement. There is no
  leg-settlement event; latest settlements are picked up via `GetRFQs` during reconciliation.
- `rfq_closed` means *stop quoting*, not *no quote accepted* — quote state is retained until the
  private quote event arrives or `GetQuotes` reconciliation resolves it.
- `quote_executed` means paired orders were *accepted for submission*, not filled — fills
  reconcile through Drop Copy only.

## Idempotency and recovery

- Every inbound record is inserted into `rfq_events` **before** processing, with `INSERT OR IGNORE`
  on the event key — a duplicate is a no-op, never a double-apply (exactly-once).
- Projection apply is additionally keyed by entity ID + `updatedTime`: a change applies only if its
  `updatedTime` is newer than the stored row's (per-entity monotonicity), which is what makes the
  no-replay, unordered, duplicate-tolerant stream safe.
- On **every** (re)connect the consumer pauses, reopens the stream (respecting the 1-stream/sec
  firm limit with backoff), runs `recovery_sync` (`GetRFQs(open)` + `GetQuotes(self)`), reconciles
  (inserts unseen RFQs, marks disappeared ones closed, refreshes quote states and late
  settlements), then resumes. An empty durable read is a valid snapshot, not an error.
- A recorded event log replays through `replay.py` to reproduce identical state — verified by the
  byte-for-byte determinism tests, including a reconnect-mid-stream scenario.

## Configuration

`PipelineConfig` (all fields validated at startup; unknown fields are fatal):

- `paper_mode: bool = True` — full pipeline runs; intended quote RPCs are logged, never sent.
  Startup prints a PAPER MODE banner.
- `staleness_ms = 2000`, `watchlist`, `db_path` (`:memory:` default).
- Reconnect: `backoff_initial_ms = 100` → `backoff_max_ms = 5000` (+20% jitter),
  `watchdog_silence_s = 30.0`, `max_reconnects = None` (retry forever).
- Retail polling: `poll_interval_s = 5.0`, `max_requests_per_poll = 10`.
- V1 pricer (bps unless noted): `base_edge_bps = 15`, `uncertainty_per_leg_bps = 5`,
  `width_weight = 0.5`, `depth_slope_bps = 20`, `event_risk_bps = 5`,
  `operational_buffer_bps = 5`, `tick_size = 0.001`, `price_min/max = 0.001/0.999`,
  `min_qty = 1.0`.
- Shadow engine: `max_per_rfq_notional = 1000.0`, `max_per_game_notional = 5000.0`,
  `initial_capital = 50000.0`, `stale_rfq_ms = 60000`, `params_version = "unversioned"`.

## Shadow quoting engine

The always-on paper-trading loop (issue #4). For every `rfq_created` / `rfq_updated`
event the engine runs four stages:

1. **Eligibility** (`eligibility.py`, pure function) — event type → RFQ present →
   status terminal → legs present (inline or reference fallback) → exchange-time
   staleness. Skips are logged per RFQ (`SKIP_NO_RFQ`, `SKIP_RFQ_CLOSED`,
   `SKIP_NO_LEGS`, `SKIP_STALE_RFQ`); anything else is ignored silently.
2. **Pricing** (`pricer.py`, the #2 seam) — any `Pricer` implementation prices the
   legs into a `PricerResult` (fair value, marginals, correlation adjustment,
   confidence, decline reason). `V1NaivePricer` adapts the existing independent-leg
   `price_combo`; the MVN pricer will implement the same interface with zero
   engine changes.
3. **Risk** (`risk.py`, the #3 seam) — any `RiskCheck` implementation verdicts the
   draft against the quoted notional and the `InventoryState`. `ConservativeRiskCheck`
   shrinks both sides proportionally past the per-RFQ cap (`RISK_SIZE_REDUCED`),
   rejects past the per-game cap (`RISK_GAME_EXPOSURE`) or total capital
   (`RISK_CAPITAL`).
4. **Draft** — a two-sided `DraftQuote` with a deterministic id
   (`shdw-<rfq_id>-<n>`), stored in the `quotes` table with `status='shadow'` and
   `origin='shadow'` (the `CHECK (origin IN ('shadow','live'))` constraint makes
   live vs shadow unmistakable). Every draft carries its full input snapshot as
   canonical JSON — leg marks/prices, model and params versions, inventory state,
   spread and risk knobs — so any draft is reproducible byte-for-byte.

Every outcome (quote, pricer decline, risk decline, eligibility skip) also lands
in `shadow_decisions` with its reason code, which powers the dashboard's
**Engine status** tab: RFQs seen vs quoted vs skipped, the skip-reason breakdown,
and the stored drafts table.

**Safety guarantees (hard requirements):**

- No code path from the engine to any order-submission endpoint — the module has
  no transport reference and no quoting client to import (the retail adapter is
  read-only). A source scan test asserts the engine, pricer, risk, and
  eligibility modules contain none of `create_quote`, `CreateQuote`,
  `submit_order`, `place_order`, `post_order`, `grpc`, `http.client`, `requests.`.
- `PAPER_MODE` guard: constructing `ShadowQuotingEngine` with
  `paper_mode=False` raises `PaperModeError` — quoting logic refuses to run
  without explicit live-trading scaffolding, which does not exist.
- All timestamps are exchange/virtual time (`event.event_at`); the engine never
  reads the wall clock, so fixture replays are deterministic.

Run it:

```bash
python3 scripts/run_pipeline.py        # scripted session through the consumer
python3 -m pytest tests/test_shadow_engine.py -q   # determinism + safety tests
```

## Running

```bash
# Tests (stdlib only + pytest; no network)
python3 -m pytest tests/ -q

# Demo: scripted session through the consumer, incl. a mid-stream disconnect
python3 scripts/run_pipeline.py

# Dashboard (demo/observability — not production)
streamlit run dashboard/app.py
```

The dashboard opens with a PAPER/SHADOW banner. Press **Run simulation** to replay the scripted
session into a SQLite DB, then browse:

1. **RFQs** — every RFQ request, each expandable to full detail (combo symbol, legs with market
   symbol / side / settlement value, size mode and quantity, timestamps, lifecycle state,
   requester ID).
2. **Pricing & quoting** — per RFQ: V1 fair combo price, quoted buy/sell prices, size, expected
   edge, and a human-readable explanation of each pricing adjustment (spread components +
   reason code).
3. **Performance** — paper/shadow replay metrics: RFQs received/quoted/rejected/expired/executed,
   quote and execution rates, expected vs realized P&L, max downswing/upswing, inventory/exposure
   over time.

A data-source selector offers **Simulated feed** (default) vs **Retail live** (activates only when
both retail env vars are set; otherwise it says so and stays simulated).

## Retail live data

Paper-trading pipeline for Polymarket combo RFQs. The core pipeline
(`combo_mm/`) runs on a simulated feed by default; this section covers the
**Retail live-data path**: polling the Polymarket US Retail REST API for
RFQ state and leg market data.

> The Exchange (gRPC) path is simulated-only in this repo: `GrpcTransport`
> and the live Drop Copy transport are explicit `NotImplementedError`
> stubs. Nothing here places orders.

### Installation

Core pipeline: Python 3.12, stdlib only, plus `pytest` for tests.

Retail live data needs the official SDK (optional dependency — the
simulated pipeline and all tests run without it):

```bash
pip install polymarket-us
```

### Credentials

From the **runtime environment only** — never from files, chat, or code:

```bash
export POLYMARKET_US_KEY_ID="..."
export POLYMARKET_US_SECRET_KEY="..."
```

Rules (enforced by tests):

- Both values are read from `os.environ` at source construction and handed
  straight to the SDK, which holds the secret in memory to sign requests
  (`X-PM-Access-Key` / `X-PM-Timestamp` / `X-PM-Signature`, Ed25519).
- They are never logged, persisted, displayed, committed, or asked for.
  Failure paths log the exception *class* only.
- The Secure Vault cannot store this key/secret scheme — env vars are the
  only supported route.

### Streamlit toggle

The dashboard offers a data-source selector:

- **Simulated feed** (default)
- **Retail live** — activates only if *both* env vars are set. Otherwise the
  dashboard says so plainly, asks you to set the two env vars, and stays on
  the simulated feed. Credential values are never displayed.

### How live polling works

Retail has **no real-time RFQ event stream** — streaming is Exchange (gRPC)
only. The adapter (`combo_mm/retail.py::RetailPollingSource`, an
`EventSource`) polls on a configurable interval (default ~5 s):

1. List RFQs (one request).
2. Fetch targeted RFQ details (only when a list row lacks leg/size fields).
3. Diff by RFQ id + `updatedTime`; emit the existing event model —
   `rfq_created` / `rfq_updated` / `rfq_closed` — plus `rfq_expired`
   (client-derived) for `EXPIRED` statuses.
4. Fetch leg book/BBO and feed the existing `LegBookCache` with
   `ts` = fetch time (staleness flags apply as usual).

Each poll is capped by `max_requests_per_poll` (default 10). The
`PollingConsumer` (`combo_mm/consumer.py`) drives any `EventSource` and
dispatches items through the same path as stream items.

### Rate limits (official Retail docs)

- **20 requests/second per API key**, global. The adapter only performs
  reads and stays far under this (default: ≤10 requests per 5 s poll).
- Combo/RFQ *creation* shares a 10-requests-per-10-seconds edge limit —
  the adapter never creates RFQs or submits quotes, so it never touches
  that budget.

### RFQ beta gate (403 handling)

The Retail **RFQ endpoints are beta-gated** — "available only to explicitly
enabled Retail API users." A key without enablement gets **403** on
`/v1/rfqs*`. The adapter treats this as a *runtime capability*, not a
failure:

- On 403 from any RFQ endpoint it logs an actionable error:
  `Retail RFQ beta not enabled for this API key - request access via
  support@polymarket.us or the developer portal`.
- RFQ events **fall back to the simulated feed** — the pipeline keeps
  running; nothing crashes and nothing retry-loops.
- **Mixed mode**: leg market data (book/BBO) is *not* beta-gated, so retail
  BBO refresh continues while RFQ reads 403 (simulated RFQ events + live
  retail books). The simulated feed's own book snapshots are skipped so
  they never clobber live books.
- The capability flag (`RetailPollingSource.rfq_beta_enabled`) is
  **re-checked every poll** — a 403 flips it off, a later successful read
  flips it back on. Enablement is picked up **without a restart**.
- The 403 is never raised to the consumer and never counts against the
  retry/backoff budget.

To get enabled: email **support@polymarket.us** or request access via the
Polymarket developer portal, then just keep the poller running — it will
pick up the RFQ beta on the next successful poll.

### What Retail does *not* give you

- **No private quote events.** Retail exposes no per-user quote stream, so
  the quote lifecycle stays paper/simulated in live mode.
- **Paper-only quoting, always.** Outbound quotes never call a transport;
  the adapter issues reads only. Shadow decisions are recorded, never sent.

### Local live testing (on your machine)

```bash
export POLYMARKET_US_KEY_ID="..."
export POLYMARKET_US_SECRET_KEY="..."
pip install polymarket-us
# run the dashboard / poller with "Retail live" selected
```

Tests are all mocked (no network, no real credentials):

```bash
python3 -m pytest tests/test_retail.py tests/test_polling.py -q
```

This includes: created/updated/closed diffing, missing env vars refusing
startup, fake credential material absent from logs/repr/DB, the request
budget guard, and the beta-gate behavior (403 → actionable message →
simulated-RFQ/retail-book mixed mode → flag recovery without restart).

### Endpoint status (2026-09-16)

`polymarket-us` 0.1.2 exposes **no RFQ resource**; market data
(`client.markets.bbo/book`) is real and used as-is. The RFQ list/detail
paths (`/v1/rfqs*`) are hand-modeled guesses routed through the SDK's
authenticated `get()` — verify against the Retail docs / API team before
treating live RFQ polling as authoritative, and check
`RetailPollingSource.last_error` after polls.

## Going live — Exchange gRPC checklist (later)

The live Exchange adapter (`GrpcTransport`) is an explicit stub. To go live,
Andrew / Totalis must supply:

- **Proto bundle** — the canonical "Polymarket - Proto Files.zip" (do not depend on server
  reflection). TODO: fetch from the Google Drive link in the gRPC docs.
- **Environment** — preprod vs prod (Auth0 domains: `pmx-preprod.us.auth0.com` / `pmx-prod.us.auth0.com`).
- **Credentials** — `client_id`, `audience`, and the firm's RSA private-key path (or secret-store
  reference) for Private Key JWT auth. Tokens expire every 3 minutes; `auth.py` already models
  auto-refresh and key rotation.
- **Enrollment** — confirm combo-RFQ maker access / whitelisting, production rate limits, fee and
  rebate treatment, and approved market-data channels with Polymarket US before sending a live quote.
- **Drop Copy** — `DropCopyAPI` access (scope `read:dropcopy`) with a `resume_token` strategy.

## Open questions

- Exact leg-book market-data feed on Polymarket US (gRPC service vs WS vs REST) — resolve from
  the proto bundle / docs.
- Rate limits on `GetRFQs`/`GetQuotes` (stream-first; durable reads for startup/recovery only).
- Polymarket's exact push/void/draw settlement semantics per leg type (verify per market rules,
  don't assume).
- Whether combo-RFQ maker access requires enrollment/whitelisting.
- Fee/rebate treatment in fair value (maker rebates shift the effective edge — quantify before sizing).

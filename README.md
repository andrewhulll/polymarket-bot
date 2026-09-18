# polymarket-bot

Paper-trading combo RFQ pipeline for Polymarket, built for Totalis (a prediction-markets startup).
Listens for combo RFQs, stores raw messages and normalized records, prices NFL same-game combos
with a joint correlation model (`combo_mm/nfl/`), shadow-quotes without ever submitting, and
replays sessions deterministically. The minimal V1 independent-leg model remains in offline
replay and supplies the naive baseline the correlation model is scored against. Live sources are the
receive-only international quoter gateway and US Retail REST polling; the Exchange gRPC adapter
is stubbed for later.

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

## Status against the Totalis brief

One row per brief step, kept current — if a PR changes a step's status, update this table
(see `.github/pull_request_template.md`). Roadmap: #17.

| Brief step | Status | Where (modules) | Tracking |
|---|---|---|---|
| 1. RFQ listener and storage | Done | `consumer.py`, `store.py`, `normalize.py`, `recovery.py` | #1 |
| 2. Pricing and correlation | NFL live capture and HTML dashboard use the model; offline shadow replay uses guarded V1 | `nfl/live_pricer.py`, `live_quoter.py`, `live_monitor.py`, `pricer.py` | #2 |
| 3. Inventory and risk ($50k capital) | Hard caps only | `risk.py` `ConservativeRiskCheck`; inventory never populated | #3 |
| 4. Shadow quoting | Done | `engine.py`, `eligibility.py` | #4 |
| 5. Backtest | Partial — historical backtest + correlation sensitivity + settlement scoring done; unified runner over the simulated RFQ dataset missing | `nfl/synthetic_backtest.py`, `scripts/nfl_backtest.py`, `docs/settlement-tracking.md` | #5 |
| Live dashboard (five tabs) | Done (closed) | `dashboard/`, `combo_mm/intl_gateway.py` | #11 |
| Always-on supervision | In progress | `combo_mm/capture_process.py`; `scripts/check_heartbeat.py` and `deploy/` planned | #34 |

## Known limitations

- **US Retail RFQ access.** `polymarket-us` 0.1.2 exposes no RFQ resource; the `/v1/rfqs*`
  paths are hand-modeled guesses (see [Endpoint status](#endpoint-status-2026-09-16)) — do not
  treat live Retail RFQ polling as authoritative.
- **Paper only.** Nothing in this repo submits orders or quotes; draft quotes are computed,
  stored, and logged — never sent.
- **Push/tie settlement unverified.** The backtest voids the whole combo when a leg pushes;
  whether the real rule voids the leg or the combo is unconfirmed.
- **Correlation model errors.** Ranked known errors and next steps live in
  `docs/correlation-model.md` §4.4 ("Known model errors").
- **Cold-game pricing latency.** A cold game costs ~1.5 s (two HTTP round trips) against the
  200 ms RFQ window; warm games price in ~2 ms (#2).

## Components (`combo_mm/`)

| Module | Role |
|---|---|
| `events.py` | Canonical event model + RFQ/quote state machines, mirroring the Polymarket US gRPC contract (`polymarket.v1`). Exact wire field names (`qtyDecimal`, `buyPrice`, …). |
| `normalize.py` | Pure validation/coercion of raw messages into `NormalizedEvent`s. Idempotency key = `event_id` when present, else a stable hash of the payload. |
| `store.py` | Append-only SQLite store: raw events first, then normalized projections. Tables: `raw_events`, `rfq`, `rfq_legs`, `quotes`, `fills`, `dropcopy_state`, `books`, `shadow_decisions`, `rfq_screen` (live-feed screen per RFQ). Exposes `state_digest()` (SHA-256 over the canonical read model) for determinism checks. |
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
| `pricer.py` | Offline shadow-engine seam: `Pricer` protocol + `PricerResult`; `V1NaivePricer` uses the independent product with a catalog-backed same-game guardrail. Live NFL RFQs use `NflLivePricer`, whose catalog and market-data lookups do not fit this pure seam. |
| `risk.py` | Risk seam (issue #3): `RiskCheck` protocol + `InventoryState` / `RiskVerdict`; `ConservativeRiskCheck` enforces per-RFQ / per-game / capital hard caps (shrink or reject). The full risk module replaces it behind the same interface. |
| `eligibility.py` | Pure pre-pricing eligibility filter: event type → RFQ present → status terminal → legs present → exchange-time staleness. Skip reasons `SKIP_NO_RFQ` / `SKIP_RFQ_CLOSED` / `SKIP_NO_LEGS` / `SKIP_STALE_RFQ`. |
| `engine.py` | Shadow quoting engine (issue #4): eligibility → pricer → risk → two-sided draft quote, stored in `quotes` (`status='shadow'`, `origin='shadow'`) with a full reproducible input snapshot. Paper-only: no call path to any outbound RPC, `PaperModeError` unless `paper_mode=True`. |
| `paper_backtest.py` | Replay-based paper backtest over fixture/simulated sessions: counts, rates, expected vs realized P&L, swings, exposure over time. No future information (books filtered to `updated_at <= event time`). |
| `fixtures.py` | Scripted sessions: full lifecycle flows, cancelled/expired RFQs, the `rfq_closed` race, duplicate + out-of-order deliveries, a mid-stream disconnect, and a missed `rfq_closed` only recovery can catch. |
| `replay.py` | Deterministic replay harness (virtual clock) producing state digests for byte-for-byte comparison. |
| `combo_markets.py` | `ComboMarketCatalog`: resolves gateway leg position ids to markets (title, outcome, price, tags, game key) from the public `combos-rfq-api` combo-markets catalog; background crawl + JSON cache. |
| `rfq_screen.py` | Pure quotable-RFQ screen: an NFL game with 2+ legs, no other game with 2+ legs, every leg resolved. |
| `quote_selections.py` | `QuoteSelectionStore`: durable SQLite of RFQs picked for quoting and the accepted quote (`RFQ_TRADE`) for picked RFQs only. |
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
# Tests (stdlib only + pytest; no network) — 491 tests
python3 -m pytest tests/ -q

# Demo: scripted session through the consumer, incl. a mid-stream disconnect
python3 scripts/run_pipeline.py

# Dashboard (demo/observability — not production); NFL deps first for the NFL tab
pip install -r requirements-nfl.txt
python3 -m dashboard.server --data-dir data/live --port 8000
```
The dashboard is a stdlib HTTP server plus a static page: the page stays loaded and
polls small JSON endpoints, so live tables update in place with no full refresh, no
lost scroll position, and no rerun of the whole script. It has five tabs — RFQs,
Pricing, Performance, Engine, and NFL correlation (the full research suite: overview,
combo pricing, calibration, correlation structure, sensitivity & P&L, combo explorer,
and params & data, rendered with vendored Vega-Lite). It opens the capture database
read-only; the headless capture process remains the sole writer. The legacy Streamlit
app (`streamlit run dashboard/app.py`) is kept as a one-release fallback.

The dashboard opens with a PAPER/SHADOW banner and two controls at the top:

- **Run backtest — NFL 2026 Week 1** — replays every same-game combo (2–3 legs of ML / spread /
  total, 17 combo types) from the week's 16 games as RFQs through the real pipeline
  (`combo_mm/nfl/week_backtest.py`). Leg books are one cent wide around the de-vigged closing
  prices; params are estimated walk-forward from games before Week 1 with the frozen estimator.
  The shadow engine prices each RFQ with the NFL joint model (`combo_mm/nfl/joint_pricer.py`), a
  naive independent-leg maker quotes the same RFQ, and the requester (about 3 in 4 buy) trades
  with the better price. Trades produce the full quote lifecycle and a fill, then settle on the
  final score; a pushed leg voids the combo. Needs the cached nflverse pull under `data/raw`
  (`python scripts/refresh_params.py --pull`). Deterministic; about 5 seconds.
- **Live monitor RFQ feed** — streams live RFQs through the same store and shadow engine
  (`combo_mm/live_monitor.py`), auto-refreshing the views every `poll_interval_s`; **Stop live
  monitor** closes the connection and keeps the data. The source is the receive-only polymarket.com
  quoter gateway when its `POLYMARKET_*` keys are set (see
  [Live international RFQ feed](#live-international-rfq-feed-quoter-gateway)), else the US Retail
  API when its two env vars are set (see [Retail live data](#retail-live-data)). There is no
  simulated fallback: without keys, the needed package, or RFQ beta access, the dashboard says
  which is missing. Live RFQs are priced by the NFL correlation model: both the dashboard's
  live monitor and the headless `scripts/capture_live_rfqs.py` build `LiveQuoter` around
  `NflLivePricer` (see [Pricing a live RFQ](#pricing-a-live-rfq-the-model-quotes)). Legs the
  catalog cannot resolve still decline (`UNRESOLVED_LEG`).

Views (all read the active run):

1. **RFQs** — the Week 1 historical RFQs or the live feed. Backtest: filterable table (game,
   status) with combo, size, requester side, naive vs model fair, our trade price, result and
   P&L; per-RFQ detail with legs, settlement values, our quote vs the naive maker's, and lifecycle
   events. Live: readable legs, a **Markets** filter defaulting to *Quotable NFL first*, a
   **Quote this RFQ** button, and the durable **Selected to quote** list with accepted quotes (see
   [Picking RFQs to quote](#picking-rfqs-to-quote)).
2. **Pricing & quoting** — every shadow decision: naive product vs model fair, correlation
   adjustment, quoted bid/offer, size, expected edge, and each spread component.
3. **Performance** — RFQs received/quoted/executed, win rate vs the naive maker, expected (model
   edge on trades) vs realized P&L, max downswing/upswing, P&L and exposure over time, and for
   the backtest results by combo family, combo size, requester side and game.
4. **Engine status** — shadow engine health, skip/decline reasons, stored drafts.
5. **NFL correlation** — independent of the controls; see
   [NFL correlation pipeline](#nfl-correlation-pipeline-issue-6).

## NFL correlation pipeline (issue #6)

Offline historical data pipeline for the same-game correlation engine (#2): **history for shape,
market for location**. NFL only, Phase A legs (moneyline / spread / total / team totals). Never in
the RFQ hot path — the pricer reads a weekly params file. Full method and results:
[`docs/correlation-model.md`](docs/correlation-model.md).

```bash
pip install -r requirements-nfl.txt              # numpy / scipy / pandas (offline only)

# 1. Tune estimator settings on the TRAIN seasons (2006-2021) only -> params/estimator.json (~15 min)
python scripts/nfl_tune.py --pull                # --pull fetches nflverse games.csv into data/raw/

# 2. Walk-forward backtest, every same-game combo: train 2006-2021 / test 2022-2025 (~1 min)
python scripts/nfl_backtest.py

# Weekly params refresh (Wednesdays): tuned estimator -> gates -> params/nfl_<season>_w<ww>.json
python scripts/refresh_params.py --pull

# NFL tests (synthetic data, no network)
python -m pytest tests/test_nfl_*.py -q
```

- **Data** — nflverse `games.csv` (scores, OT, closing spread/total/moneyline prices since 2006),
  cached immutably under `data/raw/nflverse_<pulldate>/` with a SHA-256 manifest; validated on
  ingest (duplicates, team twice in a week, implausible scores/lines, missing lines).
- **Estimation** — residuals vs closing-line implied points; recency-weighted trailing window with
  a strict `as_of` cutoff; league constant, `σ²(μ) = a + b·μ`, or shrunk team factors;
  league-wide within-game `ρ`. Settings (variance model, window, half-life, shrinkage) are
  grid-searched on the train seasons only and frozen in `params/estimator.json`
  (currently `league_constant`, 4-season window, 1-season half-life).
- **Train/test split** — chronological 80/20 by games: train 2006–2021, test 2022–2025, scored once
  with the frozen settings. 1999–2005 (no closing prices) are estimation history only. Walk-forward
  in both periods.
- **Params files** — canonical, versioned JSON with data vintage; stdlib-only loader
  (`combo_mm.nfl.params_io`); gates: range sanity, no regression vs the previous file, determinism.
  Offseason freezes the last file. `params/nfl_2026_w02.json` is the current promoted file.
- **Joint engine** (`combo_mm.nfl.joint`) — any same-game leg set as a Gaussian polygon probability
  on the scores, push-conditioned, with the market-implied mean solver.
- **Backtest** (`scripts/nfl_backtest.py`) — all 17 same-game combos (one side of 2-3 of ML /
  spread / total) per game vs naive product vs realized; Brier/log loss with game-clustered SEs,
  calibration, results by combo / family / size / favorite size, correlation structure, sensitivity
  to correlation, stylized edge P&L.

**Headline, out of sample (test 2022–2025, 1,139 games):** the model beats the naive product by
4.52% Brier skill (t −11.9), vs 4.53% on train, so there is no sign of overfitting. The gain comes from
legs sharing the margin (ML × spread: 13.2% test). Spread × total ties naive, because NFL margin/total
dependence is small except for 10+ point favorites. Inflating the modeled correlation degrades scores
gradually in both periods. Next steps (key-number margin shape, moneyline calibration) are in the doc
(§4.4).

The dashboard's **NFL correlation** tab has a Test / Train / All sample selector (default Test) and
seven views: overview (with a train-vs-test table), combo pricing, calibration, correlation structure,
sensitivity & P&L, an interactive same-game combo explorer (any team's ML / spread / total, team or
opponent side, historical or hypothetical game, with the score distribution), and params & data
(tuning grid, frozen estimator, refresh buttons, gate report).

## NFL RFQ datasets (issue #15)

Steps 2, 3 and 5 of the brief all need NFL RFQ flow to replay, and there is none: the scripted
replay fixtures are political markets, and Polymarket RFQ history is not available yet (#11).
Two pieces close that gap. Full method and assumptions:
[`docs/rfq-simulation.md`](docs/rfq-simulation.md).

**Leg-market registry** (`combo_mm/nfl/markets.py`, stdlib only) gives an RFQ leg symbol a
meaning: which game, market type, team and line. It owns the symbol grammar, the mapping to the
canonical score legs the joint model prices (nflverse `spread_line` is the *home* expected margin,
while a spread symbol carries its line in the *subject's* terms), and settlement — including the
push and tie rules, which stay config knobs because Polymarket's exact rules are unverified. An
unknown symbol resolves to `None` rather than a guessed price. A live token resolver (#11) must
produce the same `NflLegMarket`, so everything downstream stays symbol-agnostic.

**Simulated RFQ sessions** (`combo_mm/nfl/rfq_sim.py`) manufacture flow from historical games in
the wire format the pipeline already replays, so `SimulatedTransport`, `EventStore.apply` and the
shadow engine consume it unchanged. Books follow a line path run backwards from the closing line
and are pinned to the de-vigged closing prices; arrivals are a Poisson process rising into
kickoff. Sessions carry **only exogenous events** — books, requests, window closes, settlements —
never acceptances or executions, which depend on our own quote and belong to #5's fill model.
Final scores reach nothing but the post-game settlements, and a test regenerates a dataset with
every score zeroed to prove the books are identical.

```bash
python scripts/gen_nfl_rfq_dataset.py --seasons 2022-2025 --seed 7 \
    --rfqs-per-game 40 --out data/rfq_sim/test_2022_2025
```

That run is ~34s for 1,139 games and ~46k RFQs, deterministic for the seed, with a manifest
pinning the config, the source `games.csv` hash and a SHA-256 per file. Weekly covariance params
come from the walk-forward estimator (games strictly before the week), cached under
`params/history/`. Requester types, closing-line fairs and a competitor quote live in a separate
`sidecar.jsonl.gz` that only #5's fill model may read — a source-scan test fails if any pricing,
risk or engine module so much as mentions it.

`combo_mm/fixtures_nfl.py` is a committed four-game slate of 40 hand-written RFQs, one deliberate
case each (nested and impossible combos, a pushed integer spread, a moneyline tie, an overtime
settlement, a missing main total, an unknown leg, a cross-game combo, a stale book, a cancel, an
expiry, duplicate and out-of-order deliveries). It needs no data pull and no numpy/scipy, so it
runs on a fresh checkout (exercised by `tests/test_fixtures_nfl.py`; the dashboard button was
removed).

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

### Dashboard live monitor

Start the dashboard from the repo root:

```bash
python3 -m dashboard.server --data-dir data/live --port 8000
```

Then open http://127.0.0.1:8000. The page polls the server's JSON endpoints
(RFQs, Pricing, Performance, Engine every few seconds; the NFL correlation tab
loads its research views on demand) and patches the tables in place — nothing
ever full-refreshes, so scroll position, selected rows, filters, and the active
tab survive the feed. It opens `data/live/rfq_capture.db` read-only; closing or
slowing the page does not delay pricing. The legacy Streamlit app
(`streamlit run dashboard/app.py`) remains as a one-release fallback.

When the dashboard starts, it starts the headless capture process if no reader
is running. A process lock prevents duplicate readers, including when multiple
dashboard sessions open. To run capture without the dashboard, use
`python3 scripts/capture_live_rfqs.py --data-dir data/live`.

The capture process owns the quoter-gateway websocket, screening, paper
pricing and SQLite writes. The **Live monitor RFQ feed** control opens a
read-only viewer of `data/live/rfq_capture.db`; closing or slowing the page
does not delay pricing. The dashboard refreshes every 0.75 seconds. Gateway
credentials come from the environment or the gitignored `.env` file. The
runner never submits a quote.

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
# dashboard: python3 -m dashboard.server --data-dir data/live (without the gateway keys set); or drive the source directly:
python3 -c "
from combo_mm import EventStore, PollingConsumer, RetailPollingSource
store = EventStore('retail.db')
PollingConsumer(RetailPollingSource(), store).poll_once()
print(store.get_rfq_stats(), store.get_book_stats())
"
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

## Live international RFQ feed (quoter gateway)

The dashboard (and pipeline) can read the **live international RFQ stream**
on polymarket.com instead of the simulated replay. RFQs arrive over the
quoter-gateway websocket (`wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq`);
the adapter (`combo_mm/intl_gateway.py::InternationalQuoterGatewayAdapter`,
an `EventSource`) authenticates, reads the `RFQ_REQUEST` / `RFQ_TRADE`
broadcast feed, and maps frames onto the pipeline's normalized RFQ lifecycle
events (`rfq_created` / `rfq_closed`). Reconnect uses exponential backoff
with jitter; `websockets` ping/pong is the heartbeat.

**Receive-only, by construction.** The adapter has no code path that sends
quotes, orders, or any trading message — there is no quote-submission client
anywhere in the module, and `tests/test_intl_gateway.py` asserts that
structurally. The 400 ms quote window is irrelevant to us: we only watch.

### Installation

Core pipeline: Python 3.12, stdlib only, plus `pytest` for tests.

The live gateway feed needs the websocket client (optional dependency —
the simulated pipeline and all non-gateway tests run without it):

```bash
pip install websockets
```

### Credentials

From the **runtime environment only**:

```bash
export POLYMARKET_API_KEY="..."
export POLYMARKET_SECRET="..."
export POLYMARKET_PASSPHRASE="..."
export POLYMARKET_ADDRESS="0x..."
```

Create these on polymarket.com (profile → Settings → API keys; requires a
wallet signature). A gitignored local `.env` file in the repo root is also
accepted and only fills gaps — real environment variables always win.

Rules (enforced by tests):

- All four values are read at adapter construction and never leave memory.
- They are never logged (failure paths log the exception *class* only),
  persisted, displayed, or committed. `GatewayCredentials.__repr__` redacts.
- **Never commit secrets.** `.env` and `polymarket.keys*` are gitignored.
  The no-trading-code test (`test_no_trading_code_paths`) scans the adapter
  for trading wire tokens (`RFQ_QUOTE`, `signed_order`, `maker/quotes`) and
  trading identifiers, so a quote-submission path cannot be added silently.
- No private key is needed — gateway auth uses only the API triple plus the
  wallet address for the identity field.

### Dashboard

Start `scripts/capture_live_rfqs.py` and press **Live monitor RFQ feed** in the
dashboard. The RFQs, Pricing & quoting, Performance and Engine status views
read its SQLite database. **Stop live monitor** stops this page's refresh; the
headless engine continues until stopped in its own shell.

### Leg markets (combo catalog)

`RFQ_REQUEST` names legs only by on-chain position id. The public catalog
`GET https://combos-rfq-api.polymarket.com/v1/rfq/combo-markets?limit=100&cursor=…`
(no auth; it returns 403 without a browser/curl-like `User-Agent`) lists every
combo-able market with `position_ids` / `outcomes` / `outcome_prices` aligned by
index (`[0]` YES, `[1]` NO), plus `slug`, `title` and `tags`.
`ComboMarketCatalog` crawls it in a background thread (merging page by page,
tens of thousands of markets, several minutes on the first run), keeps entries
for markets that later close, and caches the index to
`data/live/combo_markets.json` (checkpointed every 200 pages) so restarts
resolve legs immediately. Sports game markets are tagged `games`; the slug up
to its date (`nfl-sea-ari-2026-09-20`) is the game key.

### Picking RFQs to quote

Each live RFQ is screened on arrival (`combo_mm/rfq_screen.py`, stored in
`rfq_screen`) and re-screened as the catalog resolves more legs:

| Screen | Meaning |
|---|---|
| `QUOTABLE` | Some NFL game has 2+ legs (the correlated block the NFL model prices) and no other game — any sport — has 2+ legs. Other legs (single legs from other games, non-game markets) are independent and multiply in. |
| `OTHER_SAME_GAME` | Another game (e.g. soccer) has 2+ legs: no correlation model for it yet. |
| `UNRESOLVED` | A leg is not in the catalog yet (could hide a second same-game leg). |
| `NO_NFL_SAME_GAME` | No NFL game with 2+ legs. |

The RFQs tab defaults to **Quotable NFL first** (then RFQs with any NFL leg,
then the rest, newest first within each); **Quotable NFL only** and
**All, newest first** are one click away. **Quote this RFQ** adds the RFQ to
`data/live/quote_selections.db` with a snapshot (direction, size, deadline,
resolved legs). When a picked RFQ trades, its `RFQ_TRADE` broadcast — the
accepted blended price, matched size and execution time — is stored as the
accepted quote; trades for RFQs not picked are never stored. Picking an RFQ
after it already traded still records it (recent trades are kept in memory).
This records intent only: nothing is sent to the gateway.

### Mapping notes

- `requested_size.unit == "notional"` → `cashOrderQty`; `"shares"` →
  `qtyDecimal` (exactly one set, per the normalize XOR rule).
- Legs carry on-chain **position ids** as `symbol` and inherit the
  combo-level YES/NO `side` — the gateway provides no per-leg market symbol
  or side. The RFQ `symbol` is the combo `condition_id`.
- `RFQ_TRADE` (confirmed trade broadcast) → `rfq_closed`: "stop quoting". The
  accepted `price_e6` / `size_e6` / `executed_at` ride along as raw `price` /
  `size` / `executed_at` extras for the accepted-quote record.

## Historical RFQ data (NFL capture)

**Polymarket has no endpoint for past RFQs.** Checked directly against the current docs
(docs.polymarket.com: `trading/combos/requesters`, `trading/combos/market-makers`, the
`combos-rfq-openapi.yaml` spec) and confirmed by this repo's own transport notes
(`combo_mm/stream.py`, `combo_mm/retail.py`): RFQs only ever appear live, once, as they happen.

- The quoter-gateway websocket is a broadcast feed, not a queryable log — "a new stream
  delivers only NEW events" (`combo_mm/stream.py`), and the official docs tell market makers to
  "maintain your own logs" if they need history.
- The Retail REST `/v1/rfqs` list (`combo_mm/retail.py`) returns current-state RFQs, is
  beta-gated, and has no date-range parameter.
- The Exchange gRPC `GetRFQs` durable read is a reconciliation snapshot ("what's open right
  now"), not an archive.
- A newer, undocumented client (`py-clob-client-v2`) exposes `GET /rfq/data/requests` with a
  `state: active|inactive` filter that looks like it *might* cover closed RFQs — but it needs
  live L2 API credentials to test, isn't in the official docs, and the client's own README
  points new integrations at a different, still-newer unified SDK instead. Untested; flagging it
  here in case it's worth trying with real keys, not relying on it.

So there was no way to backfill NFL Week 1 (2026) after the fact — nothing was capturing the
live feed while it happened. The Week 1 RFQs the dashboard's "Run backtest" button replays
(`combo_mm/nfl/week_backtest.py`) are **synthetic**: real closing lines, reconstructed RFQ
arrivals — not actual RFQ negotiations pulled from Polymarket.

**Going forward**, `scripts/capture_live_rfqs.py` listens on the receive-only gateway
and saves every RFQ and paper pricing decision to disk:

```bash
export POLYMARKET_API_KEY=... POLYMARKET_SECRET=... POLYMARKET_PASSPHRASE=... POLYMARKET_ADDRESS=...
python3 scripts/capture_live_rfqs.py --data-dir data/live   # run continuously; Ctrl+C to stop
```

It writes `data/live/rfq_raw.jsonl` for archival export and
`data/live/rfq_capture.db` for the dashboard. The database includes RFQs,
screening checks, draft quotes, decline reasons, accepted trade prices,
engine health and separate wait/compute latency samples.

Once some data has accumulated, pull out just the NFL rows:

```bash
python3 scripts/export_nfl_rfqs.py --data-dir data/live --out data/live/nfl_rfqs
# -> data/live/nfl_rfqs.csv and .json; --since/--until filter by created_time
```

## Pricing a live RFQ (the model quotes)

Every RFQ the screen calls `QUOTABLE` is queued for the NFL correlation model
as it arrives. The bid and ask we would show, or a decline reason, are stored
in `data/live/rfq_capture.db` and summarized in the application log. Realized
paper P&L becomes available once leg settlements are recorded; settlement
ingestion is described in
[`docs/settlement-tracking.md`](docs/settlement-tracking.md).

```
QUOTE rfq=0x8f2… BUY YES 25 shares | bid 0.480 / ask 0.576
      (fair 0.5284, naive 0.3681, corr +1602 bps, confidence 0.89) | NOT SENT (paper)
```

How one RFQ is priced (`combo_mm/nfl/live_pricer.py`):

1. **Resolve the legs** — position id → catalog market →
   `combo_mm/nfl/catalog_markets.py` parses the slug into the leg registry's
   `NflLegMarket` (`combo_mm/nfl/markets.py`, #15) and from there the canonical score leg (`nfl-det-buf-2026-09-18-spread-home-4pt5`
   outcome 0 → "home margin > 4.5"). Full-game moneyline, spread, total and team
   totals only; a first-half line or a player prop on the same game is declined
   (`UNSUPPORTED_LEG`), never guessed at.
2. **Leg books** — the gateway sends no prices and the CLOB does not know combo
   position ids, so `combo_mm/leg_books.py` maps the catalog market id through
   Gamma (`/markets?id=…`, giving `clobTokenIds` and `gameStartTime`) to the
   CLOB's own book (`POST /books`, top of book with sizes), falling back to
   Gamma's `bestBid`/`bestAsk`. Books are cached for 3 s, metadata for 10 min.
3. **Locate the distribution** — the game's main spread and total (nearest
   50/50, whether or not they are RFQ legs) pin `(μ_home, μ_away)` via
   `calibrate_means`; the covariance shape comes from the current weekly params
   file (`ParamsProvider`, version recorded as `nfl_2026_w02.json@<sha12>`).
4. **Fair value** — `fair = Π q_i × P_model(all legs) / Π P_model(leg_i)`
   ("market lift"): each leg keeps its own market price and only the
   *dependence* comes from the model, which avoids importing the model's
   moneyline marginal error (`docs/correlation-model.md` §4.4 #2). The result is
   clamped into the Fréchet bounds. `model_joint` (the model's own joint) is
   computed and logged alongside for comparison. Legs from other games multiply
   in as independent.
5. **Bid/ask** — the V1 spread stack (`price_combo`) plus model-risk add-ons:
   a haircut proportional to how far the model moved off naive, a
   model-vs-market marginal gap term, key-number (3/7) and big-favourite
   charges, a per-extra-leg tail charge, and a stale-params charge. A requester
   who wants to BUY trades against our ask, one who wants to SELL against our
   bid.

Declines are logged with the same detail as quotes: `UNRESOLVED_LEG`,
`UNSUPPORTED_LEG`, `OTHER_SAME_GAME`, `NO_NFL_SAME_GAME`, `GAME_STARTED`,
`MISSING_CALIBRATION_MARKET`, `CONTRADICTORY_LEGS` (e.g. "Lions win *and* Bills
cover −4.5"), `MODEL_MARKET_DISAGREE`, `LOW_CONFIDENCE`, `PARAMS_STALE`,
`PARAMS_UNAVAILABLE`, `QUOTE_DEADLINE_EXCEEDED`, `QUOTE_LATENCY_EXCEEDED`,
plus the V1 book checks (`MISSING_LEG`, `STALE_LEG`).

Pricing runs on its own thread (`combo_mm/live_quoter.py`) with a bounded
queue, so the ~200 RFQ/s feed is never blocked by a book fetch. The HTML
dashboard reads model decisions and model-versioned shadow drafts written by
the headless capture process. The legacy live monitor uses the same model
result when a `LiveQuoter` is present; it does not run V1 on that RFQ as well.
Offline replay still uses `ShadowQuotingEngine` and guarded V1 because it has
no live catalog or market-data source. Its catalog guardrail widens same-game
ML/total and spread/total quotes and declines nested ML/spread pairs.

`scripts/bench_pricer.py` measures the network-free ten-leg joint calculation:
on the development machine, 1,000 samples gave cold-model p99 0.811 ms and
warm-model p99 0.056 ms. This excludes the Gamma and CLOB requests. A cold
game can take about 1.5 s; quotes finishing after the RFQ deadline are
declined as `QUOTE_DEADLINE_EXCEEDED`, and quotes over the configured
`quote_latency_budget_ms` are declined as `QUOTE_LATENCY_EXCEEDED`.
End-to-end wait and compute latency is
recorded in `quote_latency` for live capture. **Paper only**: there is no code path from the pricer to a quote
submission, and `tests/test_live_quoter.py` asserts that structurally.

The dashboard's **Pricing & quoting** tab lists these quotes (naive vs model
fair, correlation adjustment in bps, our bid/ask, confidence, decline reason)
and, per quote, the calibrated means, each leg's market price vs model
probability, and every spread component.

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
  don't assume). The NFL registry keeps these as config knobs — `push_rule` for a spread or total
  landing on an integer line, `tie_rule` for a moneyline tie — defaulting to voiding the leg,
  which voids the combo. See [`docs/rfq-simulation.md`](docs/rfq-simulation.md).
- Whether Polymarket's listed NFL spreads and totals are always half-points. The simulated
  datasets assume so by default (`force_half_point_lines`), which makes pushes rare but moves the
  key numbers 3 and 7 by half a point.
- Whether combo-RFQ maker access requires enrollment/whitelisting.
- Fee/rebate treatment in fair value (maker rebates shift the effective edge — quantify before sizing).

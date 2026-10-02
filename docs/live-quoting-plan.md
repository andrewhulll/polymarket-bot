# Live quoting — implementation plan

Status: **draft for approval** · Branch: `live-quoting` (off `main` @ PR #67) · Date: 2026-10-01

This plan turns the paper bot into a market maker that sends firm quotes to the
Polymarket combo RFQ gateway. Nothing here is implemented yet. Approval of this
plan authorizes **building and testing offline (phases 0–5)**. Phases that touch
real funds or real quotes (6–8) need a separate, explicit go-ahead from the
owner at each gate (see §8).

## 1. Goal and non-goals

**Goal.** For each eligible NFL same-game RFQ, price it with the existing model,
size it under hard risk limits, sign and send a quote inside the submission
window, handle last look, track execution, and reconcile real positions —
with paper mode still available and live sending off by default.

**Non-goals.** New pricing models. Non-NFL markets. Requester-side RFQs.
Kairos (not needed: Polymarket's own gateway has a documented maker path).
Changing `main`'s paper-only guarantee.

## 2. What the docs establish (and what they don't)

Source: Polymarket docs — Quoter Gateway (AsyncAPI), Submit/Cancel/Confirm
(OpenAPI), "Market Makers" guide.

| Fact | Detail |
|---|---|
| Channel | `wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq`; `auth` must be the first message, within 30 s; server pings every 30 s (payload `rfq`) and the client **must pong the same payload** |
| Inbound | `RFQ_REQUEST`, `ACK_RFQ_QUOTE`, `ACK_RFQ_QUOTE_CANCEL`, `RFQ_CONFIRMATION_REQUEST`, `ACK_RFQ_CONFIRMATION_RESPONSE`, `RFQ_EXECUTION_UPDATE` (MATCHED/MINED/RETRYING/CONFIRMED/FAILED), `RFQ_TRADE`, `RFQ_ERROR` |
| Outbound | `RFQ_QUOTE` (price_e6, size_e6, signed Exchange v3 order), `RFQ_QUOTE_CANCEL`, `RFQ_CONFIRMATION_RESPONSE` (CONFIRM/DECLINE) |
| REST twin | `POST /v1/maker/quotes`, `/v1/maker/quotes/cancel`, `/v1/maker/confirmations` (HMAC L2 headers). WebSocket is the primary path; REST is a fallback |
| Window | Guide: respond within **400 ms**. Measured on real RFQs: `submission_deadline − created` ≈ **1.05 s** (p5 756 ms, p95 1,116 ms). Treat 400 ms as the target, the deadline as the hard cutoff |
| Revisions | None. Cancel and resubmit |
| Last look | Makers with ~$2,500 combo volume get `RFQ_CONFIRMATION_REQUEST` and must answer by `confirm_by`. Declining >15 % of selected quotes over 1 h can pause quoting |
| Funding | pUSD in the maker wallet; approvals for Exchange v3 / Router (pUSD) and PositionManager (operator), set via a signed Deposit Wallet batch to the Relayer |
| Environment | Polygon mainnet only. **No sandbox documented** |
| Public reads | Data API `/v2/positions/combos`, `/v2/activity/combos` (no auth) for position/fill reconciliation |

**Unknowns to resolve with Polymarket before phase 6** (each blocks go-live,
none blocks offline build):

1. **Account type.** Memory: the account is Google sign-in, `POLY_PROXY`
   (signature_type 1). The guide describes deposit wallets (type 3) for the
   Relayer approval batch. Is type 1 supported for quoting, and how do we get a
   signing key for a Google-sign-in account?
2. **Maker approval.** Is a CLOB key alone enough to quote, or is allow-listing
   required? What do `MAKER_QUOTE_LIMITED` and `QUOTED_PRICE_ABOVE_SAFETY_THRESHOLD` mean
   numerically?
3. **Connections.** May a second connection with the same identity exist (the
   capture's receive socket vs a sending socket)? Default plan assumes **one
   socket that does both**.
4. **Before $2,500 volume**, are selected quotes auto-confirmed (no last look)?
   Then every selected quote is a binding fill.
5. Per-RFQ quote limits (`MAKER_ALREADY_RESPONDED`), min size, rate limits.
6. Fees and how pUSD is reserved (`PRE_EXECUTION_BALANCE_RESERVATION_FAILED`).

## 3. Safety design (applies to every phase)

- **Default off, three independent arms.** A real send requires *all* of:
  (a) env `LIVE_QUOTING=1`, (b) CLI `--live` on the capture, (c) an **arm file**
  `data/live/ARMED` containing a session token that the operator writes and that
  expires. Removing the file, tripping the kill switch, or any breach disarms
  instantly. Missing any one → the same code path runs in **shadow** (builds and
  logs the exact payload, never opens a send).
- **Key handling.** The signing key is read at runtime from an env var / OS
  keystore into memory only. Never in the repo, `.env` examples, logs, DB, or
  dashboard. Signing lives behind a `Signer` interface; tests use a fake signer.
- **Hard caps independent of the model.** Per-quote notional, per-RFQ, per-game,
  portfolio, daily loss, max open quotes, max quotes/min, and a price band vs the
  leg-implied price. Caps are enforced in the sender, after risk, so a model bug
  cannot exceed them.
- **Fail closed.** Unknown `RFQ_ERROR` code, stale books, feed silence,
  heartbeat loss, reconcile mismatch, or an uncaught exception → cancel all open
  quotes and disarm.
- **Separate runtime.** Live runs from its own data dir (`data/live-prod/`), its
  own DB and lock, and its own log; paper keeps `data/live/`. No shared writer.
- **Tripwires.** Keep `test_no_trading_code_paths` on `intl_gateway.py`. Add a
  second tripwire: wire tokens like `RFQ_QUOTE` / `signed_order` may appear
  **only** under `combo_mm/quoter_live/`, and nothing outside it may import the
  sender without going through the arm check.
- **Branches.** `main` stays paper-only. All live code is on `live-quoting`,
  additive (new package + an opt-in hook in the capture script). Merge `main`
  into `live-quoting` regularly; never merge live code back into `main`.

## 4. Target architecture

```text
Gateway WS (one socket) ──▶ LiveQuoterGateway (combo_mm/quoter_live/gateway.py)
   RFQ_REQUEST ───────────────▶ existing: screen → NflLivePricer → paper risk
                                         │
                                         ▼  LiveQuote (bid/ask/qty, response side)
                              QuotePlanner  (sizing, price band, caps, window check)
                                         │
                                         ▼
                              OrderBuilder + Signer  (Exchange v3 order, EIP-712)
                                         │
                       ┌─────────────────┴──────────────────┐
                  shadow: log payload              armed: RFQ_QUOTE ──▶ WS
                                                         │
   ACK_RFQ_QUOTE / RFQ_ERROR ◀───────────────────────────┤  QuoteBook (state machine,
   RFQ_CONFIRMATION_REQUEST ─▶ LastLookPolicy ─▶ RESPONSE │  SQLite-backed)
   RFQ_EXECUTION_UPDATE ─────▶ FillTracker ─▶ LiveInventory ─▶ risk + reconcile
   RFQ_TRADE ────────────────▶ existing accepted-price feed
```

New package `combo_mm/quoter_live/`:

| File | Responsibility |
|---|---|
| `messages.py` | Typed frames for every inbound/outbound message; strict parsing; unknown types logged |
| `orders.py` | `Quote → ExchangeV3Order` math (BUY/SELL, YES/NO token, ceil rounding), `valid_until`, salt, e6 conversions. Pure functions |
| `signer.py` | `Signer` protocol; `SdkSigner` (official `polymarket-client` / EIP-712), `FakeSigner` for tests |
| `gateway.py` | Reuses `map_rfq_request`/`map_rfq_trade`; connect, auth, ping/pong, backoff, send queue, per-message ack futures, kill on silence |
| `planner.py` | Turns a `LiveQuote` into at most one quote per RFQ; enforces window (`deadline − now − send budget`), caps, price band |
| `quote_book.py` | Persisted quote state machine and idempotency; cancel-all; restart recovery |
| `lastlook.py` | Decision policy for `RFQ_CONFIRMATION_REQUEST` |
| `fills.py` | Execution updates → positions; reconciliation against Data API |
| `arming.py` | The three-arm check and the kill switch |
| `sim/` | Fake gateway server (WS) that speaks the real protocol, injects errors, selects quotes, drives last look |

## 5. Phases

Each phase ends with passing tests, a short doc update, and a PR into
`live-quoting`. Size: S ≈ a day, M ≈ several days, L ≈ a week+.

### Phase 0 — Prerequisites and decisions (owner + me) · S
- Owner contacts Polymarket on unknowns §2.1–2.6 and records answers in this doc.
- Decide: signing approach (SDK vs own EIP-712), where the key lives, and which
  machine runs live (a VPS in the gateway's region is recommended for latency).
- Add `polymarket-client` (or chosen lib) to a *separate* `requirements-live.txt`
  so `main`'s install is unchanged.
- Create `data/live-prod/` convention, `ARMED` file spec, branch protection on
  `live-quoting`.
- **Exit:** unknowns answered or explicitly accepted as risks; deps pinned.

### Phase 1 — Fit the window (latency) · M
Today: median 173 ms but ~50 % of quotes exceed 400 ms; p95 ≈ 1.6 s from cold
leg-book fetches. Live needs p95 comfortably under the window *including*
signing and network.
- Prefetch and cache `LiveLegBooks` for every leg of every live NFL game
  (CLOB market data via WebSocket subscription if available, else short-TTL
  polling), so `fetch_ms` is a dict lookup on the hot path; fall back to a
  bounded fetch with a hard timeout (decline rather than blow the window).
- Measure from **gateway receipt** (stamp in the socket thread, not batch start)
  to "ready to send"; add `window_margin_ms = deadline − ready` to
  `quote_latency`.
- Skip RFQs that cannot finish in time (predicted from book freshness).
- **Exit:** over a full game-day of live paper traffic, ≥ 95 % of quotable RFQs
  reach "ready" with ≥ 200 ms of margin; dashboard shows the margin histogram.

### Phase 2 — Order construction and signing (shadow output) · M
- Implement `orders.py` from the guide's math; golden tests against SDK-produced
  orders and hand-worked examples (BUY and SELL RFQs, partial size, rounding).
- Implement `Signer` + `FakeSigner`; verify the SDK signature recovers to the
  signer address in tests.
- `QuotePlanner` shadow mode: for every paper `QUOTED` RFQ, build and store the
  exact `RFQ_QUOTE` payload (signature redacted) next to the paper quote.
- **Exit:** 100 % of paper quotes have a well-formed payload that passes local
  validation (price positive, in band, size covers quote, maker/signer match
  auth), at < 5 ms build+sign.

### Phase 3 — Live gateway client + simulator · L
- `sim/` fake gateway implementing the AsyncAPI protocol: auth, request
  broadcast, quote acks, `RFQ_ERROR` injection (every code in the spec),
  selection, confirmation requests with `confirm_by`, execution updates, trades.
- `gateway.py`: single-socket client built on the existing adapter's backoff and
  watchdog patterns, plus ping/pong, send queue with per-message ack timeouts,
  and reconnect that **cancels or reconciles** quotes outstanding at the drop.
- Wire it into the capture behind the arm check; shadow by default.
- **Exit:** end-to-end tests against the simulator for happy path, every error
  code, disconnect mid-quote, duplicate frames, and slow acks; no real network.

### Phase 4 — Quote state machine and persistence · M
- `quote_book.py`: states `PLANNED → SENT → ACKED → SELECTED → CONFIRMED |
  DECLINED | CANCELED | EXPIRED | FILLED | FAILED`, persisted (use the existing
  `quotes.origin='live'` support or a new `live_quotes` table), idempotent on
  restart.
- Cancel-all on shutdown, disarm, kill switch, and gateway reconnect.
- One quote per RFQ per side unless the docs say otherwise (unknown §2.5).
- **Exit:** crash-and-restart tests (kill mid-flight) never leave an untracked
  quote; cancel-all verified against the simulator.

### Phase 5 — Last look and fills · M
- `LastLookPolicy`: on `RFQ_CONFIRMATION_REQUEST`, re-price with current books
  (the cached books from phase 1) and the model; CONFIRM only if the executed
  price is still inside tolerance of fair, caps still hold, and `now + margin <
  confirm_by`; otherwise DECLINE. Track the rolling 1 h decline rate and **alert
  at 10 %** (limit is 15 %); if it trips, widen/size down instead of declining.
- `fills.py`: map `RFQ_EXECUTION_UPDATE` to positions; only `CONFIRMED` is a
  fill; `FAILED` releases reservations. Replace the paper "assume every winning
  quote fills" model for the live path with real fills.
- **Exit:** simulator scenarios for confirm, decline, timeout, execution
  FAILED/RETRYING; inventory matches expected positions exactly.

### Phase 6 — Live risk, capital, reconciliation (owner gate required) · M
- Real capital: read pUSD balance and allowances; size against *available*
  collateral, not a notional constant; handle `BALANCE_/ALLOWANCE_VALIDATION_FAILED`.
- Live limits (initial proposal, owner to confirm): per-quote ≤ $25, per-game ≤
  $100, portfolio ≤ $250, daily loss stop ≤ $50, ≤ 5 open quotes, ≤ 10 quotes/min.
- Reconcile every N minutes against Data API `/v2/positions/combos` and
  `/v2/activity/combos`; any mismatch → disarm and alert.
- Automatic halts: loss limit, stale books, heartbeat loss, error-rate spike,
  reconcile mismatch, decline-rate near 15 %.
- **Exit:** halts each proven in the simulator; reconcile test with injected drift.

### Phase 7 — Dashboard, alerts, runbook · M
- Dashboard: SEND MODE banner (SHADOW / ARMED), live quote table with state,
  window margin, last-look outcomes, real positions vs reconcile, big
  disarm/kill button (writes the kill switch; arm requires the file).
- Alerts (push/email/log) for disarm, halt, reconcile mismatch, decline rate.
- Runbook `docs/live-quoting-runbook.md`: arm/disarm, rotate keys, what to do on
  each alert, emergency "cancel everything" procedure.
- **Exit:** a dry-run of the runbook against the simulator by someone who didn't
  write it.

### Phase 8 — Canary and ramp (owner gate at every step) · L (calendar time)
1. **Shadow soak** on the live gateway for ≥ 3 game days: payloads built, none
   sent; verify margin and payload validity.
2. **Funded no-send check:** wallet approvals done, balances read correctly,
   auth accepted on the live socket (still shadow).
3. **Canary:** armed for one RFQ type (2-leg same-game ML×total), $1–$5 quotes,
   max 1 open quote, owner watching, 30–60 minute sessions.
4. **Review gate:** compare real fills vs model fair, last-look decline rate,
   window misses, P&L, reconcile clean. Proceed only if all green.
5. **Ramp** limits by steps (e.g. 2×) with a review at each; broaden RFQ types
   last. Any halt resets to the previous step.

## 6. Test strategy
- Unit: order math, rounding, e6 conversion, state machine transitions,
  policy decisions, caps. Property tests on rounding and signing recovery.
- Protocol: the simulator replays recorded real frames (we already store raw
  RFQ frames) and the AsyncAPI examples; every `RFQ_ERROR` code exercised.
- Integration: capture → planner → simulator end to end; restart/chaos tests.
- Safety: tests that assert nothing sends unless all three arms are present;
  tripwires for wire tokens outside `quoter_live/`; no key material in logs
  (log-scrub test).
- No test, CI job, or agent session ever uses the real key or real gateway
  sends. Real-network steps are manual, owner-run, and logged.

## 7. Branch and PR strategy
- Work lands as small PRs into `live-quoting` (one per phase or sub-phase).
- `main` → `live-quoting` merges keep shared fixes flowing; conflicts stay small
  because live code is a new package plus one opt-in hook in
  `scripts/capture_live_rfqs.py`.
- Shared improvements (e.g. phase 1 latency, gateway-receipt stamping) go to
  `main` first via their own PRs, since they benefit paper too.

## 8. Approval gates
| Gate | Who | Before |
|---|---|---|
| G0 | Owner approves this plan | Starting phase 0 |
| G1 | Owner answers/accepts §2 unknowns | Phase 6 |
| G2 | Owner supplies key, funds the wallet, completes approvals | Phase 8 step 2 |
| G3 | Owner reviews shadow-soak results | Phase 8 step 3 (first real quote) |
| G4 | Owner reviews canary results | Each ramp step |

I will not sign, send, move funds, or read key material at any point; those
steps are the owner's.

## 9. Success metrics (go/no-go)
- Window: ≥ 95 % of eligible RFQs ready with ≥ 200 ms margin; < 1 %
  `SUBMISSION_WINDOW_CLOSED`.
- Last look: decline rate < 10 %; no pause events.
- Reconcile: zero unexplained position drift.
- Economics (measured, not assumed): realized edge vs model fair over ≥ N
  fills; the correlation model has shown little edge over the naive price on the
  live families, so **canary results decide whether to scale at all**.
- Reliability: zero untracked quotes after any restart or disconnect.

## 10. Risks
- **Adverse selection.** Only 12 accepted trades exist to validate pricing;
  live fills will be the ones other makers declined to take. Mitigation: tiny
  canary, last-look re-pricing, tight caps, measure before scaling.
- **Window.** 1.05 s total, 400 ms recommended; the latency tail is today's
  main technical risk (phase 1).
- **Account/key uncertainty** (§2.1) could block go-live entirely.
- **Mainnet-only testing.** Mitigated by the simulator and shadow soak.
- **Two-branch drift.** Mitigated by additive design and regular merges.

## 11. Decisions requested at approval
1. Approve phases 0–5 (offline build) as scoped?
2. Confirm the proposed initial live limits in §5 phase 6, or give your own.
3. Where will live run (local laptop vs a VPS near the gateway)?
4. SDK signing (`polymarket-client`) acceptable as a dependency of the live
   branch only?

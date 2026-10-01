# Terminal dashboard redesign — implementation plan

Design source: the "Terminal Dashboard Designs" canvas (Quad theme, v2 five-tab structure).
Scope: `dashboard/` only. No change to the capture process, the engine, or any database schema.
Paper-only; the only write routes stay `POST /api/risk/kill-switch` and `POST /api/settlements/run`.

## 1. Goals

1. Re-skin the live dashboard as a dense black / white-rule / orange-accent terminal (the "Quad" look).
2. Restructure six tabs into five that each answer one question:

| # | Tab | Question | Replaces |
|---|-----|----------|----------|
| 1 | **Flow** | What is coming in and what did we do with it? | RFQs + Pricing decisions |
| 2 | **Performance** | How are we doing? (P&L, Brier, settlement) | Performance + Settlement check |
| 3 | **Risk** | Can we lose too much? | Inventory + Risk events |
| 4 | **Engine** | Is the system healthy? | Engine (minus risk events, minus correlation lift) |
| 5 | **Research** | Does the model work? (backtest, not live) | NFL correlation + correlation lift |

3. Cross-tab behaviours: persistent kill switch in the header, a global game filter chip, a global KPI strip (4 tiles) plus tab-specific tiles, and a docked detail drawer.

## 2. Non-goals

* No build step, no framework. Stay vanilla JS + hand-written CSS, served by the stdlib server.
* No new Python dependencies, no schema migrations, no write access to the capture DB beyond the existing kill-switch insert.
* Light/amber themes from the exploration are not shipped (CSS tokens make them a later one-file change).
* The game × market heat map in the mockup is dropped: the backend only has exposure by game, by market and by team separately. Market and team bars replace it.

## 3. Current state (what exists)

* `dashboard/static/index.html`: header + six `<section class="tab">` blocks + overlay drawer.
* `dashboard/static/js/`: `core.js` (state, polling, tabs, drawer, SVG chart helpers), one file per tab.
* `dashboard/server.py`: `STATIC_FILES` whitelist (new files must be listed), JSON API, `PAGE_SIZE = 500`.
* `dashboard/live_view_models.py`: `rfqs()` already LEFT JOINs `priced_quotes`; `pricing()`, `fills()`, `performance()`, `inventory_state()`, `engine_status()`, `risk_feed()`, `exposure()`.
* `tests/test_dashboard_server.py` asserts several DOM ids and the `inventory.js` file.

## 4. Backend changes (small, additive)

| ID | Change | File | Why |
|----|--------|------|-----|
| B1 | `rfqs()` returns pricing columns (`decision`, `reason_code`, `response_price`, `response_action`, `fair`, `naive`, `after_deadline`) and `edge_vs_market` | `live_view_models.py` | One merged Flow row per RFQ without a second request. The join already exists. |
| B2 | New `flow_summary(conn, game)` → funnel counts (last hour), arrivals per minute (60 buckets), edge histogram | `live_view_models.py` | Flow funnel/arrivals/edge panels and the Engine feed-health panel. Bounded scan (newest N rows) so it stays cheap on a live capture. |
| B3 | New `GET /api/flow/summary?game=` | `server.py` | Exposes B2. |
| B4 | `fills()` / `fills_count()` accept `game`; `GET /api/fills?game=` | `live_view_models.py`, `server.py` | Global game filter on the Performance table. |
| B5 | New `GET /api/summary` → the four global KPIs (quoted 1h, accept rate, realized P&L, WCL vs equity), cached 10 s server-side | `server.py` | Global KPI strip is on every tab; `performance()`/`inventory_state()` replay the ledger and are too heavy to run per tab per poll. |
| B6 | Whitelist new static files in `STATIC_FILES` | `server.py` | Server only serves listed files. |

Decisions worth recording:

* Flow "Q edge" keeps its existing meaning (our price vs the accepted trade price). It is blank when no trade was observed. The mockup showed our − fair; the real data does not support that for most rows.
* Under a game filter the funnel's Received/Quotable stages can only count priced RFQs, because an RFQ's game is derived from its pricing detail. The panel says so.
* Paper-capital rejections (`RISK_CAPITAL`) are not replayed per Flow row (too heavy at 500 rows / 5 s). Flow shows the stored decision; the drawer (which calls `pricing()`) shows the capital-adjusted one.

## 5. Frontend architecture

### 5.1 Files

| New / changed | Notes |
|---------------|-------|
| `style.css` | Rewritten: tokens, header, KPI strips, panel grid, tables, drawer, popover, charts, responsive fallback. |
| `index.html` | Rewritten: header (tabs, game chip, kill switch), KPI strip, five tab sections as panel grids, docked drawer. |
| `js/core.js` | Updated: five tabs, global state (`game`), header kill switch + popover, global KPIs, docked drawer, chart helpers rewritten for panel-filling SVG, `barList`, `sparkline`. |
| `js/flow.js` | New (from `rfqs.js` + pricing decision table + RFQ/pricing drawers). |
| `js/performance.js` | Extended: settlement bar/summary/drawer, merged fills + settlement table, cumulative Brier, game filter. |
| `js/risk.js` | Renamed from `inventory.js`; adds risk events, exposure bars, game filter highlight. Kill-switch buttons removed from the tab (moved to header). |
| `js/engine.js` | Trimmed to health: latency, reasons, feed health, drafts. |
| `js/nfl.js` | Kept (Vega views); hosts into the Research grid; adds the live model check panel. |
| `js/rfqs.js`, `js/pricing.js`, `js/inventory.js` | Removed (logic moved). |

### 5.2 Layout system

* `body` is a flex column; `main` fills the remaining viewport height; each tab is a CSS grid of bordered panels; panel bodies scroll internally (`overflow:auto`), headers stay fixed. Below 1100 px the grid collapses to a single scrolling column.
* Tokens: `--bg #000`, `--fg #f2f2f2`, `--rule #d9d9d9`, `--soft #1a1a1a`, `--acc #ff9f1c`, `--pos #3fd07f`, `--neg #ff4d4d`, `--dim #7d7d7d`. Fonts: IBM Plex Mono (data) and IBM Plex Sans Condensed (labels) from Google Fonts with system monospace/sans fallbacks, so the dashboard still works offline.
* Charts are inline SVG in a fixed viewBox with `preserveAspectRatio="none"` and `vector-effect: non-scaling-stroke`. Labels live in HTML footers (never in stretched SVG text). Vega charts (Research) use a dark/orange config.

### 5.3 Global behaviours

* **Kill switch (header):** state read from `/api/risk` each poll; `TRIP` opens a popover (reason field, confirm, cancel); `RESET` when tripped. Uses the existing POST. Replaces the old `#killswitch-badge`.
* **Game filter chip:** `state.game`. Click a game cell anywhere sets it; the chip's × clears it. Applied server-side to Flow and the Performance fills table; client-side to Risk's game table, events and Performance's game breakdown.
* **Global KPI strip:** first four tiles from `/api/summary` (polled every 15 s regardless of tab), then six tab-specific tiles from each tab's own data.
* **Docked drawer:** replaces the overlay + scrim. `body.drawer-open` reserves 420 px on the right; Esc closes; row selection highlighted in orange. Used by Flow (RFQ detail incl. pricing and lifecycle), Performance (settlement/fill detail).
* **Keys:** `1`–`5` switch tabs, `/` focuses Flow search, `Esc` closes drawer/popover, `P` toggles polling.

## 6. Tab-by-tab spec

**Flow**: filter bar (quotable only, screen, decision, status, game, search, pagination); funnel, arrivals, edge histogram strip; merged blotter (RFQ, age, screen, legs, game, side, size, decision, reason, naive, fair, our, Q edge, status, trade); drawer with legs, prices, edges, decision explanation, lifecycle.

**Performance**: settlement action bar + result alert; realized-vs-expected P&L; cumulative Brier (model vs naive, from settlement rows); P&L by family; fills & settlement table (fill prices + outcome + Brier columns joined by `rfq_id` from `/api/settlements`); by-game breakdown (click to filter).

**Risk**: WCL over time (pending/executed/total); exposure by market; exposure by team; exposure by game table; risk & paper events table; kill-switch status in header.

**Engine**: alert bar (stale heartbeat, kill switch); wait and compute histograms; latency table; decline/skip reasons; feed health (arrivals/min, heartbeat age, drops, gateway); stored draft quotes.

**Research**: "RESEARCH · BACKTEST DATA · NOT LIVE" banner; existing subtabs/filters/run buttons; live model check (correlation-lift + settlement Brier) panel; Vega charts and tables placed in the panel grid.

## 7. Build order

1. Backend B1–B6 + unit tests.
2. `style.css` + `index.html` skeleton + `core.js` (tabs, header, drawer, charts) so the shell renders.
3. Tabs in dependency order: Engine (simplest) → Risk → Performance → Flow → Research.
4. Update server tests for new DOM ids/files; run the full dashboard test file.
5. Manual verification in the browser against a seeded database (screenshots per tab, console clean, kill-switch trip/reset round trip on a scratch DB, game filter, drawer, keyboard).
6. Docs touch-up (`docs/always-on.md` and `docs/correlation-model.md` file references).

## 8. Test plan

Automated (`tests/test_dashboard_server.py`):

* `/` serves new tab ids (`flow`, `performance`, `risk`, `engine`, `research`) and the new script files; every static file is whitelisted and 200s.
* `/api/rfqs` rows carry `decision` / `response_price` / `edge_vs_market` for priced RFQs and null for unpriced.
* `/api/flow/summary` returns funnel, 60 arrival buckets and histogram; honours `game`.
* `/api/fills?game=` filters; unknown game returns zero rows.
* `/api/summary` returns the four KPI fields and tolerates an empty database.
* Existing kill-switch, settlement, NFL, source-switching tests keep passing (DOM-id assertions updated).

Manual (browser): each tab renders without console errors at 1440×900 and at 390 px; empty-database state shows "waiting"; polling pauses when hidden; drawer open/close; popover confirm path; Research Vega charts render.

## 9. Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| Live capture is large; new aggregates could be slow | Bounded scans, `/api/summary` cached 10 s, summary panel failures degrade to "—" without blocking tables. |
| Google Fonts unavailable (offline desk) | System monospace/sans-serif fallbacks in the stacks. |
| Removing ids breaks tests/tools | Keep stable ids where the concept survives (`settlement-run`, `settlement-table`, `poll-toggle`, `chart-wcl`, `game-filter`, `status-filter`, `corr-kpis`); update the rest in tests. |
| Large rewrite regresses a tab | Port logic function-by-function, reuse existing API shapes, verify each tab against the seeded DB before moving on. |

## 10. Rollout

Single branch (`feat/terminal-dashboard`), reviewed as one PR; no config flags. Revert is a single `git revert`.

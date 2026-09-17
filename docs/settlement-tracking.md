# Settling live quotes: scoring the model after the final whistle

*Implemented — `combo_mm/nfl/settle_live.py`, `scripts/settle_live_quotes.py`
and the dashboard's Settlement section. Scope: NFL only, same-game combos,
paper trading only.*

*Where this differs from the plan as first written, the text says so: running
it against real live quotes turned up one case the plan had missed (see
**Unsettleable legs**). Related: Step 5 backtest harness
([#5](https://github.com/andrewhulll/polymarket-bot/issues/5)) and the roadmap
([#17](https://github.com/andrewhulll/polymarket-bot/issues/17)).*

## Why this exists

The live feed already prices. Every RFQ the screen calls `QUOTABLE` goes to
`LiveQuoter`, which prices it off the poll thread and writes the outcome —
quote or decline — to `priced_quotes` in `data/live/quote_selections.db`. That
file is durable: it survives closing the dashboard, and each live run appends
to it.

What is missing is the other half. Grep every reader of `priced_quotes` and you
find exactly two, both of them display code: `dashboard/app.py:609` (per-RFQ
detail) and `dashboard/app.py:927` (the live quotes table). Nothing revisits a
stored quote after the game finishes. So the database accumulates *what we
would have quoted* and never learns whether it was right — no realized
outcome, no calibration, no answer to the only question that matters about a
correlation model: **did pricing the same-game block jointly beat multiplying
the legs independently?**

The backtest answers that question, but only in-process and only on
reconstructed history (`combo_mm/nfl/week_backtest.py` settles each RFQ against
the final score before returning). Live quotes are priced against real books,
on real flow, and then dropped on the floor.

Two smaller gaps travel with this one:

- The lifecycle event store is a throwaway. `_start_live` calls
  `_new_db("combo_mm_live_")`, which is
  `tempfile.NamedTemporaryFile(delete=False)` (`dashboard/app.py:113`), so RFQ
  events, screen results and quote-latency rows land in a fresh `%TEMP%` file
  per run that nothing ever reopens. These grow fast — a single run from
  2026-09-16 left a 142 MB orphan.
- There is no marker distinguishing a scored quote from an unscored one, so
  any future job cannot tell what it has already processed.

## What already exists (the plan adds no new data source)

Everything needed to settle a stored quote is already retained. This is worth
stating precisely, because it means the work is a join and an arithmetic pass,
not a data-collection project.

| Need | Where it already is |
| --- | --- |
| Which legs the quote covered | `priced_quotes.detail_json` → `legs[]`, each with `position_id`, `slug`, `game`, `canonical`, book `bid`/`ask`, `q_market`, `p_model` |
| Leg identity → market meaning | `data/live/combo_markets.json` (the `ComboMarketCatalog` cache, `dashboard/app.py:188`) maps `position_id` → `LegMarket(slug, outcome_index)`, durably across restarts |
| Market meaning → settlement rule | `catalog_markets.parse_catalog_leg(slug, outcome_index)` → `(NflLegMarket, side)` |
| Leg settlement | `markets.settlement_price(market, home_score, away_score, push_rule)` → `"1"` / `"0"` / `"0.5"` / `None` (void) — `combo_mm/nfl/markets.py:269` |
| Final scores | the cached nflverse pull: `ingest.latest_pull()` → `load_games()` → `Game.home_score` / `away_score`, with `Game.played` guarding unplayed games |
| Combo settlement convention | already fixed by the backtest (`week_backtest.py:352-375`): any void leg voids the whole combo; otherwise all legs win → `1.0`, else `0.0` |

The plan reuses all six rather than restating any of them.

## Part A — the join from a live leg to a final score

A live leg carries a game key of the form `nfl-{away}-{home}-{date}` (for
example `nfl-det-buf-2026-09-18`). `catalog_markets.parse_game_slug` already
decomposes it into `(game_key, AWAY, HOME, date, suffix)` with the team codes
upper-cased. The nflverse side has `Game.away` / `Game.home` /
`Game.gameday`. So the join key is `(season, away, home)`, with
`catalog_markets.season_of(date)` supplying the season (it already handles
January and February belonging to the prior season).

Three things must be handled explicitly rather than assumed:

1. **Franchise aliases.** Normalize both sides through
   `ingest.FRANCHISE_MAP` (`OAK→LV`, `SD→LAC`, `STL→LA`,
   `combo_mm/nfl/ingest.py:65`). Never hand-roll a second mapping.
2. **Date drift.** The slug date is the kickoff date; nflverse `gameday` is
   ET. A late Sunday or Monday kickoff can differ by a day depending on which
   timezone rendered the slug. Match on the team pair and accept a `gameday`
   within ±1 day; if two candidate games match, do not guess — record the
   quote as `UNRESOLVED` with the ambiguity in the detail.
3. **Unplayed games.** `Game.played` is false when scores are `None`. That is
   `PENDING`, not a loss. A quote priced on Thursday must not settle to `0.0`
   because the game had not kicked off when the job ran.

### Unsettleable legs (found while implementing)

The screen only requires that **one NFL game contributes two or more legs**.
Every other leg rides along and is priced as an independent multiplier — a
single leg from another game, or a non-game market entirely. The first real
run made the consequence concrete: of 730 stored quotes, **149 (20%) carry a
leg that no final score can ever settle**. One of them is an MLB leg,
`mlb-det-cws-2026-09-17`; others are player props and half/quarter markets.

Treating these as `UNRESOLVED` would mean retrying a fifth of the book on
every run, forever, for an answer that can never arrive. So they get their own
terminal status, `UNSETTLEABLE`, distinct from `UNRESOLVED` (an unknown
position id or a game missing from the pull, both of which a later run may
well resolve).

The status precedence is therefore:

| Status | Terminal | Meaning |
| --- | --- | --- |
| `VOID` | yes | A leg pushed or an ML tied. Absorbing — one void leg voids the combo whatever the others did, which is the backtest's convention. |
| `UNSETTLEABLE` | yes | A leg no final score settles: prop, period, or non-NFL. |
| `UNRESOLVED` | no | A position id not in the catalog cache, or a game not in the pull. Retried. |
| `PENDING` | no | Every leg resolved; a game has not been played. Not a loss. |
| `SETTLED` | — | Every leg settled; `combo_value` is 1.0 or 0.0. |

A leg we cannot settle blocks the combo even when another leg has already
lost: the unknown leg could itself void, and a void combo is not a losing one.

## Part B — schema

A new table in the same database. `priced_quotes` is never mutated: what the
model said at quote time is an immutable record, and settlement is a separate
fact learned later.

```sql
CREATE TABLE IF NOT EXISTS quote_settlements (
    rfq_id           TEXT NOT NULL,
    trigger          TEXT NOT NULL,
    settled_at       TEXT NOT NULL,   -- when this row was computed
    status           TEXT NOT NULL,   -- SETTLED | VOID | PENDING | UNRESOLVED | UNSETTLEABLE
    reason_detail    TEXT,            -- why it is not SETTLED
    combo_value      REAL,            -- requested side, 1.0 / 0.0; NULL unless SETTLED
    combo_yes        REAL,            -- the combo's own YES, before the side inversion
    n_legs           INTEGER NOT NULL,
    n_legs_settled   INTEGER NOT NULL,
    legs_json        TEXT NOT NULL,   -- per leg: position_id, settlement_price, game_id, side
    side             TEXT,            -- the side the RFQ asked about
    fair             REAL,            -- copied from the quote, so scoring needs no re-join
    naive            REAL,
    bid              REAL,
    ask              REAL,
    brier            REAL,            -- (fair - combo_value)^2
    naive_brier      REAL,            -- (naive - combo_value)^2
    edge_vs_naive    REAL,            -- naive_brier - brier; > 0 means the joint model won
    hypo_edge_bid    REAL,            -- combo_value - bid   (counterfactual, 1 unit)
    hypo_edge_ask    REAL,            -- ask - combo_value    (counterfactual, 1 unit)
    realized_pnl     REAL,            -- only where an accepted fill exists
    scores_vintage   TEXT,            -- nflverse pull date used
    model_version    TEXT,
    params_version   TEXT,
    PRIMARY KEY (rfq_id, trigger)
);
CREATE INDEX IF NOT EXISTS idx_settled_status ON quote_settlements(status);
```

`PRIMARY KEY (rfq_id, trigger)` mirrors `priced_quotes` exactly, so the two
tables join one-to-one and the writer is idempotent via `INSERT OR REPLACE`.
Re-running after a newer nflverse pull is the normal path for flipping
`PENDING` → `SETTLED`; re-running after nothing changed rewrites identical
rows.

### What we are allowed to call this

**No live quote was ever traded.** `LiveQuoter` has no submit path, by
construction. So `hypo_edge_bid` / `hypo_edge_ask` are counterfactual — what
one unit would have returned had someone lifted the quote we never sent — and
must be labelled that way everywhere they surface. Calling them P&L would be a
lie the dashboard then repeats.

The honest headline metric is the Brier pair: `brier` against `naive_brier`,
aggregated over settled quotes. That is a direct, fill-free answer to whether
the correlation adjustment improved the forecast, and it is the number the
model should be judged on.

One exception: where an `accepted_quotes` row exists for the RFQ (the
pick-to-quote flow already records price, size, direction and side), realized
P&L *is* meaningful for that row. Compute it with the backtest's sign
convention — `sign * (combo_value - fill_price) * fill_qty`, `sign = +1` for a
BUY fill — and keep it in a separate column from the counterfactual figures.

## Part C — the runner

`scripts/settle_live_quotes.py`, following the existing script conventions
(stdlib + sqlite3, no network, argparse, a summary line on exit):

```
python scripts/settle_live_quotes.py \
    [--db data/live/quote_selections.db] [--raw-root data/raw] \
    [--since 2026-09-01T00:00:00Z] [--dry-run]
```

Flow: select quotes with no terminal settlement row (`status` absent, or
`PENDING`/`UNRESOLVED`) → resolve each leg through the catalog cache → join to
the latest cached pull → settle each leg → fold to a combo value → write.

Scores come from the cached pull, so the job itself never touches the network;
refreshing scores stays `python scripts/refresh_params.py --pull`, which
already exists. Settlement deliberately does **not** run inside the dashboard
process: the poll loop is already missing its 400 ms decision budget under live
load, and a scoring pass has no business competing with it.

## Part D — dashboard surface

A read-only section in the existing live-quotes tab (`dashboard/app.py:915`,
beside the metrics row it already renders): counts of settled / void / pending,
model Brier vs naive Brier with the difference, combo hit rate, and a table
joining `quote_settlements` to `priced_quotes` so each row shows quoted
bid/ask/fair alongside the realized value. Per-quote detail lists each leg with
its settlement price and the game it resolved against.

The dashboard displays; the runner computes. Nothing in the dashboard writes a
settlement row.

## Part E — making the event store durable

Change `_new_db("combo_mm_live_")` (`dashboard/app.py:113`) to a dated path
under `data/live/` so screens, lifecycle events and latency history survive a
restart alongside the quotes. `data/` is already gitignored, so nothing leaks
into the repo. Given the observed growth rate (142 MB in one session), this
lands with daily rotation and a retention cap rather than as an unbounded
append — otherwise the fix trades a lost file for a full disk.

## Testing

`tests/test_settle_live.py`, in the existing style — stdlib plus pytest, no
network, deterministic:

- all legs win → `SETTLED`, `combo_value == 1.0`; one leg loses → `0.0`
- a pushed leg → `VOID`, `combo_value` NULL, no counterfactual edge recorded
- idempotency: run twice over the same input, exactly one row per
  `(rfq_id, trigger)`, second run byte-identical
- unplayed game → `PENDING`; re-run against a pull that has scores → `SETTLED`
- franchise alias (`OAK` vs `LV`) joins; ±1 day `gameday` drift joins; two
  candidate games → `UNRESOLVED`, never a guess
- a `position_id` missing from the catalog cache → `UNRESOLVED` with the id in
  `reason_detail`, never silently dropped
- Brier arithmetic against hand-computed values, including a quote whose naive
  price beat the model (the metric must be able to say we lost)

## Assumptions and non-goals

- `push_rule` defaults to `"void"`, matching the wire convention already used
  by `settlement_price` and the backtest. A pushed leg voids the whole combo.
- Scores are nflverse, not Polymarket's own resolution. These agree on final
  scores essentially always, but the market is the real authority; a divergence
  would be invisible to this job. Recording `scores_vintage` per row is what
  makes such a case auditable after the fact.
- Live paper mode produces no fills, so there is no portfolio, no inventory
  effect and no capital at risk to report here. Step 3 owns that.
- Player props and period markets are already screened out upstream and stay
  out of scope.

## Rollout order (all landed)

1. `quote_settlements` schema + store methods on `QuoteSelectionStore`
2. `combo_mm/nfl/settle_live.py` — pure functions: leg resolution, the score
   join, the combo fold, the metrics. No I/O, so tests hit it directly.
3. `scripts/settle_live_quotes.py` — the runner around those functions
4. dashboard settlement section
5. event-store durability + rotation (independent of 1–4; can land first)

Steps 1–3 deliver the value on their own: once the runner writes rows, the
question "did the correlation model beat the naive maker on live flow" is
answerable from SQL alone, with or without the dashboard view.

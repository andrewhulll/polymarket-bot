# Simulated NFL RFQ datasets

*Issue [#15](https://github.com/andrewhulll/polymarket-bot/issues/15). Scope:
NFL only, same-game combos, paper trading only.*

## Why this exists

Steps 2 (wire the correlation model into the pricer), 3 (inventory replay) and
5 (backtest) all need **an NFL RFQ stream to replay**, and none exists:

- The only RFQ replay dataset, `combo_mm/fixtures.py`, is political markets
  (`POTUS-2028`, `SENATE-2026`, `HOUSE-2026`). Not one leg is an NFL
  moneyline, spread or total.
- There is no Polymarket API access yet (see #11), so real NFL RFQ flow cannot
  be recorded.
- `combo_mm/nfl/` has 20 seasons of games with closing lines and final scores,
  but as *games*, not as RFQs.

This document covers the bridge: a leg-market registry that gives an RFQ leg
symbol a meaning, and a generator that manufactures RFQ flow from historical
games in the wire format the pipeline already replays.

**These are simulated requests, not recorded Polymarket data.** Every result
computed on them inherits the assumptions listed below. When #11 lands and real
RFQ flow can be recorded, the recorded sessions replace the generated ones and
the same harness runs unchanged — that is the point of matching the wire
format.

## Part A — the leg-market registry

`combo_mm/nfl/markets.py` (stdlib only) is the single place the NFL market
conventions live.

### Symbol grammar

```
NFL-{season}-W{week:02d}-{AWAY}-{HOME}-ML-{TEAM}              YES = TEAM wins
NFL-{season}-W{week:02d}-{AWAY}-{HOME}-SPR-{TEAM}-{M|P}{x}    YES = TEAM's margin + its signed line > 0
NFL-{season}-W{week:02d}-{AWAY}-{HOME}-TOT-{x}                YES = game total > x
NFL-{season}-W{week:02d}-{AWAY}-{HOME}-TT-{TEAM}-{x}          YES = TEAM's own points > x
```

`M`/`P` encode the sign so no `-` appears inside a `-`-delimited field:
`SPR-KC-M3.5` is KC favoured by 3.5, `SPR-KC-P3.5` is KC getting 3.5. A spread
line is always in the **subject team's** terms.

`NO` on any leg is the opposite-direction leg on the same line, conditional on
no push — the same basis `combo_mm/nfl/joint.py` already conditions on. Totals
and team totals are listed once, so "under" is the NO side of the over market,
exactly as a binary market works.

The grammar is internal. A live resolver for real Polymarket token ids (#11)
must produce the same `NflLegMarket`, and everything downstream stays
symbol-agnostic; `parse_symbol` raises on anything it does not recognise
rather than guessing a market.

### Canonical mapping

nflverse `spread_line` is the **home expected margin**, while a spread symbol
carries its line in the subject's terms, so the two differ by a sign for home
subjects. `to_joint_leg` owns that conversion and every row of the mapping is
pinned by a unit test in `tests/test_nfl_markets.py`, including agreement
between `settlement_price` and `joint.settle_leg` over 2,000 random scores.

### Settlement assumptions (open questions)

`settlement_price` returns the **raw YES** result as the wire carries it
(`"1"`, `"0"`, `"0.5"`, or `None` for a void leg) and never inverts for NO —
the pricer owns that inversion.

| case | knob | default | effect |
|---|---|---|---|
| integer spread/total lands on the line | `push_rule` | `"void"` | leg settles `None`, which voids the combo (no P&L) |
| | | `"half"` | leg settles `"0.5"` |
| moneyline tie (~0.2% of games) | `tie_rule` | `"void"` | both moneylines void |
| | | `"half"` | both settle `"0.5"` |
| | | `"no"` | the team did not win, so `"0"` |

**To verify with Polymarket before live:** the NFL market rules for ties and
pushes, and whether listed lines are always half-points. Both are config knobs
precisely because the real rules are unverified, and #5 reports sensitivity to
them.

## Part B — the generated dataset

`combo_mm/nfl/rfq_sim.py`, driven by `scripts/gen_nfl_rfq_dataset.py`.

```bash
# the test period, scored once
python scripts/gen_nfl_rfq_dataset.py --seasons 2022-2025 --seed 7 \
    --rfqs-per-game 40 --out data/rfq_sim/test_2022_2025

# the train period, used to tune spread knobs and risk thresholds
python scripts/gen_nfl_rfq_dataset.py --seasons 2006-2021 --seed 7 \
    --out data/rfq_sim/train_2006_2021
```

The train/test split mirrors `docs/correlation-model.md`: **knobs are tuned on
train datasets only; the test dataset is scored once.**

### Four principles

1. **Exogenous events only.** A session carries leg books, RFQ requests, the
   close of each request window, and settlements. It carries no acceptances,
   confirmations or executions — those depend on *our* quote, so #5's fill
   model produces them. Pre-baking them would make the backtest meaningless.
2. **Leakage-safe by construction.** Every book is a function of the line path
   up to its own timestamp; final scores appear only in settlement events
   stamped after the game ends. `tests/test_nfl_rfq_sim.py` regenerates a
   dataset with every score zeroed and asserts the books come out byte
   identical.
3. **Same wire format.** Items are `{"t": ms, "kind": "event"|"book"|
   "disconnect", ...}` on a dataset-relative clock, so `SimulatedTransport`,
   `normalize`, `EventStore.apply`, `ShadowQuotingEngine` and
   `paper_backtest.run_backtest` consume them unchanged (the replay harnesses
   take the dataset's `base_ts`).
4. **Deterministic.** One seed gives byte-identical files. Randomness comes
   from per-(game, purpose) substreams seeded by SHA-256, so adding a feature
   cannot shift unrelated draws, and normals are Box-Muller over
   `random.Random` rather than a library sampler that may be re-tuned between
   releases.

### Assumptions, and what they cost

| assumption | default | why, and the risk |
|---|---|---|
| **Line paths** are Brownian motions run *backwards* from the closing line | `sigma_spread_week = 1.0`, `sigma_total_week = 1.25` points | nflverse has no opening lines. The path ends at the true close and is noisier the earlier it is read. Too small a sigma makes early books unrealistically informative; #5 reports sensitivity |
| **Price level** is pinned to the de-vigged closing prices by a constant correction to the model's implied means | on | Without it every game would close at a model 0.50 regardless of what the market showed. Asserted to within 1.5c in `tests/test_nfl_rfq_sim.py` |
| **Moneylines** blend linearly into the de-vigged closing moneyline | `ml_blend = True` | The spread/total calibration does not pin the moneyline, which is #2's over-identification check. Blending means closing ML prices match the market; switch it off to see the model's own ML |
| **Listed lines** are snapped to half points | `force_half_point_lines = True` | Makes pushes rare, as real NFL listings do — but it moves the key numbers (3 → 3.5, 7 → 7.5) by half a point. Use `--allow-integer-lines` to study pushes |
| **Arrivals** are a non-homogeneous Poisson process, intensity `exp(-(kickoff - t)/tau)` | `tau = 18h`, `rfqs_per_game = 40`, window 6 days | Flow rises into kickoff. The level is a guess; nothing downstream should depend on the absolute RFQ count |
| **Combo shapes** follow fixed family weights | ML×spread .25, spread×total .25, ML×total .15, ML×spread×total .20, alt/team-total .10, contradictory .05 | Retail parlay flow. The 5% contradictory family (underdog ML + favourite covers, fair 0) exists so the decline path is exercised |
| **Sides** tilt to favourites and overs | `p_fav = 0.62`, `p_over = 0.58` | Retail tilt. Adverse selection is the fill model's concern, not the flow's |
| **Books** are `mid = p_t + N(0, 0.004)`, half-spread 1c main / 2c alt, sizes lognormal (median 500) | | Microstructure noise and depth are invented. Depth feeds the pricer's size-impact term, so a real book would change quoted spreads |
| **Sizes** are 80% quantity (lognormal median 50, clipped [1, 5000]), 20% cash (lognormal median $25) | | Invented |
| **Staleness** on 2% of RFQs, one leg book older than the cutoff | `stale_share = 0.02` | Exercises the `STALE_LEG` decline |
| **Robustness noise**: 1% duplicate deliveries, 0.5% re-ordering, ~1 disconnect per week | | Re-delivery and re-ordering only, never new state, so the replayed state digest equals a clean run — the Step 1 idempotency guarantee, asserted in the tests |

### Lifecycle

Each RFQ is created, closed at its submission deadline (3s), and settled after
the game.

The close is **exogenous**: hours-old requests accept no more quotes, and
`CLOSED` is the neutral terminal state — *why* a request ended (traded, or
expired unfilled) depends on our quote and belongs to the fill model. The
pipeline lets a quote advance after its RFQ closed (see `fixtures.py`
RFQ-004), so #5 can layer accept/confirm/execute events on top. Without the
close, the engine would re-price a settled RFQ and record a pricing decline
for legs that had already resolved.

Settlements are **stream-invisible** (`"stream": false`) at kickoff + 3h30m
(+15m for overtime), carrying each leg's `settlementPrice` — the same
mechanism `fixtures.py` uses, because the real contract exposes settlements
only through `GetRFQs`.

### Walk-forward params

A game's week gets its covariance params from `WalkForwardParams`, which
estimates from games **strictly before** that week with the frozen estimator
(`params/estimator.json`) and caches the result as
`params/history/nfl_<season>_w<ww>.json` — the same kind of weekly file the
pricer reads live, so a dataset can never carry a parameter fitted on its own
outcomes. `params/history/` is gitignored and regenerated.

### On-disk layout

```
data/rfq_sim/<dataset_name>/
  session.jsonl.gz     # ordered items, fixtures.py schema
  combos.json          # reference metadata per combo symbol (tick, limits, legs)
  markets.json         # LegRegistry dump (Part A)
  sidecar.jsonl.gz     # BACKTEST-ONLY (see below)
  manifest.json        # generator version, seed, config, counts, source
                       #   games.csv sha256, params weeks, git commit,
                       #   sha256 of every file
```

`data/` is gitignored; datasets are regenerated from the manifest's seed and
config.

### The sidecar is backtest-only

`sidecar.jsonl.gz` holds what only a **fill model** may see: the requester's
id and whether they are `sharp`, the closing-line model fair, the closing
naive fair, the naive fair at request time, and a competitor maker's bid and
offer. None of it appears in the session.

Only `combo_mm/nfl/rfq_sim.py` (which writes it) and #5's
`combo_mm/backtest/fill_model.py` may reference it. A source-scan test
(`test_sidecar_is_not_readable_by_any_pricing_module`) fails if any other
module under `combo_mm/` so much as mentions it, and `load_session`
deliberately does not return it.

## The committed fixture week

`combo_mm/fixtures_nfl.py` is a hand-written four-game slate with 40 RFQs where
each request exercises a deliberate case. It needs no data pull and no
numpy/scipy, so tests and the dashboard ("NFL fixture week") work on a fresh
checkout.

| game | line / result | what it exercises |
|---|---|---|
| `BUF @ KC` | KC -3 (integer), 27-24 | the spread **pushes**, voiding the combo |
| `NYJ @ NE` | NE -6.5, 20-20 | moneyline **tie** voids |
| `SF @ SEA` | SF -2.5, 31-28 OT | **overtime** settlement delay |
| `DAL @ PHI` | PHI -1.5, 24-17 | main total book **never published** (calibration fallback) |

Requests additionally cover a nested combo, an impossible combo, an unknown
leg symbol, a cross-game combo, a stale leg book, a size below the minimum, a
cancel, an expiry, cash sizing, a duplicate delivery, a late delivery (so
exchange time and delivery time come apart), a close delivered before its
create, and a mid-slate disconnect.

Decline *codes* are deliberately not asserted: the correlation-aware pricer
that tells a nested combo from an impossible one is #2's, and this fixture is
the flow it will be built against.

## What this does not do

- No player props (Phase B), no cross-game parlays in the generated flow
  (the fixture carries one so the skip path exists), no in-game or live RFQs.
- No fill or acceptance model — that is #5.
- No claim that the flow's level or mix matches Polymarket's. Replace these
  datasets with recorded live RFQs when #11 lands.

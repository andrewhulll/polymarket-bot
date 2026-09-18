# Backtest harness (issue #5)

**PAPER / SHADOW ONLY.** The harness replays RFQ flow through the real
shadow quoting engine and a counterfactual fill model. It never submits
RFQs, quotes, confirmations or orders, and makes no network calls.

## Two-step reproduction

```bash
# 1. Generate a simulated NFL RFQ dataset (see docs/rfq-simulation.md)
python scripts/gen_nfl_rfq_dataset.py --seasons 2022-2025 --seed 7 \
    --out data/rfq_sim/test_2022_2025

# 2. Replay it chronologically
python scripts/backtest.py --dataset data/rfq_sim/test_2022_2025 \
    --out runs/ --params params/nfl_2026_w02.json [--sensitivity] [--workers 4]
```

Every run lands under `runs/<utc-timestamp>-<dataset_id>/` (git-ignored):

| file | contents |
|---|---|
| `manifest.json` | dataset hash, params versions + sha256, full config, seeds, git commit + dirty flag, `state_digest`, `leak_violations`, wall time |
| `summary.json` | headline counts, rates, P&L, swings, exposure peaks |
| `per_rfq.csv` | per-RFQ decisions, fills, settlements, P&L |
| `equity.csv` | realized equity, stepped at **settlement time** |
| `equity_mtm.csv` | mark-to-model equity (open positions at latest draft fair) |
| `exposure.csv` | additive max-loss exposure (**NOT** correlation-aware until #3) |
| `breakdown_*.csv` | by market type / combo size / favourite bucket / requester type / decline reason |
| `sensitivity.csv` | corr-scale × fill-knob grid (`--sensitivity`) |
| `store.sqlite` | the replayed event store (dashboard drill-down) |
| `figures/` | equity + exposure PNGs (when matplotlib is available) |

**Determinism:** the same command + inputs → identical `summary.json`,
CSVs and `state_digest` (the `run_id` is excluded). Two runs with the
same seeds produce byte-identical decisions.

## Methodology

**Input.** A generated NFL RFQ dataset (issue #15): `session.jsonl.gz`
(books, `rfq_created`, neutral `rfq_closed`, `rfq_updated` settlements),
`sidecar.jsonl.gz` (future information per RFQ), `combos.json`,
`markets.json` (registry snapshot), `manifest.json` (generator version,
seed, config, per-file sha256). No Polymarket RFQ history exists, so the
dataset *reconstructs* what RFQ flow would have looked like: historical
weeks' de-vigged closing lines become books, synthetic 2–3-leg same-game
combos fire as RFQs in the three hours before kickoff, settled on actual
final scores.

**Replay** (`combo_mm/backtest/runner.py`). One merged chronological pass:
dataset items stream in `t` order through the real `ShadowQuotingEngine`;
each draft schedules a fill-model decision for `t_rfq + think_ms`; due
decisions are simulated and their events (`quote_accepted` →
`quote_confirmed` → `quote_executed` → `drop_copy_fill`, or a competitor
`rfq_closed`) are applied at their own exchange timestamps before the
clock advances past them. Settlements land at their actual timestamps
(kickoff + 3.5h); the realized equity curve steps there, never at fill
time. The engine's quoting does not depend on fills (risk is parked), so
drafts are exactly what live paper trading would have produced.

**Pricing.** `combo_mm/backtest/nfl.py` builds a registry-backed
`NflJointPricer` from the dataset's own markets plus walk-forward
correlation params (fitted on games strictly before the target week —
no lookahead). `corr_scale=0` reproduces the naive product price; `=1` is
the fitted model. The edge the backtest measures is the gap between the
joint price and the naive baseline.

## Fill-model assumptions and caveats

The fill model (`combo_mm/backtest/fill_model.py`) is the **only** module
allowed to read the sidecar (enforced by `leak_guard.py` + a source-scan
test + an instrumented no-leak run). Per quoted RFQ, at
`t_rfq + requester_think_ms` (default 1,000 ms):

- competitor present with p=`competitor_presence` (0.8), quoting
  `naive_fair_at_request ± competitor_half_spread` (2.5c);
- retail (95%) values the combo at `closing_naive_fair + N(+1c, 1c)`;
  sharp (5%) at `closing_model_fair + N(0, 0.5c)` (adverse selection);
- the requester lifts the best price inside `valuation ± tolerance`
  (0.5c); ties split 50/50; ~90% of RFQs are BUY (retail parlay flow);
- a quote counts only if
  `decided_at + quote_latency_ms (150) ≤ submission_deadline`
  (`LATE_QUOTE` otherwise);
- fills use our risk-adjusted qty for the side; cash RFQs use
  `floor(cash / price)`.

**Caveats.** Absolute P&L depends on these assumptions — the requester
valuation distribution and competitor behavior are modeled, not measured.
Treat **relative** comparisons (model vs naive, `corr_scale` sweeps,
knob variations in `sensitivity.csv`) as the meaningful output. With
real recorded RFQs (#11), the sidecar disappears: competitor quotes and
outcomes come from the tape, and the fill model becomes a pure
replay check.

## Metrics (shared)

`combo_mm/backtest/metrics.py` is the single implementation used by the
CLI runner, `paper_backtest.py` (dashboard replay path) and
`nfl/synthetic_backtest.py` (parity-tested):

- counts/rates: received / quoted / rejected / expired / executed,
  `lost_to_competitor`, `late_quotes`, quote/execution/win-vs-competitor rates;
- **expected P&L** (ex-ante, per fill): `(our price − model fair) × qty`
  signed by side; `expected_pnl_naive_basis` uses the naive product;
  `quoted_edge_notional` keeps the legacy half-spread-over-quotes number;
- **realized P&L** at settlement: `(settlement − price) × qty` long,
  `(price − settlement) × qty` short; pushes settle at 0.5, contribute 0
  and are counted in `voided`;
- swings (positive magnitudes + peak/trough timestamps) on both the
  settlement-time realized curve and the mark-to-model curve;
- Brier score of our fair vs the naive product on settled non-void combos;
- exposure: additive max-loss from the fills ledger (labeled NOT
  correlation-aware; scenario WCL arrives with #3's `exposure_snapshots`);
- breakdowns by market type, combo size, favourite bucket, requester
  type (sharp/retail, from fill-model outcomes only), decline reason —
  every table sums to the totals.

## No-leakage guarantees

- **Source scan** (`tests/test_nfl_rfq_sim.py`,
  `tests/test_backtest_harness.py`): only `fill_model.py`, the #15
  generator, and the boundary modules that document the rule may mention
  the sidecar.
- **Instrumented run**: `instrumented_run()` arms the guard; any sidecar
  read outside the fill model raises `SidecarLeak` and lands in
  `manifest.leak_violations` (must be 0).
- **Perturbation test**: re-running with post-T items removed (or the
  sidecar's future fields shifted) leaves every pre-T draft snapshot and
  decision byte-identical; only counterfactual outcomes move.

## Sensitivity (item D)

`combo_mm/backtest/sensitivity.py` replays the dataset over:

- `corr_scale` in `[0, 0.5, 1, 1.5, 2]` at default fill knobs, plus
- one-knob-at-a-time: `competitor_half_spread`, `retail_bias_mean`,
  `sharp_share` at `corr_scale=1`.

Cells are independent; `--workers N` fans them out over a process pool.
The CLI reports the corr sweep's max adjacent `|Δexpected_pnl|` as a
share of the sweep range (graceful-degradation check).

## Train / test discipline (B6)

Not yet run: spread knobs, risk limits and `min_confidence` are to be
tuned on `data/rfq_sim/train_2006_2021` with `scripts/backtest_tune.py`,
frozen into `params/backtest_frozen.json`, and then scored once on
`data/rfq_sim/test_2022_2025`. Until then, treat all numbers as
in-sample with respect to the fill-model knobs.

## What changes with real RFQs (#11)

Recorded live RFQs replace the simulated session through the same
`Dataset` loader; the sidecar and fill model become unnecessary for
fills that actually happened (the tape says who traded). The harness
keeps its value for counterfactuals: reprice history with a new
`corr_scale` or spread policy and compare against what the tape
recorded.

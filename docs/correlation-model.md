# NFL same-game correlation model (Phase A)

How the bot prices a **same-game combo** — one side of two or three of a game's moneyline, spread and
total markets (e.g. *Chiefs ML + Chiefs −6.5 + over*, *Chiefs ML + opponent +6.5*) — at its joint
probability instead of the naive product of leg prices, how the correlation parameters are estimated,
and what 16 seasons of walk-forward backtesting say about it. Issues: #2 (model), #6 (data pipeline).

Everything below is reproducible:

```bash
pip install -r requirements-nfl.txt
python scripts/nfl_tune.py --pull            # nflverse pull + train-only tuning -> params/estimator.json
python scripts/nfl_backtest.py              # walk-forward train 2006-2021 / test 2022-2025 -> results/nfl_backtest/
python scripts/refresh_params.py             # weekly params file + gates -> params/
streamlit run dashboard/app.py               # "NFL correlation" tab
```

Numbers quoted here are from the nflverse pull of 2026-09-16 (sha256 `0ac6de43583d…`), seasons
2006–2025 incl. playoffs: 5,426 games, 90,474 combos, 2,959 dropped for pushes. **Train 2006–2021,
test 2022–2025**, and estimator settings tuned on train only (§2).

---

## 1. Canonical form: every leg is a linear inequality on the scores

Model a game's final scores `S = (S_home, S_away)` as bivariate normal:

```
S ~ N(μ, Σ),   μ = (μ_h, μ_a),   Σ = [[σ_h², ρσ_hσ_a], [ρσ_hσ_a, σ_a²]]
```

With margin `M = S_h − S_a` and total `T = S_h + S_a`, every Phase A leg is `a·S > b` or `a·S < b`:

| Leg | row `a` | wins iff |
|---|---|---|
| home / away moneyline | (1, −1) | `M > 0` / `M < 0` |
| home / away covers (nflverse `spread_line` = home expected margin `x`) | (1, −1) | `M > x` / `M < x` |
| over / under `U` | (1, 1) | `T > U` / `T < U` |
| home / away team total over `t` | (1, 0) / (0, 1) | `S_h > t` / `S_a > t` |

A combo wins on the intersection of half-planes — a convex polygon in the 2-D score plane — so its
fair value is a Gaussian probability over that polygon (`combo_mm/nfl/joint.py`).

**Where correlation comes from.** Nothing is bolted on:

```
Var(M) = σ_h² + σ_a² − 2ρσ_hσ_a
Var(T) = σ_h² + σ_a² + 2ρσ_hσ_a
Cov(M, T) = σ_h² − σ_a²
```

- Legs on the **same dimension** (moneyline and spread are both about `M`) are strongly dependent by
  construction: covering −6.5 implies winning.
- **Margin vs total** dependence exists only if the two teams' score variances differ. If the
  favorite's score is noisier than the underdog's, "favorite covers & over" is positively correlated.
- `ρ` (within-game score correlation) links team totals to each other and to the game total.

**Discreteness and pushes.** Scores are integers and rows have integer coefficients, so `a·S > L`
means `a·S ≥ ⌊L⌋ + 1`, which is evaluated as `a·S > ⌊L⌋ + 0.5` (continuity correction). Integer
lines can push (`a·S = L`). Pushed combos are dropped from the backtest (Polymarket's push rule —
void leg vs void combo — is still open), and model prices are **conditional on no leg pushing**, the
same basis as de-vigged sportsbook prices. The no-push probability uses inclusion–exclusion over the
distinct push bands.

**Numerics.** Whiten `S = μ + L R u` (Cholesky `L`, a fixed rotation `R` so no canonical row aligns
with the integration axis), integrate the conditional normal CDF of `u₂` over a 1,201-node grid in
`u₁`. Error vs the exact bivariate normal CDF < 1e-5 (tested); a 3-leg combo with team totals matches
Monte Carlo. Region probabilities are memoized per game, so pricing all 17 combos on one game shares
work. Unlike scipy's MVN CDF, this handles 3+ legs on the 2-D score (a singular covariance for the
stacked legs).

## 2. History for shape, market for location

### Location — market-implied means (per game, cheap)

Given the de-vigged closing prices `p_cover` (home side of `x`) and `p_over` (of `U`), solve for
`(μ_h, μ_a)` such that the model reproduces both prices, holding the covariance shape fixed
(`calibrate_means`). `P(cover)` depends only on `μ_h − μ_a` and `P(over)` only on `μ_h + μ_a`, so
each is a 1-D Brent root find; when `σ` depends on `μ` the two are iterated to a fixed point (1–5
iterations, median 4, on the backtest games). Consequence: **the model and the naive product agree on every spread and total leg**,
so any gap on a spread × total combo is purely dependence. Moneyline legs are over-identified (§4.4).

### Shape — how correlations are estimated (`combo_mm/nfl/estimate.py`)

1. **Residuals against the closing line, not raw scores.** Implied points
   `μ_h = (total + spread)/2`, `μ_a = (total − spread)/2`; residual `e = score − μ`. Raw scores mix
   within-game noise with between-matchup differences in expected scoring, which inflates `σ` and
   biases `ρ` negative (raw home/away score correlation is −0.04; residual correlation is ≈ +0.01).
2. **Walk-forward, recency-weighted window.** For target (season, week), only games strictly before
   it; a trailing window (tuned: 4 seasons plus the current season to date) with weight
   `0.5^(age/half-life)` (tuned half-life: 1 season); at least 3 prior seasons required.
3. **Variance model** (weighted least squares on the squared residuals):
   - `league_constant`: `σ² = a` — one league number.
   - `mean_linear`: `σ²(μ) = a + b·μ` — score variance grows with implied points.
   - `mean_linear_team`: `mean_linear` × shrunk per-team offensive × opponent defensive variance factors.
4. **Within-game `ρ`**: weighted correlation of standardized residuals `e/σ(μ)`, league-wide only.
5. **Shrinkage** for team factors and team scoring means: prior = previous seasons in the window,
   regressed halfway to the league value (offseason roll-forward) with a pseudo-count; current season
   blended in with weight `n/(n+k)`. Means use `k = 5` (weeks 1–4 mostly prior, mostly data by week
   8). Variance factors use `k = 15` and cap each game's squared-residual ratio at 9 (3σ): one squared
   residual is χ²(1) noise, and with `k = 5` a single week-1 blowout pushed a team's factor to the cap.

Walk-forward estimates over 2006–2025 (425 weekly fits, `mean_linear`): `σ` at league-average points
9.41 (range 8.93–9.86); slope `b` mean 1.64, SD 0.94; `ρ` mean +0.008, SD 0.036. The slope is
statistically hard — the squared residual has SD ≈ √2·σ² while implied points span only a few SDs —
so its standard error on a 4-season window is ≈ 0.6.

Empirical residual SD by implied team points (2006–2025) shows the effect `mean_linear` models:

| implied points | ≤15 | 15–18 | 18–21 | 21–24 | 24–27 | 27–30 | 30+ |
|---|---|---|---|---|---|---|---|
| empirical σ | 8.48 | 9.08 | 9.10 | 9.48 | 9.59 | 9.71 | 10.23 |

It is real, but too small to improve prices (§4.3), so the train-period tuning selects `league_constant`.

### Hyperparameters — tuned on the train period only (`combo_mm/nfl/tuning.py`)

The weekly estimates are walk-forward by construction, but the estimator's *settings* are choices too.
`scripts/nfl_tune.py` grid-searches them on the **train seasons 2006–2021 only**. Games after 2021 are
removed from the input before anything runs, and a test checks that scrambling every test-season score
leaves the grid and the selection byte-identical:

- variance model: `league_constant`, `mean_linear`, `mean_linear_team`
- trailing window: 4 seasons, 8 seasons, all history
- recency half-life: 1, 2, 4 seasons
- variance-factor shrinkage multiplier: 1×, 3× (team model only)

That is 36 candidates, each scored by Brier over all same-game combos in a walk-forward backtest of
2006–2021. The winner is frozen to `params/estimator.json`: **`league_constant`, 4-season window,
1-season half-life** (train Brier 0.16805 vs naive 0.17602). The race is close. The top nine
candidates are all `league_constant` and within 0.00001 Brier of each other; the best `mean_linear`
is 0.00004 behind, and team factors are clearly worse (best 0.00041 behind). Both the backtest's test
period and the weekly refresh use the frozen file.

## 3. Weekly parameter files (the hot-path contract)

`scripts/refresh_params.py` (Wednesdays, using `params/estimator.json`) → `params/nfl_<season>_w<ww>.json`:
league variance function and `ρ`, per-team factors and means, and per-game
`σ_h, σ_a, ρ, Var(M), Var(T), Corr(M,T)` for the slate at current lines, plus data vintage (pull date +
SHA-256) and estimator config. Canonical JSON (sorted keys, 6 dp): identical inputs give identical
bytes. The pricer only needs `params_io.load_params` + `matchup_covariance(params, home, away, μ_h, μ_a)`,
which are stdlib-only.

Promotion gates (refuses to overwrite the current file on failure):

1. **Range sanity** — schema, finite values, `σ` in bounds, `|ρ| < 1`, positive variances.
2. **No regression** — Brier on the last 272 games' spread × total combos must not exceed the previous
   file's (or naive on the first run) by more than 0.001. In-sample: a guard against broken refreshes,
   not an evaluation.
3. **Determinism** — re-estimation reproduces the staged bytes.

Offseason (no unplayed games): nothing is written; the last file stays current.

## 4. Backtest results: train 2006–2021, test 2022–2025

**Split.** Chronological 80/20 by games over the seasons with closing prices (2006–2025): train
2006–2021 (4,287 games, 79%), test 2022–2025 (1,139 games, 21%). Seasons 1999–2005 have no closing
prices and serve only as estimation history. The split is never shuffled; shuffling would leak future
information.

**Walk-forward in both periods.** Each week's params use only earlier games. Test-period weeks are
estimated from everything before them, including earlier test weeks, exactly as the live weekly refresh
would. The *settings* are frozen from train tuning. The test period was scored once.

`scripts/nfl_backtest.py` prices, per game, all 17 same-game combos with the naive product, each
variance model (at the tuned window/half-life), and `mean_linear` with correlation scaled by
`c ∈ {0, 0.5, 1, 1.5, 2}`. Brier differences use game-clustered standard errors, since all combos in
one game share its outcome.

### 4.1 Headline: out-of-sample holds up

| | Train 2006–2021 | **Test 2022–2025** |
|---|---|---|
| Brier, naive product | 0.17602 | 0.17533 |
| Brier, tuned model (`league_constant`) | 0.16805 | 0.16741 |
| Skill vs naive, all combos | 4.53% (t −22.5) | **4.52% (t −11.9)** |
| Skill vs naive, excluding nested combos | 2.51% (t −14.1) | **2.41% (t −7.4)** |
| `mean_linear` skill, all combos | 4.51% | 4.49% |
| `mean_linear_team` skill, all combos | 3.93% | 3.86% |

Test skill is essentially identical to train skill, and the model ranking is the same in both periods.
There is no sign of overfitting. The test t-statistics are smaller because the test period has about a
quarter as many games.

### 4.2 By family: where the value is

| family | train skill (t) | **test skill (t)** |
|---|---|---|
| ML × spread | 13.4% (−23.3) | **13.2% (−12.0)** |
| ML × spread × total | 5.4% (−22.9) | **5.4% (−12.0)** |
| spread × total | 0.00% (tie) | **0.00% (tie)** |
| ML × total | −0.13% (+2.3) | **+0.01% (−0.2)** |

Selected combos in the test period (realized ± SE vs naive vs model):

| combo | realized | naive | model |
|---|---|---|---|
| Fav ML + Dog covers ("fav wins, doesn't cover") | 16.9% ± 1.1 | 32.8% | 13.4% |
| Fav ML + Fav covers | 49.8% ± 1.5 | 32.9% | 51.6% |
| Dog ML + Dog covers | 33.3% ± 1.4 | 17.1% | 35.0% |
| Fav ML + Dog covers + Under | 8.1% ± 0.8 | 16.4% | 6.7% |
| Fav covers + Over | 24.1% ± 1.3 | 25.1% | 25.1% |

**Reading it.** The naive product is badly wrong whenever legs share the margin dimension, and the model
fixes most of that error, out of sample too. Margin/total dependence is small: the selected
`league_constant` model sets it to zero (spread × total then equals the naive price), and the richer
models that estimate it do not price better in either period. Empirically, Corr(favorite margin
residual, total residual) by favorite size (train | test) is +0.03 ± 0.03 | −0.07 ± 0.06 (0–2.5),
+0.04 ± 0.02 | +0.07 ± 0.04 (3–6.5), +0.02 ± 0.04 | −0.02 ± 0.08 (7–9.5), and
**+0.18 ± 0.04 | +0.10 ± 0.09** for 10+ point favorites. That is the only bucket where the dependence
is clearly non-zero, and it is too narrow a slice to move combo-level scores.

### 4.3 Sensitivity to correlation

Scaling the dependence in `mean_linear` by `c` (`ρ → cρ`, variance asymmetry → c×) on spread × total and
ML × total combos:

| c | 0 | 0.5 | 1 | 1.5 | 2 |
|---|---|---|---|---|---|
| train skill vs naive | −0.06% | −0.06% | −0.09% | −0.15% | −0.24% |
| **test skill vs naive** | **−0.00%** | **−0.01%** | **−0.03%** | **−0.06%** | **−0.11%** |
| test edge P&L (units, 1-cent threshold) | +5.8 | +22.8 | +15.2 | +2.4 | −12.5 |
| test max downswing | 22.5 | 21.3 | 30.9 | 34.3 | 40.4 |

Scores degrade gradually and monotonically as modeled dependence is inflated, with the same shape in
train and test. At c = 0 spread × total reproduces the naive price exactly (tested). The negative skill
at c = 0 comes from the ML × total moneyline-marginal error (§4.4), not from dependence. Legs on the
*same* dimension (ML × spread) stay fully dependent at every `c`; that structural dependence is where
the headline gain comes from.

### 4.4 Known model errors (ranked; next steps)

1. **Margin shape: key numbers.** NFL margins bunch on 3 and 7. A normal margin puts too little mass on
   "favorite wins by less than the spread" (test: 16.9% realized vs 13.4% model) and too much on
   "favorite covers given it wins" (lift over independence: test model +18.8 pp vs empirical
   +17.0 ± 1.5; train model +18.6 pp vs empirical +15.7 ± 0.8). Fix: a discrete margin distribution with key-number mass (e.g. an empirical
   margin PMF conditioned on the spread) in place of the normal margin, keeping the total dimension
   Gaussian.
2. **Moneyline over-identification.** The model's favorite-win probability, derived from the
   spread + total calibration, misses the market moneyline by 2.4 points on average in train (Brier
   0.2122 vs the market price's 0.2114) and 1.7 points in test (0.2099 vs 0.2100). That is why ML × total
   is slightly worse than naive in train: the error is in the marginal, not the dependence. Fix:
   calibrate to all three prices (least squares on `μ_h, μ_a`, or a margin-shape parameter), or quote
   ML combos off the market ML marginal with the model's conditional.
3. **Big-favorite dependence.** Corr(margin, total) for 10+ point favorites (+0.18 train) is not
   modeled by the selected model. Candidate: a `|spread|`-dependent variance asymmetry, validated on
   train only.
4. **Tails.** Calibration buckets (test): 0–10% predicted 5.3% vs realized 5.9% ± 0.5; 50%+ predicted
   51.9% vs 50.0% ± 1.6 (train: 5.2% vs 6.0% ± 0.3; 52.0% vs 48.5% ± 0.9). Gaussian tails are thin, as
   issue #2 anticipated; this mainly affects 3-leg combos.
5. **Team variance factors hurt** (spread × total skill −0.8% train, −1.1% test). They stay in the
   params file for diagnostics only.

### 4.5 Caveats

- Closing lines are the sharpest available prices; live RFQs arrive earlier, with noisier legs.
- The edge P&L is stylized: real books price some same-game correlation, charge vig, and RFQ flow
  selects against the quoter.
- Pushed combos are dropped pending Polymarket's settlement rule; ties void moneyline legs the same way.
- 2006 and 2008 are missing some closing prices in nflverse: spread/total fall back to −110 both sides,
  and ML combos are skipped for those games.
- The earlier (pre-split) version of this analysis chose the default model after seeing 2010–2025
  results. The numbers above replace it: settings now come from train-only tuning.

## 5. Module map

| File | Role |
|---|---|
| `combo_mm/nfl/ingest.py` | nflverse `games.csv` pull, dated raw cache + SHA-256 manifest, parsing (franchise relocations mapped), validation |
| `combo_mm/nfl/estimate.py` | walk-forward covariance estimator, variance models, shrinkage |
| `combo_mm/nfl/params_io.py` | params file schema, canonical writer, validating loader, `matchup_covariance` (stdlib) |
| `combo_mm/nfl/joint.py` | canonical legs, push-conditioned joint probability, market-implied mean solver |
| `combo_mm/nfl/synthetic_backtest.py` | same-game combo universe, walk-forward backtest, summary tables |
| `combo_mm/nfl/tuning.py`, `scripts/nfl_tune.py` | train-only hyperparameter grid search → `params/estimator.json` |
| `combo_mm/nfl/refresh.py`, `scripts/refresh_params.py` | weekly refresh + gates |
| `scripts/nfl_backtest.py` | backtest CLI (parallel across seasons) |
| `dashboard/nfl_tab.py` | Streamlit "NFL correlation" tab |

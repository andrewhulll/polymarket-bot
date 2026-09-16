# NFL same-game correlation model (Phase A)

How the bot prices a **same-game combo** — one side of two or three of a game's moneyline, spread and
total markets (e.g. *Chiefs ML + Chiefs −6.5 + over*, *Chiefs ML + opponent +6.5*) — at its joint
probability instead of the naive product of leg prices, how the correlation parameters are estimated,
and what 16 seasons of walk-forward backtesting say about it. Issues: #2 (model), #6 (data pipeline).

Everything below is reproducible:

```bash
pip install -r requirements-nfl.txt
python scripts/nfl_backtest.py --pull        # nflverse pull + walk-forward backtest -> results/nfl_backtest/
python scripts/refresh_params.py             # weekly params file + gates -> params/
streamlit run dashboard/app.py               # "NFL correlation" tab
```

Numbers quoted here are from the nflverse pull of 2026-09-16 (sha256 `0ac6de43583d…`), seasons
2010–2025 incl. playoffs: 4,358 games, 74,073 combos, 2,206 dropped for pushes.

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
   it; trailing 4 seasons plus the current season to date; weight `0.5^(age/2 seasons)`; at least 3
   prior seasons required.
3. **Variance model** (weighted least squares on the squared residuals):
   - `league_constant`: `σ² = a` — one league number.
   - `mean_linear`: `σ²(μ) = a + b·μ` — score variance grows with implied points (default).
   - `mean_linear_team`: `mean_linear` × shrunk per-team offensive × opponent defensive variance factors.
4. **Within-game `ρ`**: weighted correlation of standardized residuals `e/σ(μ)`, league-wide only.
5. **Shrinkage** for team factors and team scoring means: prior = previous seasons in the window,
   regressed halfway to the league value (offseason roll-forward) with a pseudo-count; current season
   blended in with weight `n/(n+k)`. Means use `k = 5` (weeks 1–4 mostly prior, mostly data by week
   8). Variance factors use `k = 15` and cap each game's squared-residual ratio at 9 (3σ): one squared
   residual is χ²(1) noise, and with `k = 5` a single week-1 blowout pushed a team's factor to the cap.

Walk-forward estimates over 2010–2025 (341 weekly fits): `σ` at league-average points 9.38 (range
9.02–9.81); slope `b` mean 1.59, SD 0.86; `ρ` mean +0.006, SD 0.034. The slope is statistically hard
— the squared residual has SD ≈ √2·σ² while implied points span only a few SDs — so its standard
error on a 4-season window is ≈ 0.6.

Empirical residual SD by implied team points confirms the direction of `mean_linear`:

| implied points | ≤15 | 15–18 | 18–21 | 21–24 | 24–27 | 27–30 | 30+ |
|---|---|---|---|---|---|---|---|
| empirical σ | 8.24 | 8.94 | 9.00 | 9.40 | 9.43 | 9.70 | 10.21 |
| model σ (walk-forward) | 8.71 | 8.90 | 9.13 | 9.38 | 9.60 | 9.79 | 9.95 |

## 3. Weekly parameter files (the hot-path contract)

`scripts/refresh_params.py` (Wednesdays) → `params/nfl_<season>_w<ww>.json`: league variance
function and `ρ`, per-team factors and means, and per-game `σ_h, σ_a, ρ, Var(M), Var(T), Corr(M,T)`
for the slate at current lines, plus data vintage (pull date + SHA-256) and estimator config.
Canonical JSON (sorted keys, 6 dp) — identical inputs give identical bytes. The pricer only needs
`params_io.load_params` + `matchup_covariance(params, home, away, μ_h, μ_a)`, which are stdlib-only.

Promotion gates (refuses to overwrite the current file on failure):

1. **Range sanity** — schema, finite values, `σ` in bounds, `|ρ| < 1`, positive variances.
2. **No regression** — Brier on the last 272 games' spread × total combos must not exceed the previous
   file's (or naive on the first run) by more than 0.001. In-sample: a guard against broken refreshes,
   not an evaluation.
3. **Determinism** — re-estimation reproduces the staged bytes.

Offseason (no unplayed games): nothing is written; the last file stays current.

## 4. Backtest results (walk-forward, 2010–2025)

`scripts/nfl_backtest.py`: per game, walk-forward params → market-calibrated means → all 17 same-game
combos priced by the naive product, each variance model, and the primary model with correlation
scaled by `c ∈ {0, 0.5, 1, 1.5, 2}`. Brier differences use game-clustered standard errors (all combos
in one game share its outcome).

### 4.1 Headline

| | Brier | skill vs naive | t |
|---|---|---|---|
| naive product | 0.17576 | — | — |
| league_constant | 0.16782 | 4.52% | −22.9 |
| **mean_linear** | 0.16784 | 4.51% | −22.6 |
| mean_linear_team | 0.16822 | 4.29% | −20.6 |

Excluding nested combos (one leg implies another): skill 2.44%, t −13.9.

### 4.2 By family — where the value is

| family | n | mean_linear skill | t |
|---|---|---|---|
| ML × spread | 12,705 | **13.3%** | −23.4 |
| ML × spread × total | 25,146 | **5.4%** | −23.1 |
| spread × total | 16,816 | −0.01% | +0.2 (tie) |
| ML × total | 17,200 | −0.15% | +2.5 (slightly worse) |

Selected combos (realized ± SE vs naive vs model):

| combo | realized | naive | model |
|---|---|---|---|
| Fav ML + Dog covers ("fav wins, doesn't cover") | 16.7% ± 0.6 | 33.3% | 13.3% |
| Fav ML + Fav covers | 48.9% ± 0.8 | 33.1% | 51.3% |
| Dog ML + Dog covers | 34.5% ± 0.7 | 16.8% | 35.3% |
| Fav ML + Dog covers + Under | 8.2% ± 0.4 | 16.6% | 6.8% |
| Fav covers + Over | 24.1% ± 0.7 | 25.0% | 25.8% |

**Reading it.** The naive product is badly wrong whenever legs share the margin dimension, and the
model fixes most of that error. On spread × total the model ties the naive price, because NFL
margin/total dependence is small on average: empirical Corr(favorite margin residual, total residual)
is −0.00 ± 0.03 (spread 0–2.5), +0.04 ± 0.02 (3–6.5), +0.01 ± 0.04 (7–9.5) and **+0.18 ± 0.04 for
10+ point favorites** vs model +0.02 / +0.04 / +0.07 / +0.10. So the model gets the sign and the
growth with favorite size right, but understates the big-favorite effect and overstates it for 7–9.5.

### 4.3 Sensitivity to correlation

Scaling `c` (`ρ → cρ`, variance asymmetry → c×) on spread × total combos: Brier skill +0.01%
(c = 0.5), −0.01% (c = 1), −0.05% (c = 1.5), −0.13% (c = 2; Brier worse by 0.00024, t ≈ 1.5). At
c = 0 the model reproduces the naive price exactly (tested). P&L against a counterparty quoting the
naive product (1-cent threshold) peaks at c = 1 (+64.5 units over 4,218 trades, t = 2.3), with max
downswing growing with `c` (10.5 at c = 0.5 → 35.0 at c = 2). Degradation is gradual, not
discontinuous: scoring gets monotonically (but only weakly significantly) worse as the fitted
correlation is inflated, and halving it is indistinguishable from the fit. Legs on
the same dimension (ML × spread) stay fully dependent at every `c` — that structural dependence is
where the headline gain comes from, not from the tunable part.

### 4.4 Known model errors (ranked; next steps)

1. **Margin shape: key numbers.** NFL margins bunch on 3 and 7. A normal margin puts too little mass
   on "favorite wins by less than the spread" (16.7% realized vs 13.3% model) and too much on
   "favorite covers given it wins". Fix: a discrete margin distribution with key-number mass (e.g. an
   empirical margin PMF conditioned on the spread) in place of the normal margin, keeping the total
   dimension Gaussian.
2. **Moneyline over-identification.** The model's favorite-win probability, derived from the
   spread + total calibration, misses the market moneyline by 2.2 points on average (Brier 0.2120 vs
   the market price's 0.2111). This is why ML × total is slightly worse than naive — the error is in
   the marginal, not the dependence. Fix: calibrate to all three prices (least squares on
   `μ_h, μ_a`, or a margin-shape parameter), or quote ML combos off the market ML marginal with the
   model's conditional.
3. **Big-favorite dependence.** Corr(margin, total) for 10+ point favorites is under-modeled
   (0.18 vs 0.10). Candidate: let the variance slope, or `ρ`, depend on `|spread|`.
4. **Tails.** Calibration buckets: 0–10% predicted 5.3% vs realized 6.1% ± 0.2; 50%+ predicted 51.9%
   vs 48.8% ± 0.9. Gaussian tails are thin, as issue #2 anticipated; this mainly affects 3-leg combos.
5. **Team variance factors don't help** (skill −0.36% on spread × total, t = 3.3). Keep them in the
   file for diagnostics; default to `mean_linear`.

### 4.5 Caveats

- Closing lines are the sharpest available prices; live RFQs arrive earlier, with noisier legs.
- The edge P&L is stylized: real books price some same-game correlation, charge vig, and RFQ flow
  selects against the quoter.
- Pushed combos are dropped pending Polymarket's settlement rule; ties void moneyline legs the same way.
- Before 2006 nflverse has no closing prices (spread/total fall back to −110 both sides and ML combos
  are skipped); the default backtest starts in 2010.

## 5. Module map

| File | Role |
|---|---|
| `combo_mm/nfl/ingest.py` | nflverse `games.csv` pull, dated raw cache + SHA-256 manifest, parsing (franchise relocations mapped), validation |
| `combo_mm/nfl/estimate.py` | walk-forward covariance estimator, variance models, shrinkage |
| `combo_mm/nfl/params_io.py` | params file schema, canonical writer, validating loader, `matchup_covariance` (stdlib) |
| `combo_mm/nfl/joint.py` | canonical legs, push-conditioned joint probability, market-implied mean solver |
| `combo_mm/nfl/synthetic_backtest.py` | same-game combo universe, walk-forward backtest, summary tables |
| `combo_mm/nfl/refresh.py`, `scripts/refresh_params.py` | weekly refresh + gates |
| `scripts/nfl_backtest.py` | backtest CLI (parallel across seasons) |
| `dashboard/nfl_tab.py` | Streamlit "NFL correlation" tab |

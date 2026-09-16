"""Walk-forward same-game combo backtest: correlation model vs naive product vs realized.

For every historical game in the test seasons:

1. **Params, walk-forward.** Estimate covariance params with games strictly
   before the game's (season, week) -- one estimation per week per variance
   model. No future information.
2. **Market marginals.** De-vig the closing spread / total / moneyline prices
   (``-110``/``-110`` => 0.5 when a spread/total price is missing).
3. **Location.** Solve ``(mu_home, mu_away)`` so the model's spread and total
   marginals equal the market's (:func:`combo_mm.nfl.joint.calibrate_means`).
   Model and naive therefore agree on every spread/total leg; any difference
   in a spread x total combo price is *dependence*, not a different view of
   the legs.
4. **Same-game combos.** One side of any two or three of the game's markets
   (moneyline, spread, total) from the favorite's perspective -- "Chiefs ML +
   Chiefs -6.5 + over", "Chiefs ML + opponent +6.5", ... -- priced three ways:
   naive product of market marginals, the model joint probability, and the
   realized payout. Combos where any leg pushes are dropped (settlement rule
   TBD) and counted.

Outputs: a per-combo table, a per-game structure table (residuals, model
covariance, binary outcomes, pairwise model joints) and summary tables:
scores (Brier / log loss / skill vs naive with game-clustered standard
errors), results by combo / family / combo size / spread bucket, calibration
buckets, sensitivity to correlation (``corr_scale`` 0 -> 2, where 0 removes
all modeled dependence beyond the legs' shared scores), correlation
structure, and a stylized "edge vs a naive counterparty" P&L.

Moneyline legs are over-identified: the model's moneyline marginal comes from
the spread/total-calibrated means and can differ from the market price, so
moneyline combos mix dependence with marginal error -- reported separately.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from combo_mm.nfl import MODEL_VERSION
from combo_mm.nfl.estimate import EstimatorConfig, InsufficientHistory, ResidualTable, estimate_params, implied_means
from combo_mm.nfl.ingest import Game, devig_pair
from combo_mm.nfl.joint import (
    PUSH,
    WIN,
    GameModel,
    Leg,
    away_cover,
    away_ml,
    away_team_over,
    calibrate_means,
    home_cover,
    home_ml,
    home_team_over,
    over,
    settle_leg,
    under,
)
from combo_mm.nfl.params_io import VARIANCE_MODELS, matchup_covariance

__all__ = [
    "MARKETS",
    "COMBOS",
    "OUTCOMES",
    "SPREAD_BUCKETS",
    "BacktestConfig",
    "BacktestOutput",
    "run_backtest",
    "write_outputs",
    "load_outputs",
    "model_col",
    "scale_col",
    "prob_columns",
    "score_table",
    "group_table",
    "calibration_table",
    "edge_pnl",
    "pnl_stats",
    "sensitivity_table",
    "add_spread_bucket",
    "spread_bucket_structure",
    "sigma_vs_mu",
    "season_structure",
    "outcome_lift",
    "moneyline_consistency",
    "evaluate_params_on_games",
]

# Same-game combo universe: one side of any 2 or 3 of the game's markets, from
# the favorite's perspective. Logically impossible combos (underdog wins AND
# favorite covers) are excluded.
MARKETS: Tuple[Tuple[str, Tuple[str, str]], ...] = (
    ("moneyline", ("fav_ml", "dog_ml")),
    ("spread", ("fav_cover", "dog_cover")),
    ("total", ("over", "under")),
)
_FAMILY_LABEL = {"moneyline": "ML", "spread": "spread", "total": "total"}
# (a, b): a winning implies b wins, so the combo is priced by its stronger leg.
NESTED_PAIRS = (("fav_cover", "fav_ml"), ("dog_ml", "dog_cover"))
IMPOSSIBLE_PAIRS = (("dog_ml", "fav_cover"),)


def _build_combos() -> Dict[str, Tuple[Tuple[str, ...], str, bool]]:
    combos: Dict[str, Tuple[Tuple[str, ...], str, bool]] = {}
    for size in (2, 3):
        for markets in itertools.combinations(MARKETS, size):
            for sides in itertools.product(*(m[1] for m in markets)):
                keys = set(sides)
                if any(a in keys and b in keys for a, b in IMPOSSIBLE_PAIRS):
                    continue
                nested = any(a in keys and b in keys for a, b in NESTED_PAIRS)
                family = " x ".join(_FAMILY_LABEL[m[0]] for m in markets)
                combos["+".join(sides)] = (tuple(sides), family, nested)
    return combos


# name -> (leg keys, family, nested). Leg keys resolve per game against the favorite.
COMBOS: Dict[str, Tuple[Tuple[str, ...], str, bool]] = _build_combos()

# Binary outcomes for the correlation-structure view (team totals use a
# half-point line at the closing-line implied team points).
OUTCOMES = ("fav_win", "fav_cover", "over", "fav_team_over", "dog_team_over")

SPREAD_BUCKETS = ((0.0, 3.0, "0-2.5"), (3.0, 7.0, "3-6.5"), (7.0, 10.0, "7-9.5"), (10.0, 99.0, "10+"))

DEFAULT_SCALES = (0.0, 0.5, 1.0, 1.5, 2.0)


def _pd():
    import pandas as pd  # offline dependency, imported lazily
    return pd


def _np():
    import numpy as np
    return np


@dataclass(frozen=True)
class BacktestConfig:
    first_season: int = 2010
    last_season: int = 2025
    variance_models: Tuple[str, ...] = VARIANCE_MODELS
    primary_model: str = "mean_linear"
    corr_scales: Tuple[float, ...] = DEFAULT_SCALES
    edge_threshold: float = 0.01
    include_playoffs: bool = True
    estimator: EstimatorConfig = field(default_factory=EstimatorConfig)

    def __post_init__(self) -> None:
        if self.primary_model not in self.variance_models:
            raise ValueError("primary_model must be one of variance_models")
        if 1.0 not in self.corr_scales:
            raise ValueError("corr_scales must include 1.0 (the fitted model)")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["variance_models"] = list(self.variance_models)
        d["corr_scales"] = list(self.corr_scales)
        return d


@dataclass
class BacktestOutput:
    combos: Any                 # pandas DataFrame, one row per (game, combo)
    games: Any                  # pandas DataFrame, one row per game
    params_history: Any         # pandas DataFrame, league params per (season, week, model)
    meta: Dict[str, Any]


# ---------------------------------------------------------------------------
# Column naming
# ---------------------------------------------------------------------------

def model_col(model: str) -> str:
    return f"p_{model}"


def scale_col(scale: float) -> str:
    return f"p_scale_{scale:g}"


def prob_columns(config: BacktestConfig) -> List[str]:
    cols = [model_col(m) for m in config.variance_models]
    cols += [scale_col(c) for c in config.corr_scales if c != 1.0]
    return cols


# ---------------------------------------------------------------------------
# Per-game pricing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _MarketView:
    fav_is_home: bool
    p_home_cover: float
    p_over: float
    p_home_ml: Optional[float]
    odds_fallback: bool


def _market(game: Game) -> _MarketView:
    p_cover = devig_pair(game.home_spread_odds, game.away_spread_odds)
    p_over = devig_pair(game.over_odds, game.under_odds)
    p_ml = devig_pair(game.home_moneyline, game.away_moneyline)
    return _MarketView(
        fav_is_home=game.spread_line > 0,
        p_home_cover=0.5 if p_cover is None else p_cover,
        p_over=0.5 if p_over is None else p_over,
        p_home_ml=p_ml,
        odds_fallback=p_cover is None or p_over is None,
    )


def _legs_for(game: Game, mkt: _MarketView) -> Dict[str, Tuple[Leg, Optional[float]]]:
    """Leg key -> (canonical leg, de-vigged market probability or None)."""
    s, u = game.spread_line, game.total_line
    fh = mkt.fav_is_home
    p_fav_cover = mkt.p_home_cover if fh else 1.0 - mkt.p_home_cover
    p_fav_ml = None if mkt.p_home_ml is None else (mkt.p_home_ml if fh else 1.0 - mkt.p_home_ml)
    mu_h, mu_a = implied_means(s, u)
    mu_fav, mu_dog = (mu_h, mu_a) if fh else (mu_a, mu_h)
    fav_tt_line = math.floor(mu_fav) + 0.5
    dog_tt_line = math.floor(mu_dog) + 0.5
    return {
        "fav_cover": (home_cover(s) if fh else away_cover(s), p_fav_cover),
        "dog_cover": (away_cover(s) if fh else home_cover(s), 1.0 - p_fav_cover),
        "over": (over(u), mkt.p_over),
        "under": (under(u), 1.0 - mkt.p_over),
        "fav_ml": (home_ml() if fh else away_ml(), p_fav_ml),
        "dog_ml": (away_ml() if fh else home_ml(), None if p_fav_ml is None else 1.0 - p_fav_ml),
        "fav_win": (home_ml() if fh else away_ml(), p_fav_ml),
        "fav_team_over": (home_team_over(fav_tt_line) if fh else away_team_over(fav_tt_line), None),
        "dog_team_over": (away_team_over(dog_tt_line) if fh else home_team_over(dog_tt_line), None),
    }


def _combo_realized(legs: Sequence[Leg], game: Game) -> Optional[int]:
    results = [settle_leg(leg, game.home_score, game.away_score) for leg in legs]
    if PUSH in results:
        return None
    return int(all(r == WIN for r in results))


def _price_game(game: Game, params_by_model: Dict[str, Dict[str, Any]],
                config: BacktestConfig) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    mkt = _market(game)
    legs = _legs_for(game, mkt)
    fh = mkt.fav_is_home

    variants: List[Tuple[str, str, float]] = [(model_col(m), m, 1.0) for m in config.variance_models]
    variants += [(scale_col(c), config.primary_model, c) for c in config.corr_scales if c != 1.0]

    calibrated = {}
    for col, model, scale in variants:
        params = params_by_model[model]

        def cov_fn(mh: float, ma: float, params=params, scale=scale):
            return matchup_covariance(params, game.home, game.away, mh, ma, corr_scale=scale)

        calibrated[col] = calibrate_means(game.spread_line, mkt.p_home_cover,
                                          game.total_line, mkt.p_over, cov_fn)
    models = {col: GameModel((cal.mu_home, cal.mu_away), cal.cov) for col, cal in calibrated.items()}

    base = {
        "season": game.season, "week": game.week, "game_type": game.game_type,
        "gameday": game.gameday, "game_id": game.game_id,
        "home": game.home, "away": game.away,
        "fav": game.home if fh else game.away, "dog": game.away if fh else game.home,
        "spread_line": game.spread_line, "abs_spread": abs(game.spread_line),
        "total_line": game.total_line, "odds_fallback": mkt.odds_fallback,
    }

    combo_rows: List[Dict[str, Any]] = []
    for name, (keys, family, nested) in COMBOS.items():
        leg_objs = [legs[k][0] for k in keys]
        market_ps = [legs[k][1] for k in keys]
        if any(p is None for p in market_ps):
            continue  # no moneyline price (pre-2006): combo not constructible
        realized = _combo_realized(leg_objs, game)
        row = dict(base, combo=name, family=family, n_legs=len(keys), nested=nested,
                   pushed=realized is None, realized=realized,
                   naive=float(math.prod(market_ps)))  # type: ignore[arg-type]
        if realized is not None:
            for col, gm in models.items():
                row[col] = gm.joint(leg_objs)
        combo_rows.append(row)

    # Structure row (primary model, corr_scale 1).
    prim = calibrated[model_col(config.primary_model)]
    pmodel = models[model_col(config.primary_model)]
    mu_h, mu_a = implied_means(game.spread_line, game.total_line)
    e_h, e_a = game.home_score - mu_h, game.away_score - mu_a
    cov = prim.cov
    corr_mt_home = cov.corr_margin_total
    g_row = dict(base)
    g_row.update({
        "home_score": game.home_score, "away_score": game.away_score,
        "mu_home_line": mu_h, "mu_away_line": mu_a,
        "mu_fav_line": mu_h if fh else mu_a, "mu_dog_line": mu_a if fh else mu_h,
        "resid_home": e_h, "resid_away": e_a,
        "resid_fav": e_h if fh else e_a, "resid_dog": e_a if fh else e_h,
        "mu_home_cal": prim.mu_home, "mu_away_cal": prim.mu_away,
        "sigma_fav": cov.sigma_home if fh else cov.sigma_away,
        "sigma_dog": cov.sigma_away if fh else cov.sigma_home,
        "rho": cov.rho,
        "corr_mt_fav": corr_mt_home if fh else -corr_mt_home,
        "p_mkt_fav_ml": legs["fav_ml"][1],
        "calibration_iterations": prim.iterations,
    })
    for model in config.variance_models:
        g_row[f"p_fav_ml_{model}"] = models[model_col(model)].leg(legs["fav_ml"][0])
    for key in OUTCOMES:
        leg = legs[key][0]
        res = settle_leg(leg, game.home_score, game.away_score)
        g_row[f"y_{key}"] = None if res == PUSH else int(res == WIN)
        g_row[f"pm_{key}"] = pmodel.leg(leg)
    for i, a in enumerate(OUTCOMES):
        for b in OUTCOMES[i + 1:]:
            g_row[f"pj_{a}__{b}"] = pmodel.joint([legs[a][0], legs[b][0]])
    return combo_rows, g_row


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _target_games(games: Sequence[Game], config: BacktestConfig) -> List[Game]:
    out = []
    for g in games:
        if not (g.played and g.has_lines):
            continue
        if not config.first_season <= g.season <= config.last_season:
            continue
        if g.game_type != "REG" and not config.include_playoffs:
            continue
        if g.spread_line == 0:
            continue  # pick'em: no favorite
        out.append(g)
    return sorted(out, key=lambda g: (g.season, g.week, g.game_id))


def _with_model(cfg: EstimatorConfig, model: str) -> EstimatorConfig:
    d = cfg.to_dict()
    d["variance_model"] = model
    return EstimatorConfig(**d)


def _run_season(games: Sequence[Game], season: int, config: BacktestConfig) -> Dict[str, List[Any]]:
    """Walk-forward over one season's weeks. Top-level so it can run in a worker process."""
    table = ResidualTable.build(games)
    weeks: Dict[int, List[Game]] = {}
    for g in _target_games(games, config):
        if g.season == season:
            weeks.setdefault(g.week, []).append(g)
    out: Dict[str, List[Any]] = {"combos": [], "games": [], "history": [], "skipped": []}
    for week in sorted(weeks):
        try:
            params_by_model = {
                m: estimate_params(table, season, week, _with_model(config.estimator, m))
                for m in config.variance_models
            }
        except InsufficientHistory as exc:
            out["skipped"].append(str(exc))
            continue
        for m, p in params_by_model.items():
            lg = p["league"]
            out["history"].append({
                "season": season, "week": week, "model": m,
                "var_intercept": lg["var_intercept"], "var_slope": lg["var_slope"],
                "rho": lg["rho"], "sigma_at_mean_points": lg["sigma_at_mean_points"],
                "mean_points": lg["mean_points"], "n_games": lg["n_games"],
            })
        for g in weeks[week]:
            rows, g_row = _price_game(g, params_by_model, config)
            out["combos"].extend(rows)
            out["games"].append(g_row)
    return out


def run_backtest(games: Sequence[Game], config: Optional[BacktestConfig] = None,
                 data_vintage: Optional[Dict[str, Any]] = None,
                 progress: Optional[Callable[[int, int], None]] = None,
                 workers: Optional[int] = None) -> BacktestOutput:
    """Run the walk-forward backtest; seasons are independent and run in parallel.

    ``workers=1`` runs in-process (tests, debugging). Output is identical for
    any ``workers``: seasons are merged in order.
    """
    pd = _pd()
    config = config or BacktestConfig()
    started = time.time()
    games = list(games)
    seasons = sorted({g.season for g in _target_games(games, config)})
    workers = workers or max(1, min(len(seasons), (os.cpu_count() or 2) - 1))

    results: Dict[int, Dict[str, List[Any]]] = {}
    if workers <= 1 or len(seasons) <= 1:
        for i, season in enumerate(seasons):
            results[season] = _run_season(games, season, config)
            if progress:
                progress(i + 1, len(seasons))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {season: pool.submit(_run_season, games, season, config) for season in seasons}
            for i, season in enumerate(seasons):
                results[season] = futures[season].result()
                if progress:
                    progress(i + 1, len(seasons))

    combos = pd.DataFrame([r for s in seasons for r in results[s]["combos"]])
    n_pickem = sum(1 for g in games if g.played and g.has_lines and g.spread_line == 0
                   and config.first_season <= g.season <= config.last_season)
    meta = {
        "model_version": MODEL_VERSION,
        "generated_at_unix": int(time.time()),
        "runtime_s": round(time.time() - started, 1),
        "workers": workers,
        "config": config.to_dict(),
        "data_vintage": dict(data_vintage or {}),
        "seasons": seasons,
        "n_games": sum(len(results[s]["games"]) for s in seasons),
        "n_pickem_skipped": n_pickem,
        "n_combo_rows": int(len(combos)),
        "n_pushed": int(combos["pushed"].sum()) if len(combos) else 0,
        "skipped_weeks": [r for s in seasons for r in results[s]["skipped"]],
        "prob_columns": prob_columns(config),
        "combo_universe": {name: {"legs": list(k), "family": f, "nested": n}
                           for name, (k, f, n) in COMBOS.items()},
    }
    return BacktestOutput(
        combos=combos,
        games=pd.DataFrame([r for s in seasons for r in results[s]["games"]]),
        params_history=pd.DataFrame([r for s in seasons for r in results[s]["history"]]),
        meta=meta,
    )


def write_outputs(out: BacktestOutput, directory: Path | str) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    out.combos.to_csv(directory / "combos.csv", index=False)
    out.games.to_csv(directory / "games.csv", index=False)
    out.params_history.to_csv(directory / "params_history.csv", index=False)
    (directory / "meta.json").write_text(json.dumps(out.meta, indent=2, sort_keys=True) + "\n")
    return directory


def load_outputs(directory: Path | str) -> Optional[BacktestOutput]:
    pd = _pd()
    directory = Path(directory)
    needed = ["combos.csv", "games.csv", "params_history.csv", "meta.json"]
    if not all((directory / n).exists() for n in needed):
        return None
    return BacktestOutput(
        combos=pd.read_csv(directory / "combos.csv"),
        games=pd.read_csv(directory / "games.csv"),
        params_history=pd.read_csv(directory / "params_history.csv"),
        meta=json.loads((directory / "meta.json").read_text()),
    )


# ---------------------------------------------------------------------------
# Summaries (take the output DataFrames; shared by the CLI and the dashboard)
# ---------------------------------------------------------------------------

_EPS = 1e-6


def _scored(df):
    return df[~df["pushed"].astype(bool)].copy()


def _logloss(np, p, y):
    p = np.clip(p, _EPS, 1 - _EPS)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def score_table(combos, columns: Sequence[str]):
    """Brier / log loss per probability column vs naive, with game-clustered SEs.

    ``brier_diff`` = mean(model Brier - naive Brier); negative is better.
    Combos of one game share its outcome, so the SE clusters by game.
    """
    np, pd = _np(), _pd()
    df = _scored(combos)
    if df.empty:
        return pd.DataFrame()
    y = df["realized"].astype(float).to_numpy()
    naive = df["naive"].to_numpy()
    b_naive = (naive - y) ** 2
    rows = [{"model": "naive", "n": len(df), "brier": b_naive.mean(), "log_loss": _logloss(np, naive, y).mean(),
             "brier_skill_vs_naive": 0.0, "brier_diff": 0.0, "brier_diff_se": 0.0, "t_stat": float("nan"),
             "mean_abs_gap_vs_naive": 0.0}]
    groups = df["game_id"].to_numpy()
    for col in columns:
        p = df[col].to_numpy()
        b = (p - y) ** 2
        d = b - b_naive
        mean_diff = d.mean()
        cluster_sums = pd.Series(d - mean_diff).groupby(groups).sum()
        se = float(np.sqrt((cluster_sums ** 2).sum()) / len(d)) if len(cluster_sums) > 1 else float("nan")
        rows.append({
            "model": col.removeprefix("p_"), "n": len(df), "brier": b.mean(),
            "log_loss": _logloss(np, p, y).mean(),
            "brier_skill_vs_naive": 1.0 - b.mean() / b_naive.mean(),
            "brier_diff": mean_diff, "brier_diff_se": se,
            "t_stat": mean_diff / se if se and se > 0 else float("nan"),
            "mean_abs_gap_vs_naive": np.abs(p - naive).mean(),
        })
    return pd.DataFrame(rows)


def group_table(combos, by, model: str):
    """Realized vs naive vs model hit rates and Brier by grouping column(s)."""
    np, pd = _np(), _pd()
    df = _scored(combos)
    if df.empty:
        return pd.DataFrame()
    df["y"] = df["realized"].astype(float)
    df["b_naive"] = (df["naive"] - df["y"]) ** 2
    df["b_model"] = (df[model] - df["y"]) ** 2
    g = df.groupby(by).agg(
        n=("y", "size"), realized=("y", "mean"), naive=("naive", "mean"),
        model=(model, "mean"), brier_naive=("b_naive", "mean"), brier_model=("b_model", "mean"),
    )
    g["realized_minus_naive"] = g["realized"] - g["naive"]
    g["model_minus_naive"] = g["model"] - g["naive"]
    g["realized_se"] = np.sqrt(g["realized"] * (1 - g["realized"]) / g["n"])
    g["brier_skill"] = 1.0 - g["brier_model"] / g["brier_naive"]
    g["pushed"] = combos.groupby(by)["pushed"].sum().reindex(g.index).fillna(0).astype(int)
    return g.reset_index()


def calibration_table(combos, column: str, bin_width: float = 0.05):
    """Reliability buckets: mean predicted vs realized per ``bin_width``."""
    np, pd = _np(), _pd()
    df = _scored(combos)
    if df.empty:
        return pd.DataFrame()
    p = df[column].to_numpy()
    n_bins = int(round(1 / bin_width))
    bins = np.minimum((p / bin_width).astype(int), n_bins - 1)
    t = pd.DataFrame({"bin": bins, "p": p, "y": df["realized"].astype(float).to_numpy()})
    g = t.groupby("bin").agg(n=("y", "size"), predicted=("p", "mean"), realized=("y", "mean")).reset_index()
    g["bin_lo"] = g["bin"] * bin_width
    g["bin_hi"] = g["bin_lo"] + bin_width
    g["realized_se"] = np.sqrt(g["realized"] * (1 - g["realized"]) / g["n"])
    g["source"] = "naive" if column == "naive" else column.removeprefix("p_")
    return g.drop(columns="bin")


def edge_pnl(combos, column: str, threshold: float = 0.01):
    """Stylized P&L vs a counterparty that prices combos at the naive product.

    Buy one $1-payout combo at the naive price when ``model - naive >
    threshold``; sell one when ``naive - model > threshold``. Chronological.
    This isolates the value of modeling dependence -- it is **not** a live
    P&L forecast (real books price some same-game correlation, charge vig,
    and select against you).
    """
    np = _np()
    df = _scored(combos).sort_values(["gameday", "game_id", "combo"]).reset_index(drop=True)
    gap = df[column] - df["naive"]
    df["side"] = np.where(gap > threshold, 1, np.where(gap < -threshold, -1, 0))
    df["pnl"] = df["side"] * (df["realized"].astype(float) - df["naive"])
    df["cum_pnl"] = df["pnl"].cumsum()
    return df[["season", "week", "gameday", "game_id", "combo", "family", "n_legs", "nested",
               "naive", column, "realized", "side", "pnl", "cum_pnl"]]


def _swings(np, cum) -> Tuple[float, float]:
    if len(cum) == 0:
        return 0.0, 0.0
    c = np.concatenate([[0.0], np.asarray(cum, dtype=float)])
    down = float((c - np.maximum.accumulate(c)).min())
    up = float((c - np.minimum.accumulate(c)).max())
    return -down, up


def pnl_stats(trades) -> Dict[str, float]:
    np = _np()
    t = trades[trades["side"] != 0]
    down, up = _swings(np, trades["cum_pnl"].to_numpy())
    n = len(t)
    sd = float(t["pnl"].std(ddof=1)) if n > 1 else 0.0
    return {
        "n_trades": n,
        "total_pnl": float(t["pnl"].sum()),
        "pnl_per_trade": float(t["pnl"].mean()) if n else 0.0,
        "pnl_t_stat": float(t["pnl"].mean() / (sd / math.sqrt(n))) if n > 2 and sd > 0 else float("nan"),
        "win_rate": float((t["pnl"] > 0).mean()) if n else 0.0,
        "max_downswing": down,
        "max_upswing": up,
    }


def sensitivity_table(combos, corr_scales: Sequence[float], primary_model: str,
                      threshold: float = 0.01):
    """Scores and edge P&L as the correlation assumption is scaled."""
    pd = _pd()
    rows = []
    for c in sorted(corr_scales):
        col = model_col(primary_model) if c == 1.0 else scale_col(c)
        if col not in combos.columns:
            continue
        sc = score_table(combos, [col]).iloc[-1]
        stats = pnl_stats(edge_pnl(combos, col, threshold))
        rows.append({"corr_scale": c, "column": col, "brier": sc["brier"], "log_loss": sc["log_loss"],
                     "brier_skill_vs_naive": sc["brier_skill_vs_naive"], "brier_diff_se": sc["brier_diff_se"],
                     "mean_abs_gap_vs_naive": sc["mean_abs_gap_vs_naive"], **stats})
    return pd.DataFrame(rows)


def _bucket(abs_spread: float) -> str:
    for lo, hi, label in SPREAD_BUCKETS:
        if lo <= abs_spread < hi:
            return label
    return SPREAD_BUCKETS[-1][2]


def add_spread_bucket(df):
    df = df.copy()
    df["spread_bucket"] = df["abs_spread"].map(_bucket)
    return df


def spread_bucket_structure(games):
    """Empirical vs model Corr(favorite margin, total) and score SDs by favorite size."""
    np, pd = _np(), _pd()
    g = add_spread_bucket(games)
    rows = []
    for _, _, label in SPREAD_BUCKETS:
        s = g[g["spread_bucket"] == label]
        if len(s) < 3:
            continue
        m = s["resid_fav"] - s["resid_dog"]
        t = s["resid_fav"] + s["resid_dog"]
        r = float(np.corrcoef(m, t)[0, 1])
        n = len(s)
        rows.append({
            "spread_bucket": label, "n_games": n,
            "emp_corr_margin_total": r,
            "emp_corr_se": (1 - r * r) / math.sqrt(max(n - 1, 1)),
            "model_corr_margin_total": float(s["corr_mt_fav"].mean()),
            "emp_sigma_fav": float(s["resid_fav"].std()), "emp_sigma_dog": float(s["resid_dog"].std()),
            "model_sigma_fav": float(s["sigma_fav"].mean()), "model_sigma_dog": float(s["sigma_dog"].mean()),
        })
    return pd.DataFrame(rows)


def sigma_vs_mu(games, bin_edges: Sequence[float] = (0, 15, 18, 21, 24, 27, 30, 60)):
    """Residual SD by closing-line implied team points: empirical vs model."""
    np, pd = _np(), _pd()
    stacked = pd.DataFrame({
        "mu": np.concatenate([games["mu_fav_line"], games["mu_dog_line"]]),
        "resid": np.concatenate([games["resid_fav"], games["resid_dog"]]),
        "model_sigma": np.concatenate([games["sigma_fav"], games["sigma_dog"]]),
    })
    stacked["bin"] = pd.cut(stacked["mu"], list(bin_edges))
    g = stacked.groupby("bin", observed=True).agg(
        n=("resid", "size"), mu=("mu", "mean"), emp_sigma=("resid", "std"), model_sigma=("model_sigma", "mean"))
    g["emp_sigma_se"] = g["emp_sigma"] / np.sqrt(2 * (g["n"] - 1).clip(lower=1))
    g = g.reset_index()
    g["bin"] = g["bin"].astype(str)
    return g


def season_structure(games):
    """Per-season empirical vs model rho, sigma and Corr(fav margin, total)."""
    np, pd = _np(), _pd()
    rows = []
    for season, s in games.groupby("season"):
        rows.append({
            "season": int(season), "n_games": len(s),
            "emp_rho": float(np.corrcoef(s["resid_home"], s["resid_away"])[0, 1]),
            "model_rho": float(s["rho"].mean()),
            "emp_sigma": float(np.concatenate([s["resid_home"], s["resid_away"]]).std(ddof=1)),
            "model_sigma": float(np.concatenate([s["sigma_fav"], s["sigma_dog"]]).mean()),
            "emp_corr_mt_fav": float(np.corrcoef(s["resid_fav"] - s["resid_dog"],
                                                 s["resid_fav"] + s["resid_dog"])[0, 1]),
            "model_corr_mt_fav": float(s["corr_mt_fav"].mean()),
        })
    return pd.DataFrame(rows)


def outcome_lift(games):
    """Pairwise joint-hit "lift" over independence, empirical vs model.

    For outcomes A, B (pushes excluded pairwise), with ``pA, pB`` the model's
    market-calibrated marginals:
    ``emp_lift = mean(1[A and B]) - mean(pA * pB)`` and
    ``model_lift = mean(P(A and B)) - mean(pA * pB)``.
    Positive = the outcomes co-occur more often than independence implies.
    """
    np, pd = _np(), _pd()
    rows = []
    for i, a in enumerate(OUTCOMES):
        for b in OUTCOMES[i + 1:]:
            s = games.dropna(subset=[f"y_{a}", f"y_{b}"])
            ya, yb = s[f"y_{a}"].astype(float), s[f"y_{b}"].astype(float)
            indep = float((s[f"pm_{a}"] * s[f"pm_{b}"]).mean())
            both = ya * yb
            model_joint = float(s[f"pj_{a}__{b}"].mean())
            rows.append({
                "a": a, "b": b, "n": len(s),
                "emp_joint": float(both.mean()), "model_joint": model_joint, "independent": indep,
                "emp_lift": float(both.mean()) - indep, "model_lift": model_joint - indep,
                "emp_lift_se": float(both.std(ddof=1) / math.sqrt(len(s))) if len(s) > 1 else float("nan"),
                "emp_phi": float(np.corrcoef(ya, yb)[0, 1]) if len(s) > 2 else float("nan"),
            })
    return pd.DataFrame(rows)


def moneyline_consistency(games, models: Sequence[str]):
    """Over-identification check: model ML marginal (from spread + total) vs market ML price."""
    pd = _pd()
    s = games.dropna(subset=["p_mkt_fav_ml", "y_fav_win"])
    y = s["y_fav_win"].astype(float)
    rows = [{"source": "market", "mean_p_fav_win": float(s["p_mkt_fav_ml"].mean()),
             "brier": float(((s["p_mkt_fav_ml"] - y) ** 2).mean()), "mean_abs_gap_vs_market": 0.0}]
    for m in models:
        col = f"p_fav_ml_{m}"
        rows.append({"source": m, "mean_p_fav_win": float(s[col].mean()),
                     "brier": float(((s[col] - y) ** 2).mean()),
                     "mean_abs_gap_vs_market": float((s[col] - s["p_mkt_fav_ml"]).abs().mean())})
    out = pd.DataFrame(rows)
    out.attrs["realized_fav_win_rate"] = float(y.mean()) if len(y) else float("nan")
    out.attrs["n"] = len(s)
    return out


# ---------------------------------------------------------------------------
# Refresh gate helper
# ---------------------------------------------------------------------------

GATE_COMBOS = ("fav_cover+over", "fav_cover+under", "dog_cover+over", "dog_cover+under")


def evaluate_params_on_games(params: Dict[str, Any], games: Sequence[Game],
                             combos: Sequence[str] = GATE_COMBOS) -> Dict[str, float]:
    """Brier of one params file vs naive on spread x total combos for ``games``.

    Used by the refresh no-regression gate. In-sample for the candidate
    (these games fed its estimation): a guard against broken refreshes, not
    an out-of-sample evaluation -- the walk-forward backtest is that.
    """
    b_model, b_naive, n = 0.0, 0.0, 0
    for g in games:
        if not (g.played and g.has_lines) or g.spread_line == 0:
            continue
        mkt = _market(g)
        legs = _legs_for(g, mkt)

        def cov_fn(mh: float, ma: float, g=g):
            return matchup_covariance(params, g.home, g.away, mh, ma)

        cal = calibrate_means(g.spread_line, mkt.p_home_cover, g.total_line, mkt.p_over, cov_fn)
        gm = GameModel((cal.mu_home, cal.mu_away), cal.cov)
        for name in combos:
            keys = COMBOS[name][0]
            leg_objs = [legs[k][0] for k in keys]
            realized = _combo_realized(leg_objs, g)
            if realized is None:
                continue
            naive = math.prod(legs[k][1] for k in keys)  # type: ignore[misc]
            p = gm.joint(leg_objs)
            b_model += (p - realized) ** 2
            b_naive += (naive - realized) ** 2
            n += 1
    if n == 0:
        return {"n": 0, "brier_model": float("nan"), "brier_naive": float("nan")}
    return {"n": n, "brier_model": b_model / n, "brier_naive": b_naive / n}

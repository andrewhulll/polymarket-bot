"""JSON API over the NFL backtest research views (no Streamlit).

Refactored from ``dashboard/nfl_tab.py``: the same data loading, filtering,
and Altair chart builders, but instead of drawing into Streamlit each view
returns a JSON-serializable dict::

    {"metrics": [...], "specs": [{"id", "title", "spec"}], "tables": {...}, "notes": [...]}

Altair charts compile to Vega-Lite dicts (``chart.to_dict()``); the browser
renders them with vega-embed, so every chart from the Streamlit tab survives
without being reimplemented. This module is only imported lazily by the
dashboard server (it needs pandas/altair). Nothing here quotes or trades.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import altair as alt
    import numpy as np
    import pandas as pd
    from combo_mm.nfl import synthetic_backtest as bt
    from combo_mm.nfl.ingest import devig_pair, latest_pull
    from combo_mm.nfl.joint import (
        GameModel, away_cover, away_ml, calibrate_means, home_cover, home_ml, over, under,
    )
    from combo_mm.nfl.params_io import (
        VARIANCE_MODELS, MatchupCovariance, list_params, load_params, matchup_covariance,
    )
    DEPS_OK = True
except ImportError:
    # The dashboard server must import (and serve its live tabs) without the
    # offline research stack. Anything below that needs it raises DepsMissing.
    DEPS_OK = False
    alt = np = pd = None
    bt = devig_pair = latest_pull = None
    GameModel = away_cover = away_ml = calibrate_means = None
    home_cover = home_ml = over = under = None
    VARIANCE_MODELS = MatchupCovariance = list_params = None
    load_params = matchup_covariance = None


class DepsMissing(RuntimeError):
    """The NFL research stack (pandas/altair/scipy) is not installed."""


def _require_deps() -> None:
    if not DEPS_OK:
        raise DepsMissing("nfl research deps unavailable")

REPO = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO / "results" / "nfl_backtest"
PARAMS_DIR = REPO / "params"
RAW_ROOT = REPO / "data" / "raw"

MODEL_LABELS = {
    "league_constant": "League constant sigma",
    "mean_linear": "sigma^2 grows with implied points",
    "mean_linear_team": "sigma^2 ~ implied points x team factors",
}
FAMILY_ORDER = ["ML x spread", "ML x total", "spread x total", "ML x spread x total"]
OUTCOME_LABELS = {
    "fav_win": "Fav wins", "fav_cover": "Fav covers", "over": "Over",
    "fav_team_over": "Fav team total over", "dog_team_over": "Dog team total over",
}
LEG_LABELS = {
    "fav_ml": "Fav ML", "dog_ml": "Dog ML", "fav_cover": "Fav covers", "dog_cover": "Dog covers",
    "over": "Over", "under": "Under",
}
C_NAIVE, C_MODEL, C_REAL = "#9aa0a6", "#1a73e8", "#e8710a"
HEADER = ("Offline research over historical NFL closing lines and scores (nflverse). "
          "History for shape, market for location: score covariance is estimated walk-forward "
          "from past games; each game's score means are solved so model leg prices equal the "
          "de-vigged closing prices. Any gap between the model and the naive product of leg "
          "prices is therefore dependence between legs. Nothing here quotes or trades.")

VIEWS = ["overview", "combo_pricing", "calibration", "structure", "sensitivity", "params"]

# Scripts the dashboard may run, mapped to argv (run via sys.executable, cwd=REPO).
RUN_ALLOWLIST = {
    "refresh_params": ["scripts/refresh_params.py"],
    "run_backtest": ["scripts/nfl_backtest.py"],
    "run_week_backtest": ["scripts/run_week_backtest.py"],
}


# ---------------------------------------------------------------------------
# JSON sanitizing
# ---------------------------------------------------------------------------

def clean(value: Any) -> Any:
    """Convert numpy/pandas scalars and NaN/NaT/inf into JSON-safe values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    if np is not None:
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            f = float(value)
            return f if math.isfinite(f) else None
        if isinstance(value, np.bool_):
            return bool(value)
    if pd is not None:
        if isinstance(value, pd.Timestamp):
            return None if pd.isna(value) else value.isoformat()
        if value is pd.NaT or value is pd.NA:
            return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [clean(v) for v in value]
    return value


def table_rows(df: pd.DataFrame) -> List[Dict]:
    """A DataFrame as a JSON-safe list of row dicts (NaN -> None)."""
    _require_deps()
    df = df.reset_index(drop=True)
    df.columns = [str(c) for c in df.columns]
    return [{k: clean(v) for k, v in row.items()} for row in df.to_dict("records")]


def spec(chart_id: str, title: str, chart: alt.Chart) -> Dict:
    """One rendered chart: its id, title, and cleaned Vega-Lite spec."""
    return {"id": chart_id, "title": title, "spec": clean(chart.to_dict())}


def metric(label: str, value: str, delta: Optional[str] = None,
           help: Optional[str] = None) -> Dict:  # noqa: A002 (matches st.metric naming)
    m = {"label": label, "value": value}
    if delta is not None:
        m["delta"] = delta
    if help is not None:
        m["help"] = help
    return m


def combo_label(name: str) -> str:
    return " + ".join(LEG_LABELS.get(k, k) for k in name.split("+"))


def _pct(x: float, dp: int = 1) -> str:
    return "-" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.{dp}f}%"


# ---------------------------------------------------------------------------
# Data loading (module-level cache keyed on file modification time)
# ---------------------------------------------------------------------------

_CACHE: Dict[str, Any] = {}


def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def load() -> Optional[Dict[str, Any]]:
    """Load the backtest outputs; None when no results exist yet."""
    _require_deps()
    key = _mtime(RESULTS_DIR / "meta.json")
    cached = _CACHE.get("results")
    if cached is not None and cached["mtime_key"] == key:
        return cached["data"]
    out = bt.load_outputs(str(RESULTS_DIR))
    data = None
    if out is not None:
        combos = bt.add_spread_bucket(out.combos)
        combos["combo_label"] = combos["combo"].map(combo_label)
        combos["nested"] = combos["nested"].astype(bool)
        combos["pushed"] = combos["pushed"].astype(bool)
        games = bt.add_spread_bucket(out.games)
        data = {"combos": combos, "games": games,
                "history": out.params_history, "meta": out.meta}
    _CACHE["results"] = {"mtime_key": key, "data": data}
    return data


def _explorer_games() -> List[Dict]:
    """Compact list of nflverse games for the explorer's game picker."""
    _require_deps()
    pull = latest_pull(RAW_ROOT)
    if pull is None:
        return []
    key = (str(pull.directory), pull.sha256)
    cached = _CACHE.get("explorer_games")
    if cached is not None and cached["key"] == key:
        return cached["games"]
    from combo_mm.nfl.ingest import load_games, load_pull
    games = load_games(load_pull(str(pull.directory)))
    rows = [{"game_id": g.game_id, "season": g.season, "week": g.week,
             "home": g.home, "away": g.away, "spread_line": g.spread_line,
             "total_line": g.total_line, "played": bool(g.played),
             "home_score": g.home_score, "away_score": g.away_score,
             "home_spread_odds": g.home_spread_odds, "away_spread_odds": g.away_spread_odds,
             "over_odds": g.over_odds, "under_odds": g.under_odds,
             "home_moneyline": g.home_moneyline, "away_moneyline": g.away_moneyline,
             "has_lines": bool(g.has_lines)}
            for g in games if g.has_lines]
    _CACHE["explorer_games"] = {"key": key, "games": rows}
    return rows


def has_results() -> bool:
    return DEPS_OK and load() is not None


def meta() -> Optional[Dict]:
    data = load() if DEPS_OK else None
    if data is None:
        return None
    m = data["meta"]
    return clean({"generated_at": m.get("generated_at"),
                  "generated_at_unix": m.get("generated_at_unix"),
                  "runtime_s": m.get("runtime_s"), "workers": m.get("workers"),
                  "config": m.get("config"), "split": m.get("split"),
                  "tuned_estimator": m.get("tuned_estimator"),
                  "data_vintage": m.get("data_vintage")})


def filter_options() -> Dict:
    """The filter panel's choices, derived from the backtest data + meta."""
    _require_deps()
    data = load()
    if data is None:
        return {}
    combos, meta_cfg = data["combos"], data["meta"]
    cfg = meta_cfg["config"]
    seasons = sorted(int(s) for s in combos["season"].unique())
    return {
        "seasons": seasons,
        "season_default": [seasons[0], seasons[-1]],
        "families": FAMILY_ORDER,
        "buckets": [b[2] for b in bt.SPREAD_BUCKETS],
        "models": cfg["variance_models"],
        "model_labels": {m: MODEL_LABELS.get(m, m) for m in cfg["variance_models"]},
        "primary_model": cfg["primary_model"],
        "game_types": ["REG", "Playoffs"],
        "samples": ["Test", "Train", "All"] if "split" in combos.columns else ["All"],
        "sample_default": "Test" if "split" in combos.columns else "All",
        "split": clean(meta_cfg.get("split")),
        "tuned_estimator": clean(meta_cfg.get("tuned_estimator")),
        "heatmap_metrics": ["Realized - naive", "Model - naive", "Model - realized"],
        "cal_widths": [0.025, 0.05, 0.1],
        "sensitivity_families_default": ["spread x total", "ML x total"],
        "edge_threshold_default": cfg.get("edge_threshold", 0.01),
        "corr_scales": clean(list(cfg.get("corr_scales", []))),
        "param_history_params": ["sigma_at_mean_points", "var_slope", "rho"],
        "params_files": [p.name for _, p in list_params(PARAMS_DIR)],
    }


# ---------------------------------------------------------------------------
# Filtering (pure function; params arrive as query strings)
# ---------------------------------------------------------------------------

def default_filter_params(data: Dict[str, Any]) -> Dict:
    _require_deps()
    combos, meta_cfg = data["combos"], data["meta"]
    seasons = sorted(int(s) for s in combos["season"].unique())
    return {
        "season_min": seasons[0], "season_max": seasons[-1],
        "families": list(FAMILY_ORDER),
        "buckets": [b[2] for b in bt.SPREAD_BUCKETS],
        "primary": meta_cfg["config"]["primary_model"],
        "include_nested": True,
        "game_types": ["REG", "Playoffs"],
        "sample": "Test" if "split" in combos.columns else "All",
    }


def parse_filter_params(data: Dict[str, Any], qs: Dict[str, str]) -> Dict:
    """Merge query-string params over the defaults. Lists are comma-separated."""
    _require_deps()
    p = default_filter_params(data)

    def num(key: str) -> Optional[int]:
        try:
            return int(qs[key])
        except (KeyError, ValueError, TypeError):
            return None

    def lst(key: str) -> Optional[List[str]]:
        v = qs.get(key)
        return v.split(",") if v else None

    lo, hi = num("season_min"), num("season_max")
    if lo is not None:
        p["season_min"] = lo
    if hi is not None:
        p["season_max"] = hi
    for key in ("families", "buckets", "game_types"):
        v = lst(key)
        if v:
            p[key] = v
    if qs.get("primary"):
        p["primary"] = qs["primary"]
    if qs.get("include_nested") in ("0", "false", "False"):
        p["include_nested"] = False
    if qs.get("sample"):
        p["sample"] = qs["sample"]
    return p


def apply_filters(data: Dict[str, Any], params: Dict) -> Dict[str, Any]:
    """Apply the filter panel to combos/games. Returns filtered frames + primary."""
    _require_deps()
    combos, games, meta_cfg = data["combos"], data["games"], data["meta"]
    has_split = "split" in combos.columns
    sample = params.get("sample", "All")
    sel = (combos["season"].between(params["season_min"], params["season_max"])
           & combos["family"].isin(params["families"])
           & combos["spread_bucket"].isin(params["buckets"]))
    gsel = (games["season"].between(params["season_min"], params["season_max"])
            & games["spread_bucket"].isin(params["buckets"]))
    if not params.get("include_nested", True):
        sel &= ~combos["nested"]
    types = set(params.get("game_types") or [])
    if types and types != {"REG", "Playoffs"}:
        want_reg, want_po = "REG" in types, "Playoffs" in types
        sel &= ((combos["game_type"] == "REG") & want_reg
                | (combos["game_type"] != "REG") & want_po)
        gsel &= ((games["game_type"] == "REG") & want_reg
                 | (games["game_type"] != "REG") & want_po)
    both = combos[sel]
    if has_split and sample != "All":
        sel &= combos["split"] == sample.lower()
        gsel &= games["split"] == sample.lower()
    primary = params.get("primary") or meta_cfg["config"]["primary_model"]
    return {"combos": combos[sel], "games": games[gsel], "primary": primary, "both": both}


# ---------------------------------------------------------------------------
# View builders
# ---------------------------------------------------------------------------

def _view_shell() -> Dict:
    return {"metrics": [], "specs": [], "tables": {}, "notes": []}


def view_overview(f: Dict[str, Any], meta_cfg: Dict) -> Dict:
    _require_deps()
    combos, games, primary, both = f["combos"], f["games"], f["primary"], f["both"]
    out = _view_shell()
    vintage = meta_cfg.get("data_vintage", {})
    out["metrics"] = [
        metric("Games", f"{games['game_id'].nunique():,}"),
        metric("Combos scored", f"{int((~combos['pushed']).sum()):,}"),
        metric("Pushed (dropped)", f"{int(combos['pushed'].sum()):,}",
               help="Any leg landing exactly on its line. Polymarket's push rule "
                    "(void leg vs void combo) is still open, so pushed combos are excluded."),
        metric("Seasons", f"{combos['season'].min()}-{combos['season'].max()}" if len(combos) else "-"),
        metric("Combo types", str(len(combos["combo"].unique()))),
        metric("nflverse pull", str(vintage.get("pull_date", "-")),
               help=f"sha256 {vintage.get('sha256', '')[:16]}"),
    ]
    if combos.empty or (~combos["pushed"]).sum() == 0:
        out["empty"] = True
        out["notes"].append("No combos match the filters.")
        return out
    cols = [bt.model_col(m) for m in meta_cfg["config"]["variance_models"]]
    scores = bt.score_table(combos, cols)
    prim = scores[scores["model"] == primary].iloc[0]
    naive = scores.iloc[0]
    out["metrics"] += [
        metric("Brier - naive product", f"{naive['brier']:.4f}"),
        metric(f"Brier - {MODEL_LABELS.get(primary, primary)}", f"{prim['brier']:.4f}",
               delta=f"{prim['brier_diff']:+.4f}"),
        metric("Brier skill vs naive", _pct(prim["brier_skill_vs_naive"], 2),
               help="1 - Brier(model)/Brier(naive). Positive = better than assuming independence."),
        metric("t-stat (game-clustered)", f"{prim['t_stat']:.1f}",
               help="Mean Brier difference / cluster-robust SE; combos in one game share its outcome."),
    ]
    show = scores.copy()
    show["model"] = show["model"].map(lambda m: MODEL_LABELS.get(m, "Naive product" if m == "naive" else m))
    out["tables"]["scores"] = table_rows(show[[
        "model", "n", "brier", "log_loss", "brier_skill_vs_naive", "brier_diff",
        "brier_diff_se", "t_stat", "mean_abs_gap_vs_naive"]])
    rows = []
    for fam, sub in combos.groupby("family"):
        sc = bt.score_table(sub, cols)
        for _, r in sc.iloc[1:].iterrows():
            rows.append({"family": fam, "model": MODEL_LABELS.get(r["model"], r["model"]),
                         "skill": r["brier_skill_vs_naive"],
                         "lo": (-(r["brier_diff"] - 1.96 * r["brier_diff_se"])) / sc.iloc[0]["brier"],
                         "hi": (-(r["brier_diff"] + 1.96 * r["brier_diff_se"])) / sc.iloc[0]["brier"]})
    fam_df = pd.DataFrame(rows)
    base = alt.Chart(fam_df).encode(
        y=alt.Y("family:N", sort=FAMILY_ORDER, title=None),
        yOffset=alt.YOffset("model:N"),
        color=alt.Color("model:N", title=None, legend=alt.Legend(orient="bottom", columns=1)),
    )
    bars = base.mark_bar().encode(x=alt.X("skill:Q", title="Brier skill vs naive", axis=alt.Axis(format="%")),
                                  tooltip=["family", "model", alt.Tooltip("skill:Q", format=".2%")])
    err = base.mark_rule(color="black").encode(x="lo:Q", x2="hi:Q")
    rule = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(strokeDash=[4, 3]).encode(x="x:Q")
    out["specs"].append(spec("skill_by_family", "Brier skill vs naive, by combo family",
                             (bars + err + rule).properties(height=300)))
    if "split" in both.columns and both["split"].nunique() >= 2:
        out["tables"]["train_vs_test"] = table_rows(_train_vs_test_rows(both, meta_cfg, primary))
        est = meta_cfg["config"].get("estimator", {})
        out["notes"].append(
            f"Frozen estimator ({MODEL_LABELS.get(primary, primary)}): window "
            f"{est.get('window_seasons') or 'all history'} seasons, half-life "
            f"{est.get('half_life_seasons')} seasons, variance shrink "
            f"x{est.get('var_shrink_multiplier')}.")
    out["notes"] += _findings_notes(combos, games, meta_cfg, primary)
    return out


def _train_vs_test_rows(both: pd.DataFrame, meta_cfg: Dict, primary: str) -> pd.DataFrame:
    _require_deps()
    col = bt.model_col(primary)
    rows = []
    for split in (bt.TRAIN, bt.TEST):
        sub = both[both["split"] == split]
        if (~sub["pushed"]).sum() == 0:
            continue
        seasons = sorted(sub["season"].unique())
        for label, part in (("All combos", sub), ("Excluding nested", sub[~sub["nested"]]),
                            ("Spread x total", sub[sub["family"] == "spread x total"])):
            if (~part["pushed"]).sum() == 0:
                continue
            sc = bt.score_table(part, [col]).set_index("model")
            m = col.removeprefix("p_")
            rows.append({"split": f"{split.title()} {seasons[0]}-{seasons[-1]}", "combos": label,
                         "games": part["game_id"].nunique(), "n": int(sc.loc[m, "n"]),
                         "brier_naive": sc.loc["naive", "brier"], "brier_model": sc.loc[m, "brier"],
                         "skill": sc.loc[m, "brier_skill_vs_naive"], "t": sc.loc[m, "t_stat"]})
    return pd.DataFrame(rows)


def _findings_notes(combos: pd.DataFrame, games: pd.DataFrame, meta_cfg: Dict, primary: str) -> List[str]:
    _require_deps()
    col = bt.model_col(primary)
    out: List[str] = []
    fam = bt.group_table(combos, "family", col).set_index("family")
    if len(fam):
        best = fam["brier_skill"].idxmax()
        out.append(f"Largest gain over naive: **{best}** combos (Brier skill {_pct(fam.loc[best, 'brier_skill'], 1)}) - "
                   "legs that load on the same score dimension (margin) are strongly dependent, and the naive "
                   "product misprices them.")
        if "spread x total" in fam.index:
            out.append(f"**Spread x total** combos: Brier skill {_pct(fam.loc['spread x total', 'brier_skill'], 2)} - "
                       "margin/total dependence in the NFL is weak on average, so these trade close to the naive price.")
    by_combo = bt.group_table(combos, "combo", col).set_index("combo")
    if "fav_ml+dog_cover" in by_combo.index:
        r = by_combo.loc["fav_ml+dog_cover"]
        out.append(f"'Favorite wins but underdog covers' hit **{_pct(r['realized'])}** vs naive **{_pct(r['naive'])}** "
                   f"and model **{_pct(r['model'])}** (n={int(r['n']):,}).")
    sb = bt.spread_bucket_structure(games)
    if len(sb):
        big = sb.iloc[-1]
        out.append(f"For **{big['spread_bucket']}-point favorites**, empirical Corr(fav margin, total) is "
                   f"**{big['emp_corr_margin_total']:+.3f} +/- {big['emp_corr_se']:.3f}** vs model "
                   f"{big['model_corr_margin_total']:+.3f}.")
    ml = bt.moneyline_consistency(games, meta_cfg["config"]["variance_models"])
    if len(ml) > 1:
        mkt = ml.iloc[0]
        mod = ml[ml["source"] == primary].iloc[0]
        out.append(f"Moneyline over-identification: model fav-win probability (from spread + total) differs from the "
                   f"market's by {mod['mean_abs_gap_vs_market']:.3f} on average (Brier {mod['brier']:.4f} vs market "
                   f"{mkt['brier']:.4f}); ML combos carry this marginal error on top of dependence.")
    return out


def view_combo_pricing(f: Dict[str, Any], heat_metric: str = "Realized - naive") -> Dict:
    _require_deps()
    combos, primary = f["combos"], f["primary"]
    out = _view_shell()
    col = bt.model_col(primary)
    out["notes"].append(
        "Every same-game combo from the favorite's perspective: one side of any two or three of "
        "moneyline / spread / total. Prices are averages over games; realized is the hit rate (+/-1 SE).")
    table = bt.group_table(combos, ["combo", "family", "n_legs", "nested"], col)
    if table.empty:
        out["empty"] = True
        out["notes"].append("No combos match the filters.")
        return out
    table["combo_label"] = table["combo"].map(combo_label)
    table = table.sort_values(["n_legs", "family", "combo"])
    order = list(table["combo_label"])

    long = table.melt(id_vars=["combo_label", "family", "realized_se", "realized"],
                      value_vars=["naive", "model"], var_name="price", value_name="p")
    points = pd.concat([
        long.assign(series=long["price"].map({"naive": "Naive product", "model": "Model"})),
        table.assign(series="Realized", p=table["realized"])[["combo_label", "family", "series", "p", "realized_se", "realized"]],
    ])
    color = alt.Scale(domain=["Naive product", "Model", "Realized"], range=[C_NAIVE, C_MODEL, C_REAL])
    base = alt.Chart(points).encode(y=alt.Y("combo_label:N", sort=order, title=None, axis=alt.Axis(labelLimit=320)))
    dots = base.mark_point(filled=True, size=90).encode(
        x=alt.X("p:Q", title="Probability", axis=alt.Axis(format="%")),
        color=alt.Color("series:N", scale=color, title=None, legend=alt.Legend(orient="top")),
        shape=alt.Shape("series:N", scale=alt.Scale(domain=["Naive product", "Model", "Realized"],
                                                    range=["circle", "diamond", "triangle-up"]), title=None),
        tooltip=["combo_label", "series", alt.Tooltip("p:Q", format=".2%")],
    )
    err = alt.Chart(table).mark_rule(color=C_REAL, strokeWidth=2).encode(
        y=alt.Y("combo_label:N", sort=order),
        x=alt.X("lo:Q"), x2="hi:Q",
    ).transform_calculate(lo="datum.realized - datum.realized_se", hi="datum.realized + datum.realized_se")
    out["specs"].append(spec("combo_dots", "Naive vs model vs realized, by combo",
                             (err + dots).properties(height=26 * len(order) + 40)))

    lift = table.melt(id_vars=["combo_label"], value_vars=["realized_minus_naive", "model_minus_naive"],
                      var_name="what", value_name="lift")
    lift["what"] = lift["what"].map({"realized_minus_naive": "Realized - naive", "model_minus_naive": "Model - naive"})
    out["specs"].append(spec(
        "lift_bars", "Correlation lift over the naive product",
        alt.Chart(lift).mark_bar().encode(
            y=alt.Y("combo_label:N", sort=order, title=None, axis=alt.Axis(labelLimit=320)),
            yOffset="what:N",
            x=alt.X("lift:Q", title="Probability points", axis=alt.Axis(format="%")),
            color=alt.Color("what:N", scale=alt.Scale(domain=["Realized - naive", "Model - naive"],
                                                      range=[C_REAL, C_MODEL]),
                            title=None, legend=alt.Legend(orient="top")),
            tooltip=["combo_label", "what", alt.Tooltip("lift:Q", format="+.2%")],
        ).properties(height=26 * len(order) + 40)))
    out["tables"]["combos"] = table_rows(table[[
        "combo_label", "family", "n_legs", "nested", "n", "pushed", "realized", "realized_se",
        "naive", "model", "realized_minus_naive", "model_minus_naive",
        "brier_naive", "brier_model", "brier_skill"]])
    out["notes"].append(
        "Within a family the combos partition every outcome, so family-level average hit rates are "
        "exactly 1/k for realized and model alike - compare Brier, not family averages.")

    size = bt.group_table(combos, "n_legs", col)
    size["n_legs"] = size["n_legs"].map(lambda n: f"{n}-leg")
    out["tables"]["by_size"] = table_rows(size[["n_legs", "n", "brier_naive", "brier_model", "brier_skill"]])
    buck = bt.group_table(combos, "spread_bucket", col)
    out["tables"]["by_bucket"] = table_rows(buck[["spread_bucket", "n", "brier_naive", "brier_model", "brier_skill"]])

    grid = bt.group_table(combos, ["combo", "spread_bucket"], col)
    grid["combo_label"] = grid["combo"].map(combo_label)
    metric_map = {"Realized - naive": grid["realized_minus_naive"],
                  "Model - naive": grid["model_minus_naive"],
                  "Model - realized": grid["model"] - grid["realized"]}
    grid["value"] = metric_map.get(heat_metric, metric_map["Realized - naive"])
    lim = float(np.nanmax(np.abs(grid["value"]))) if len(grid) else 0.1
    out["specs"].append(spec(
        "heatmap", f"Heatmap: {heat_metric}",
        alt.Chart(grid).mark_rect().encode(
            x=alt.X("spread_bucket:N", sort=[b[2] for b in bt.SPREAD_BUCKETS], title="Favorite size (|spread|)"),
            y=alt.Y("combo_label:N", sort=order, title=None, axis=alt.Axis(labelLimit=320)),
            color=alt.Color("value:Q", scale=alt.Scale(scheme="redblue", domain=[-lim, lim], reverse=True),
                            title=heat_metric, legend=alt.Legend(format="%")),
            tooltip=["combo_label", "spread_bucket", alt.Tooltip("value:Q", format="+.2%"), "n",
                     alt.Tooltip("realized:Q", format=".2%"), alt.Tooltip("naive:Q", format=".2%"),
                     alt.Tooltip("model:Q", format=".2%")],
        ).properties(height=26 * len(order) + 40)))
    return out


def view_calibration(f: Dict[str, Any], width: float = 0.05, combo: str = "All filtered") -> Dict:
    _require_deps()
    combos, primary = f["combos"], f["primary"]
    out = _view_shell()
    out["notes"].append("Events priced at 30% should hit ~30%. Points are sized by count; bars are +/-1 SE.")
    out["combo_options"] = ["All filtered"] + sorted(combos["combo"].unique(), key=combo_label)
    sub = combos if combo == "All filtered" else combos[combos["combo"] == combo]
    if (~sub["pushed"]).sum() == 0:
        out["empty"] = True
        out["notes"].append("No combos match.")
        return out
    col = bt.model_col(primary)
    cal = pd.concat([bt.calibration_table(sub, "naive", width), bt.calibration_table(sub, col, width)])
    cal["source"] = cal["source"].map(lambda s: "Naive product" if s == "naive" else "Model")
    cal["lo"] = cal["realized"] - cal["realized_se"]
    cal["hi"] = cal["realized"] + cal["realized_se"]
    top = float(max(cal["predicted"].max(), cal["realized"].max())) + 0.05
    diag = alt.Chart(pd.DataFrame({"x": [0, top], "y": [0, top]})).mark_line(
        strokeDash=[4, 4], color="black").encode(x="x:Q", y="y:Q")
    color = alt.Scale(domain=["Naive product", "Model"], range=[C_NAIVE, C_MODEL])
    base = alt.Chart(cal).encode(
        x=alt.X("predicted:Q", title="Mean predicted probability", axis=alt.Axis(format="%"),
                scale=alt.Scale(domain=[0, top])),
        color=alt.Color("source:N", scale=color, title=None))
    pts = base.mark_circle(opacity=0.85).encode(
        y=alt.Y("realized:Q", title="Realized hit rate", axis=alt.Axis(format="%"),
                scale=alt.Scale(domain=[0, top])),
        size=alt.Size("n:Q", legend=None, scale=alt.Scale(range=[30, 500])),
        tooltip=["source", "n", alt.Tooltip("predicted:Q", format=".2%"), alt.Tooltip("realized:Q", format=".2%")])
    bars = base.mark_rule().encode(y="lo:Q", y2="hi:Q")
    line = base.mark_line(opacity=0.5).encode(y="realized:Q")
    out["specs"].append(spec("calibration", "Calibration: predicted vs realized",
                             (diag + bars + line + pts).properties(height=420)))
    s = sub[~sub["pushed"]]
    gaps = pd.DataFrame({"gap": s[col] - s["naive"], "family": s["family"]})
    out["specs"].append(spec(
        "gap_hist", "Distribution of the model's correlation adjustment (model - naive)",
        alt.Chart(gaps).mark_bar(opacity=0.8).encode(
            x=alt.X("gap:Q", bin=alt.Bin(maxbins=60), title="Model - naive (probability)",
                    axis=alt.Axis(format="%")),
            y=alt.Y("count()", stack=True, title="Combos"),
            color=alt.Color("family:N", sort=FAMILY_ORDER, title=None),
        ).properties(height=260)))
    return out


def view_structure(f: Dict[str, Any], meta_cfg: Dict, param: str = "sigma_at_mean_points") -> Dict:
    _require_deps()
    games, history = f["games"], f.get("history")
    out = _view_shell()
    out["notes"].append(
        "Residual = final score - closing-line implied points "
        "(home = (total + spread)/2, away = (total - spread)/2). The model says "
        "Cov(margin, total) = sigma^2(fav) - sigma^2(dog): dependence between a favorite's margin "
        "and the total exists only because the team expected to score more has noisier scores.")
    if games.empty:
        out["empty"] = True
        out["notes"].append("No games match the filters.")
        return out
    sb = bt.spread_bucket_structure(games)
    long = pd.concat([
        sb.assign(series="Empirical", value=sb["emp_corr_margin_total"],
                  lo=sb["emp_corr_margin_total"] - 1.96 * sb["emp_corr_se"],
                  hi=sb["emp_corr_margin_total"] + 1.96 * sb["emp_corr_se"]),
        sb.assign(series="Model", value=sb["model_corr_margin_total"], lo=np.nan, hi=np.nan),
    ])
    order = [b[2] for b in bt.SPREAD_BUCKETS]
    base = alt.Chart(long).encode(x=alt.X("spread_bucket:N", sort=order, title="|Spread|"),
                                  xOffset="series:N",
                                  color=alt.Color("series:N", scale=alt.Scale(range=[C_REAL, C_MODEL]), title=None))
    out["specs"].append(spec(
        "corr_by_bucket", "Corr(favorite margin, total) by favorite size - empirical +/-1.96 SE vs model",
        (base.mark_bar().encode(y=alt.Y("value:Q", title="Correlation"),
                                tooltip=["spread_bucket", "series", alt.Tooltip("value:Q", format="+.3f"), "n_games"])
         + base.mark_rule(color="black").encode(y="lo:Q", y2="hi:Q")).properties(height=300)))
    sv = bt.sigma_vs_mu(games)
    sbase = alt.Chart(sv).encode(x=alt.X("mu:Q", title="Closing-line implied team points", scale=alt.Scale(zero=False)))
    emp = sbase.mark_circle(size=80, color=C_REAL).encode(
        y=alt.Y("emp_sigma:Q", title="Residual SD (points)", scale=alt.Scale(zero=False)),
        tooltip=["bin", "n", alt.Tooltip("emp_sigma:Q", format=".2f"), alt.Tooltip("model_sigma:Q", format=".2f")])
    err = sbase.mark_rule(color=C_REAL).encode(y="lo:Q", y2="hi:Q").transform_calculate(
        lo="datum.emp_sigma - 1.96 * datum.emp_sigma_se", hi="datum.emp_sigma + 1.96 * datum.emp_sigma_se")
    mod = sbase.mark_line(color=C_MODEL, point=True).encode(y="model_sigma:Q")
    out["specs"].append(spec(
        "sigma_vs_mu", "Score volatility vs implied team points - empirical +/-1.96 SE vs model sigma",
        (err + emp + mod).properties(height=300)))

    out["notes"].append("Lift = P(A and B) - P(A)*P(B), using the model's market-calibrated marginals for "
                       "P(A), P(B). Team totals use a half-point line at the closing-line implied team points.")
    lift = bt.outcome_lift(games)
    labels = list(OUTCOME_LABELS.values())
    full = []
    for _, r in lift.iterrows():
        for a, b in ((r["a"], r["b"]), (r["b"], r["a"])):
            full.append({"A": OUTCOME_LABELS[a], "B": OUTCOME_LABELS[b], "Empirical": r["emp_lift"],
                         "Model": r["model_lift"], "SE": r["emp_lift_se"], "n": r["n"]})
    full_df = pd.DataFrame(full).melt(id_vars=["A", "B", "SE", "n"], value_vars=["Empirical", "Model"],
                                      var_name="source", value_name="lift")
    lim = float(full_df["lift"].abs().max())
    heat = alt.Chart(full_df).mark_rect().encode(
        x=alt.X("A:N", sort=labels, title=None), y=alt.Y("B:N", sort=labels, title=None),
        color=alt.Color("lift:Q", scale=alt.Scale(scheme="redblue", domain=[-lim, lim], reverse=True),
                        legend=alt.Legend(format="%", title="Lift")),
        tooltip=["A", "B", "source", alt.Tooltip("lift:Q", format="+.2%"), alt.Tooltip("SE:Q", format=".2%"), "n"],
    )
    text = alt.Chart(full_df).mark_text(fontSize=11).encode(
        x=alt.X("A:N", sort=labels), y=alt.Y("B:N", sort=labels), text=alt.Text("lift:Q", format="+.1%"))
    out["specs"].append(spec(
        "outcome_lift", "How outcomes co-move: joint-hit lift over independence",
        (heat + text).properties(width=300, height=300).facet(column=alt.Column("source:N", title=None))))
    show = lift.copy()
    show["pair"] = show["a"].map(OUTCOME_LABELS) + " & " + show["b"].map(OUTCOME_LABELS)
    show["gap_in_se"] = (show["model_lift"] - show["emp_lift"]) / show["emp_lift_se"]
    out["tables"]["outcome_lift"] = table_rows(show[[
        "pair", "n", "emp_joint", "model_joint", "independent", "emp_lift",
        "emp_lift_se", "model_lift", "gap_in_se", "emp_phi"]])

    ss = bt.season_structure(games)
    long = ss.melt(id_vars=["season"], value_vars=["emp_rho", "model_rho"], var_name="s", value_name="rho")
    long["s"] = long["s"].map({"emp_rho": "Empirical (season)", "model_rho": "Model (walk-forward)"})
    out["specs"].append(spec(
        "rho_by_season", "Within-game score correlation rho by season",
        alt.Chart(long).mark_line(point=True).encode(
            x=alt.X("season:O"), y=alt.Y("rho:Q", title="rho"),
            color=alt.Color("s:N", scale=alt.Scale(domain=["Empirical (season)", "Model (walk-forward)"],
                                                   range=[C_REAL, C_MODEL]), title=None,
                            legend=alt.Legend(orient="top")),
            tooltip=["season", "s", alt.Tooltip("rho:Q", format="+.3f")]).properties(height=260)))
    long = ss.melt(id_vars=["season"], value_vars=["emp_corr_mt_fav", "model_corr_mt_fav"],
                   var_name="s", value_name="corr")
    long["s"] = long["s"].map({"emp_corr_mt_fav": "Empirical (season)",
                               "model_corr_mt_fav": "Model (walk-forward)"})
    out["specs"].append(spec(
        "corr_by_season", "Corr(favorite margin, total) by season",
        alt.Chart(long).mark_line(point=True).encode(
            x=alt.X("season:O"), y=alt.Y("corr:Q", title="Correlation"),
            color=alt.Color("s:N", scale=alt.Scale(domain=["Empirical (season)", "Model (walk-forward)"],
                                                   range=[C_REAL, C_MODEL]), title=None,
                            legend=alt.Legend(orient="top")),
            tooltip=["season", "s", alt.Tooltip("corr:Q", format="+.3f")]).properties(height=260)))

    if history is not None and len(history):
        h = history.copy()
        h = h[h["season"].between(games["season"].min(), games["season"].max())]
        h["t"] = h["season"] + (h["week"] - 1) / 22.0
        out["specs"].append(spec(
            "param_history", "Walk-forward league parameters (one estimate per week, games strictly before that week)",
            alt.Chart(h).mark_line().encode(
                x=alt.X("t:Q", title="Season", axis=alt.Axis(format="d")),
                y=alt.Y(f"{param}:Q", title=None, scale=alt.Scale(zero=False)),
                color=alt.Color("model:N", title=None, legend=alt.Legend(orient="top"),
                                scale=alt.Scale(domain=list(MODEL_LABELS))),
                tooltip=["season", "week", "model", alt.Tooltip(f"{param}:Q", format=".3f")],
            ).properties(height=260)))

    ml = bt.moneyline_consistency(games, meta_cfg["config"]["variance_models"])
    ml["source"] = ml["source"].map(lambda s: "Market ML price" if s == "market" else MODEL_LABELS.get(s, s))
    out["tables"]["moneyline"] = table_rows(ml)
    out["notes"].append(
        "Moneyline over-identification - model fav-win probability implied by spread + total vs the market's ML "
        f"price. Realized favorite win rate: {_pct(ml.attrs.get('realized_fav_win_rate', float('nan')))} "
        f"over {ml.attrs.get('n', 0):,} games (ties excluded).")
    return out


def view_sensitivity(f: Dict[str, Any], meta_cfg: Dict, families: List[str], thr: float) -> Dict:
    _require_deps()
    combos = f["combos"]
    out = _view_shell()
    cfg = meta_cfg["config"]
    primary = cfg.get("sensitivity_model") or cfg["primary_model"]
    out["notes"].append(
        f"Re-price every combo with the **{MODEL_LABELS.get(primary, primary)}** model while scaling all modeled "
        "dependence by c: rho -> c*rho and the favorite/underdog variance asymmetry -> c x. **c = 0** makes margin "
        "and total independent (identical to the naive product for spread x total combos, because marginals are "
        "market-calibrated); c = 1 is the fitted model. Legs on the *same* score dimension (ML x spread) stay "
        "fully dependent at every c.")
    sub = combos[combos["family"].isin(families)]
    if (~sub["pushed"]).sum() == 0:
        out["empty"] = True
        out["notes"].append("No combos match.")
        return out
    sens = bt.sensitivity_table(sub, cfg["corr_scales"], primary, thr)
    base_brier = bt.score_table(sub, []).iloc[0]["brier"]
    s = sens.assign(lo=(-(sens["brier"] - base_brier) - 1.96 * sens["brier_diff_se"]) / base_brier,
                    hi=(-(sens["brier"] - base_brier) + 1.96 * sens["brier_diff_se"]) / base_brier)
    base = alt.Chart(s).encode(x=alt.X("corr_scale:Q", title="Correlation scale c"))
    out["specs"].append(spec(
        "brier_vs_c", "Brier skill vs naive as correlation is scaled",
        (base.mark_area(opacity=0.15, color=C_MODEL).encode(y="lo:Q", y2="hi:Q")
         + base.mark_line(point=True, color=C_MODEL).encode(
             y=alt.Y("brier_skill_vs_naive:Q", title="Brier skill vs naive", axis=alt.Axis(format="%")),
             tooltip=[alt.Tooltip("corr_scale:Q"), alt.Tooltip("brier_skill_vs_naive:Q", format=".3%")])
         ).properties(height=280)))
    pbase = alt.Chart(sens).encode(x=alt.X("corr_scale:Q", title="Correlation scale c"))
    out["specs"].append(spec(
        "pnl_vs_c", "Edge P&L vs a naive-pricing counterparty",
        (pbase.mark_bar(opacity=0.35, color=C_NAIVE).encode(y=alt.Y("n_trades:Q", title="Trades"))
         + pbase.mark_line(point=True, color=C_REAL).encode(
             y=alt.Y("total_pnl:Q", title="Total P&L ($1 payout units)"),
             tooltip=["corr_scale", "n_trades", alt.Tooltip("total_pnl:Q", format=",.1f"),
                      alt.Tooltip("pnl_t_stat:Q", format=".1f")])
         ).resolve_scale(y="independent").properties(height=280)))
    out["tables"]["sensitivity"] = table_rows(sens.drop(columns=["column"]))

    curves = []
    for c in cfg["corr_scales"]:
        col = bt.model_col(primary) if c == 1.0 else bt.scale_col(c)
        if col not in sub.columns:
            continue
        t = bt.edge_pnl(sub, col, thr)
        t = t[t["side"] != 0]
        t = t.groupby("gameday", as_index=False)["pnl"].sum()
        t["cum_pnl"] = t["pnl"].cumsum()
        t["c"] = f"c = {c:g}"
        curves.append(t)
    if curves:
        cur = pd.concat(curves)
        cur["gameday"] = pd.to_datetime(cur["gameday"])
        out["specs"].append(spec(
            "cum_pnl", "Cumulative edge P&L over time",
            alt.Chart(cur).mark_line().encode(
                x=alt.X("gameday:T", title=None), y=alt.Y("cum_pnl:Q", title="Cumulative P&L ($1 payout units)"),
                color=alt.Color("c:N", title="Correlation scale", scale=alt.Scale(scheme="viridis")),
                tooltip=["c", alt.Tooltip("gameday:T"), alt.Tooltip("cum_pnl:Q", format=",.2f")],
            ).properties(height=320)))
    t = bt.edge_pnl(sub, bt.model_col(primary), thr)
    t = t[t["side"] != 0]
    by = t.groupby(["season", "family"], as_index=False)["pnl"].sum()
    out["specs"].append(spec(
        "pnl_by_season_family", f"P&L by season and family (fitted model, threshold {thr:.4f})",
        alt.Chart(by).mark_bar().encode(
            x=alt.X("season:O"), y=alt.Y("pnl:Q", title="P&L"),
            color=alt.Color("family:N", sort=FAMILY_ORDER, title=None),
            tooltip=["season", "family", alt.Tooltip("pnl:Q", format=",.2f")]).properties(height=260)))
    out["notes"].append(
        "Stylized: buy (sell) one $1-payout combo at the naive product when the model is above (below) it by more "
        "than the threshold. It isolates the value of modeling dependence - it is **not** a live P&L forecast: "
        "real books price some same-game correlation, charge vig, and RFQ flow selects against the quoter.")
    return out


def view_params(params_file: Optional[str] = None) -> Dict:
    """Params & data: pull/backtest provenance, estimator tuning, weekly params."""
    _require_deps()
    out = _view_shell()
    data = load()
    meta_cfg = data["meta"] if data else {}
    pull = latest_pull(RAW_ROOT)
    gen = ""
    try:
        gen = pd.to_datetime(meta_cfg.get("generated_at_unix", 0), unit="s").strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        pass
    out["metrics"] = [
        metric("Cached nflverse pull", pull.pull_date if pull else "none"),
        metric("Backtest generated", gen or "-"),
        metric("Backtest runtime", f"{meta_cfg.get('runtime_s', 0):.0f}s on {meta_cfg.get('workers', 1)} workers"),
    ]
    selection = PARAMS_DIR / "estimator.json"
    if selection.exists():
        sel = json.loads(selection.read_text())
        out["metrics"] += [
            metric("Tuned on", f"{sel['train_seasons'][0]}-{sel['train_seasons'][1]}"),
            metric("Selected model", MODEL_LABELS.get(sel["variance_model"], sel["variance_model"])),
            metric("Train Brier", f"{sel['train_brier']:.5f}", help=f"Naive: {sel['train_brier_naive']:.5f}"),
            metric("Candidates", str(sel["n_candidates"])),
        ]
        out["notes"].append(f"Frozen estimator: `{json.dumps(sel['estimator'])}` - used by the backtest's test "
                            "period and the weekly refresh. Test-season games are removed from the input before "
                            "tuning runs.")
    else:
        out["notes"].append("No tuned estimator yet: `python scripts/nfl_tune.py` (train-period grid search).")
    grid_path = REPO / "results" / "nfl_tuning" / "grid.csv"
    if grid_path.exists():
        grid = pd.read_csv(grid_path)
        grid["window_seasons"] = grid["window_seasons"].map(lambda w: "all" if pd.isna(w) else f"{int(w)}")
        grid["variance_model"] = grid["variance_model"].map(lambda m: MODEL_LABELS.get(m, m))
        out["tables"]["tuning_grid"] = table_rows(grid.drop(columns=["grid_index"]))
    files = list_params(PARAMS_DIR)
    names = [p.name for _, p in files]
    out["params_files"] = names
    if not files:
        out["notes"].append("No promoted params files yet (`python scripts/refresh_params.py --pull`).")
        return out
    chosen = params_file if params_file in names else names[-1]
    out["params_file"] = chosen
    params = load_params(PARAMS_DIR / chosen)
    lg = params["league"]
    out["metrics"] += [
        metric("Variance model", MODEL_LABELS.get(lg["variance_model"], lg["variance_model"])),
        metric("sigma at league-avg points", f"{lg['sigma_at_mean_points']:.2f}"),
        metric("Variance slope b", f"{lg['var_slope']:.3f}", help="sigma^2(mu) = a + b*mu"),
        metric("rho", f"{lg['rho']:+.3f}"),
        metric("Games in window", f"{lg['n_games']:,}", help=f"Seasons {lg['seasons']}"),
    ]
    out["notes"].append(f"As of {params['as_of']} - data vintage {params['data_vintage'].get('pull_date')} - "
                        f"model {params['model_version']}")
    if params["games"]:
        out["tables"]["slate"] = table_rows(pd.DataFrame(params["games"])[
            ["game_id", "away", "home", "spread_line", "total_line", "mu_home", "mu_away",
             "sigma_home", "sigma_away", "rho", "corr_margin_total"]])
    if params["teams"]:
        tdf = pd.DataFrame(params["teams"]).T.reset_index(names="team").sort_values(
            "off_var_factor", ascending=False)
        out["tables"]["team_factors"] = table_rows(tdf)
    report = PARAMS_DIR / "reports" / chosen.replace(".json", ".gates.json")
    if report.exists():
        rep = json.loads(report.read_text())
        out["tables"]["gates"] = table_rows(pd.DataFrame(rep["gates"]))
    return out


# ---------------------------------------------------------------------------
# Combo explorer
# ---------------------------------------------------------------------------

def explorer_games() -> List[Dict]:
    """Game picker options for the explorer (historical games with lines)."""
    _require_deps()
    return [{"game_id": g["game_id"], "season": g["season"], "week": g["week"],
             "home": g["home"], "away": g["away"], "spread_home": g["spread_line"],
             "total": g["total_line"], "played": g["played"],
             "label": f"{g['away']} @ {g['home']}  (home {-g['spread_line']:+g}, "
                      f"total {g['total_line']:g})"
                      + (f" - final {g['away_score']}-{g['home_score']}" if g["played"]
                         else " - not played")}
            for g in _explorer_games()]


def _explorer_params(params_file: Optional[str], model: str) -> Dict:
    _require_deps()
    files = list_params(PARAMS_DIR)
    names = [p.name for _, p in files]
    params = None
    if params_file in names:
        params = load_params(PARAMS_DIR / params_file)
    elif names:
        params = load_params(PARAMS_DIR / names[-1])
    if params is None:
        params = {"league": {"variance_model": "mean_linear", "var_intercept": 58.0,
                             "var_slope": 1.07, "rho": 0.05,
                             "sigma_min": 5.0, "sigma_max": 16.0}, "teams": {}}
    params = json.loads(json.dumps(params))
    params["league"]["variance_model"] = model
    if model == "league_constant":
        mp = params["league"].get("mean_points", 22.5)
        params["league"]["var_intercept"] = params["league"]["var_intercept"] + params["league"]["var_slope"] * mp
        params["league"]["var_slope"] = 0.0
    return params


def explorer_price(payload: Dict) -> Dict:
    """Price a same-game combo. Payload: game_id (historical) or home/away/spread_home/total
    (hypothetical), team perspective, market prices, legs, and model settings."""
    _require_deps()
    out = {"metrics": [], "specs": [], "tables": {}, "notes": []}
    game = None
    game_id = payload.get("game_id")
    if game_id:
        matches = [g for g in _explorer_games() if g["game_id"] == game_id]
        if not matches:
            return {"error": f"unknown game_id {game_id!r}"}
        game = matches[0]
        home, away = game["home"], game["away"]
        spread_line, total = float(game["spread_line"]), float(game["total_line"])
        p_home_cover_mkt = devig_pair(game["home_spread_odds"], game["away_spread_odds"]) or 0.5
        p_over_mkt = devig_pair(game["over_odds"], game["under_odds"]) or 0.5
        p_home_ml_mkt = devig_pair(game["home_moneyline"], game["away_moneyline"])
    else:
        home = payload.get("home") or "KC"
        away = payload.get("away") or "BUF"
        spread_line = float(payload.get("spread_home", -3.5))  # home expected margin
        total = float(payload.get("total", 47.5))
        p_home_cover_mkt = p_over_mkt = 0.5
        p_home_ml_mkt = None
    team = payload.get("team") or home
    team_is_home = team == home
    opp = away if team == home else home
    p_team_cover = float(payload.get("p_team_cover",
                                     p_home_cover_mkt if team_is_home else 1 - p_home_cover_mkt))
    p_over = float(payload.get("p_over", p_over_mkt))
    use_ml = bool(payload.get("use_ml", p_home_ml_mkt is not None))
    ml_default = None if p_home_ml_mkt is None else (p_home_ml_mkt if team_is_home else 1 - p_home_ml_mkt)
    p_team_ml_mkt = payload.get("p_team_ml", ml_default if ml_default is not None else 0.5)

    params = _explorer_params(payload.get("params_file"), payload.get("model") or "mean_linear_team")
    corr_scale = float(payload.get("corr_scale", 1.0))
    override = payload.get("override")

    def cov_fn(mh: float, ma: float):
        if override:
            sig_h, sig_a, rho = float(override["sigma_home"]), float(override["sigma_away"]), float(override["rho"])
            base_cov = MatchupCovariance(sig_h, sig_a, rho)
            if corr_scale == 1.0:
                return base_cov
            vbar = 0.5 * (sig_h ** 2 + sig_a ** 2)
            vh = vbar + corr_scale * (sig_h ** 2 - vbar)
            va = vbar + corr_scale * (sig_a ** 2 - vbar)
            return MatchupCovariance(math.sqrt(max(vh, 1e-6)), math.sqrt(max(va, 1e-6)),
                                     max(min(corr_scale * rho, 0.95), -0.95))
        return matchup_covariance(params, home, away, mh, ma, corr_scale=corr_scale)

    p_home_cover = p_team_cover if team_is_home else 1 - p_team_cover
    cal = calibrate_means(spread_line, p_home_cover, total, p_over, cov_fn)
    gm = GameModel((cal.mu_home, cal.mu_away), cal.cov)
    p_team_ml_model = gm.leg(home_ml() if team_is_home else away_ml())
    p_ml_naive = float(p_team_ml_mkt) if use_ml else p_team_ml_model

    team_ml_leg = home_ml() if team_is_home else away_ml()
    opp_ml_leg = away_ml() if team_is_home else home_ml()
    team_cov_leg = home_cover(spread_line) if team_is_home else away_cover(spread_line)
    opp_cov_leg = away_cover(spread_line) if team_is_home else home_cover(spread_line)
    catalog = {
        ("ml", "team"): (f"{team} ML", team_ml_leg, p_ml_naive),
        ("ml", "opp"): (f"{opp} ML", opp_ml_leg, 1 - p_ml_naive),
        ("spread", "team"): (f"{team} {-spread_line:+g}" if team_is_home else f"{team} {spread_line:+g}",
                             team_cov_leg, p_team_cover),
        ("spread", "opp"): (f"{opp} {spread_line:+g}" if team_is_home else f"{opp} {-spread_line:+g}",
                            opp_cov_leg, 1 - p_team_cover),
        ("total", "over"): (f"Over {total:g}", over(total), p_over),
        ("total", "under"): (f"Under {total:g}", under(total), 1 - p_over),
    }
    chosen = [catalog[(leg.get("kind"), leg.get("side"))] for leg in payload.get("legs", [])
               if (leg.get("kind"), leg.get("side")) in catalog]
    legs = [c[1] for c in chosen]

    out["metrics"] = [
        metric(f"{home} implied pts", f"{cal.mu_home:.1f}", help="Market-calibrated score mean"),
        metric(f"{away} implied pts", f"{cal.mu_away:.1f}", help="Market-calibrated score mean"),
        metric("sigma home / away", f"{cal.cov.sigma_home:.1f} / {cal.cov.sigma_away:.1f}"),
        metric("Corr(margin, total)", f"{cal.cov.corr_margin_total:+.3f}",
               help=f"Home margin vs game total. rho(home, away scores) = {cal.cov.rho:+.3f}"),
    ]
    if chosen:
        p_model = gm.joint(legs)
        p_naive = math.prod(c[2] for c in chosen)
        combo = {"legs": [c[0] for c in chosen], "model": p_model, "naive": p_naive,
                 "adj_bps": (p_model - p_naive) * 1e4,
                 "american_model": _american(p_model), "american_naive": _american(p_naive)}
        if game is not None and game["played"]:
            res = [_settle(leg, game) for leg in legs]
            combo["realized"] = "push" if "push" in res else ("won" if all(r == "win" for r in res) else "lost")
        out["combo"] = clean(combo)
        out["tables"]["legs"] = clean([{"leg": c[0], "naive_p": c[2], "model_p": gm.leg(c[1])}
                                       for c in chosen])
    else:
        out["notes"].append("Pick at least one leg.")
    out["specs"].append(spec("score_heatmap", "Score distribution - highlighted cells: every chosen leg wins",
                             _score_heatmap_chart(gm, legs, home, away, game)))

    rows = []
    keys = list(catalog)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            for k in range(j, len(keys)):
                sel = [keys[i], keys[j]] + ([keys[k]] if payload.get("three_leg", False) else [])
                sel = [s for s in sel if s]
                if len(sel) < 2:
                    continue
                lg = [catalog[s][1] for s in sel]
                pm = gm.joint(lg)
                if pm < 1e-9:
                    continue
                pn = math.prod(catalog[s][2] for s in sel)
                row = {"combo": " + ".join(catalog[s][0] for s in sel), "legs": len(sel),
                       "model": pm, "naive": pn, "adj_bps": (pm - pn) * 1e4}
                if game is not None and game["played"]:
                    res = [_settle(leg, game) for leg in lg]
                    row["result"] = "push" if "push" in res else ("won" if all(r == "win" for r in res) else "lost")
                rows.append(row)
    # also all 3-leg combos
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            for k in range(j + 1, len(keys)):
                sel = [keys[i], keys[j], keys[k]]
                lg = [catalog[s][1] for s in sel]
                pm = gm.joint(lg)
                if pm < 1e-9:
                    continue
                pn = math.prod(catalog[s][2] for s in sel)
                row = {"combo": " + ".join(catalog[s][0] for s in sel), "legs": 3,
                       "model": pm, "naive": pn, "adj_bps": (pm - pn) * 1e4}
                if game is not None and game["played"]:
                    res = [_settle(leg, game) for leg in lg]
                    row["result"] = "push" if "push" in res else ("won" if all(r == "win" for r in res) else "lost")
                rows.append(row)
    rows.sort(key=lambda r: (r["legs"], r["adj_bps"]))
    out["tables"]["all_combos"] = clean(rows)
    return out


def _american(p: float) -> str:
    if p <= 0 or p >= 1:
        return "-"
    return f"{-100 * p / (1 - p):+.0f}" if p >= 0.5 else f"+{100 * (1 - p) / p:.0f}"


def _settle(leg, game: Dict) -> str:
    from combo_mm.nfl.joint import settle_leg
    return settle_leg(leg, game["home_score"], game["away_score"])


def _score_heatmap_chart(gm: GameModel, legs, home: str, away: str, game: Optional[Dict]) -> alt.Chart:
    """Integer-score probability mass with the cells where every chosen leg wins highlighted."""
    _require_deps()
    mh, ma = gm.mean
    (v11, v12), (_, v22) = gm.cov.matrix()
    lo_h, hi_h = max(0, int(mh - 3 * math.sqrt(v11))), int(mh + 3 * math.sqrt(v11)) + 1
    lo_a, hi_a = max(0, int(ma - 3 * math.sqrt(v22))), int(ma + 3 * math.sqrt(v22)) + 1
    hs, as_ = np.meshgrid(np.arange(lo_h, hi_h + 1), np.arange(lo_a, hi_a + 1), indexing="ij")
    det = v11 * v22 - v12 * v12
    dh, da = hs - mh, as_ - ma
    dens = np.exp(-0.5 * (v22 * dh * dh - 2 * v12 * dh * da + v11 * da * da) / det) / (2 * math.pi * math.sqrt(det))
    win = np.ones_like(hs, dtype=bool)
    for leg in legs:
        v = leg.row[0] * hs + leg.row[1] * as_
        win &= (v > leg.line) if leg.direction > 0 else (v < leg.line)
    df = pd.DataFrame({"home": hs.ravel(), "away": as_.ravel(), "p": dens.ravel() / dens.sum(),
                       "combo": np.where(win.ravel(), "Combo wins", "Combo loses") if legs else "-"})
    enc = dict(x=alt.X("home:O", title=f"{home} points",
                       axis=alt.Axis(labelOverlap=True, values=list(range(0, 80, 7)))),
               y=alt.Y("away:O", title=f"{away} points", sort="descending",
                       axis=alt.Axis(labelOverlap=True, values=list(range(0, 80, 7)))))
    heat = alt.Chart(df).mark_rect().encode(
        **enc,
        color=alt.Color("p:Q", scale=alt.Scale(scheme="blues"), legend=None),
        opacity=alt.condition(alt.datum.combo == "Combo loses", alt.value(0.2), alt.value(1.0)),
        stroke=alt.condition(alt.datum.combo == "Combo wins", alt.value(C_REAL), alt.value(None)),
        strokeWidth=alt.value(0.4),
        tooltip=["home", "away", alt.Tooltip("p:Q", format=".3%"), "combo"],
    )
    layers = heat
    if game is not None and game["played"]:
        final = pd.DataFrame({"home": [game["home_score"]], "away": [game["away_score"]]})
        layers = heat + alt.Chart(final).mark_point(shape="cross", size=250, color="black", filled=True).encode(**enc)
    return layers.properties(height=380)


# ---------------------------------------------------------------------------
# Research scripts (allowlisted only)
# ---------------------------------------------------------------------------

def _build_argv(name: str, options: Optional[Dict] = None,
                data_dir: Optional[str] = None) -> List[str]:
    """Argv for an allowlisted script; raises ValueError for unknown names."""
    if name not in RUN_ALLOWLIST:
        raise ValueError(f"unknown script {name!r}")
    argv = list(RUN_ALLOWLIST[name])
    if name == "run_backtest" and options:
        first = options.get("first_season")
        last = options.get("last_season")
        if first is not None:
            argv += ["--first-season", str(int(first))]
        if last is not None:
            argv += ["--last-season", str(int(last))]
        if options.get("pull"):
            argv.append("--pull")
    if name == "run_week_backtest" and data_dir:
        argv += ["--data-dir", data_dir]
    return argv


def run_script(name: str, options: Optional[Dict] = None,
               data_dir: Optional[str] = None) -> Dict:
    """Run an allowlisted research script; returns ok + the last 40 output lines."""
    argv = _build_argv(name, options, data_dir)
    proc = subprocess.run([sys.executable, *argv], cwd=REPO, capture_output=True,
                          text=True, timeout=7200)
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-40:])
    _CACHE.pop("results", None)  # results may have changed
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "output": tail or "(no output)"}


def meta_brief() -> Dict:
    """Stdlib-only meta check: has_results + parsed meta.json, no pandas needed."""
    needed = ["combos.csv", "games.csv", "params_history.csv", "meta.json"]
    if not all((RESULTS_DIR / n).exists() for n in needed):
        return {"has_results": False}
    try:
        m = json.loads((RESULTS_DIR / "meta.json").read_text())
    except (OSError, ValueError):
        return {"has_results": False}
    cfg = m.get("config", {}) if isinstance(m, dict) else {}
    return {"has_results": True, "meta": clean(m), "views": VIEWS,
            "models": cfg.get("variance_models", []),
            "primary_model": cfg.get("primary_model"),
            "split": clean(m.get("split")) if isinstance(m, dict) else None}

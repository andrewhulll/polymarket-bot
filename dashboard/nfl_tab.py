"""Streamlit "NFL correlation" tab: same-game combo backtest + correlation explorer.

Offline research view over ``results/nfl_backtest`` (written by
``scripts/nfl_backtest.py``) and the weekly ``params/`` files (written by
``scripts/refresh_params.py``). Nothing here quotes or trades.

Sub-views:

1. Overview -- data vintage, headline scores vs the naive product, key findings.
2. Combo pricing -- realized vs naive vs model for every same-game combo,
   by combo, family, size and favorite size.
3. Calibration -- reliability of model and naive prices.
4. Correlation structure -- how NFL outcomes actually co-move: margin/total
   correlation by favorite size, score volatility vs implied points,
   pairwise outcome lift (empirical vs model), parameter drift.
5. Sensitivity & P&L -- scale the correlation assumption 0 -> 2x; stylized
   edge P&L against a naive-pricing counterparty.
6. Combo explorer -- build any same-game combo (team ML / spread / total,
   team or opponent side) for a historical or hypothetical game and see the
   model joint vs the naive product, with the score distribution.
7. Params & data -- the promoted weekly params file, gate report, and
   buttons to refresh data / params / backtest.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from combo_mm.nfl import synthetic_backtest as bt
from combo_mm.nfl.ingest import devig_pair, latest_pull, load_games
from combo_mm.nfl.joint import (
    GameModel, away_cover, away_ml, calibrate_means, home_cover, home_ml, over, under,
)
from combo_mm.nfl.params_io import (
    VARIANCE_MODELS, MatchupCovariance, list_params, load_params, matchup_covariance,
)

REPO = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO / "results" / "nfl_backtest"
PARAMS_DIR = REPO / "params"
RAW_ROOT = REPO / "data" / "raw"

MODEL_LABELS = {
    "league_constant": "League constant σ",
    "mean_linear": "σ² grows with implied points",
    "mean_linear_team": "σ² ~ implied points × team factors",
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


def combo_label(name: str) -> str:
    return " + ".join(LEG_LABELS.get(k, k) for k in name.split("+"))


def _pct(x: float, dp: int = 1) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.{dp}f}%"


def _pp(x: float, dp: int = 1) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:+.{dp}f} pp"


def _chart(chart) -> None:
    st.altair_chart(chart, width="stretch")


# ---------------------------------------------------------------------------
# Data loading (cached on file modification time)
# ---------------------------------------------------------------------------

def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


@st.cache_data(show_spinner="Loading NFL backtest results...")
def _load_results(directory: str, _mtime_key: float):
    out = bt.load_outputs(directory)
    if out is None:
        return None
    combos = bt.add_spread_bucket(out.combos)
    combos["combo_label"] = combos["combo"].map(combo_label)
    combos["nested"] = combos["nested"].astype(bool)
    combos["pushed"] = combos["pushed"].astype(bool)
    games = bt.add_spread_bucket(out.games)
    return combos, games, out.params_history, out.meta


@st.cache_data(show_spinner=False)
def _load_games_table(pull_dir: str, _sha: str):
    from combo_mm.nfl.ingest import load_pull
    games = load_games(load_pull(pull_dir))
    return games


def _run_script(args: List[str], label: str) -> None:
    with st.status(label, expanded=True) as status:
        proc = subprocess.run([sys.executable, *args], cwd=REPO, capture_output=True, text=True)
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-40:])
        st.code(tail or "(no output)", language="text")
        if proc.returncode == 0:
            status.update(label=f"{label} -- done", state="complete")
            st.cache_data.clear()
        else:
            status.update(label=f"{label} -- failed (exit {proc.returncode})", state="error")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render() -> None:
    st.header("NFL same-game combo correlation")
    st.caption(
        "Offline research over historical NFL closing lines and scores (nflverse). "
        "**History for shape, market for location**: score covariance is estimated walk-forward "
        "from past games; each game's score means are solved so model leg prices equal the "
        "de-vigged closing prices. Any gap between the model and the naive product of leg prices "
        "is therefore dependence between legs. Nothing here quotes or trades."
    )
    loaded = _load_results(str(RESULTS_DIR), _mtime(RESULTS_DIR / "meta.json"))
    if loaded is None:
        _empty_state()
        return
    combos, games, history, meta = loaded
    filtered_combos, filtered_games, primary, both_splits = _filters(combos, games, meta)

    views = st.tabs(["Overview", "Combo pricing", "Calibration", "Correlation structure",
                     "Sensitivity & P&L", "Combo explorer", "Params & data"])
    with views[0]:
        _overview(filtered_combos, filtered_games, meta, primary, both_splits)
    with views[1]:
        _combo_pricing(filtered_combos, primary)
    with views[2]:
        _calibration(filtered_combos, meta, primary)
    with views[3]:
        _structure(filtered_games, history, meta)
    with views[4]:
        _sensitivity(filtered_combos, meta, primary)
    with views[5]:
        _explorer(meta)
    with views[6]:
        _params_and_data(meta)


def _empty_state() -> None:
    st.info(
        "No backtest results yet. Run the walk-forward backtest (downloads nflverse data on first "
        "run; ~1-2 minutes on a multi-core machine), or from a terminal: "
        "`python scripts/nfl_backtest.py --pull`."
    )
    have_data = latest_pull(RAW_ROOT) is not None
    c1, c2 = st.columns(2)
    with c1:
        first, last = st.slider("Seasons", 2003, 2025, (2006, 2025), key="nfl_empty_seasons")
    with c2:
        st.write("")
        st.write("")
        if st.button("Run NFL backtest", type="primary", key="nfl_run_empty"):
            args = ["scripts/nfl_backtest.py", "--first-season", str(first), "--last-season", str(last)]
            if not have_data:
                args.append("--pull")
            _run_script(args, "Running walk-forward backtest")
            st.rerun()


def _filters(combos: pd.DataFrame, games: pd.DataFrame, meta: Dict):
    seasons = sorted(combos["season"].unique())
    models = [m for m in meta["config"]["variance_models"]]
    has_split = "split" in combos.columns
    sample = "All"
    if has_split:
        split = meta.get("split", {})
        train, test = split.get("train"), split.get("test")
        c1, c2 = st.columns([1, 2.2], vertical_alignment="center")
        with c1:
            sample = st.segmented_control(
                "Sample", ["Test", "Train", "All"], default="Test", key="nfl_f_split",
                help="Chronological 80/20 split. Estimator settings were tuned on the train seasons only; "
                     "test seasons were scored once with the frozen settings.") or "Test"
        with c2:
            tuned = meta.get("tuned_estimator")
            boundaries = f"**Train** {train[0]}–{train[1]} · **Test** {test[0]}–{test[1]}. " if train and test else ""
            tuning = (f"Estimator tuned on {tuned['train_seasons'][0]}–{tuned['train_seasons'][1]} only."
                      if tuned else "⚠ Estimator not tuned (defaults).")
            st.caption(boundaries + "Walk-forward everywhere: each week's params use only earlier games. " + tuning)
    with st.expander("Filters (apply to every view except the explorer)", expanded=False, icon=":material/tune:"):
        c1, c2, c3 = st.columns([2, 2, 1.2])
        with c1:
            season_range = st.slider("Seasons", int(seasons[0]), int(seasons[-1]),
                                     (int(seasons[0]), int(seasons[-1])), key="nfl_f_seasons")
            families = st.multiselect("Combo families", FAMILY_ORDER, default=FAMILY_ORDER, key="nfl_f_families")
        with c2:
            buckets = st.multiselect("Favorite size (|spread|)", [b[2] for b in bt.SPREAD_BUCKETS],
                                     default=[b[2] for b in bt.SPREAD_BUCKETS], key="nfl_f_buckets")
            primary = st.selectbox("Model shown in single-model views", models,
                                   index=models.index(meta["config"]["primary_model"]),
                                   format_func=lambda m: MODEL_LABELS.get(m, m), key="nfl_f_model")
        with c3:
            include_nested = st.toggle("Include nested combos", value=True, key="nfl_f_nested",
                                       help="Nested: one leg implies another (Fav covers ⇒ Fav wins; "
                                            "Dog wins ⇒ Dog covers). The naive product is structurally "
                                            "wrong for these, so they dominate headline gains.")
            game_types = st.segmented_control("Games", ["REG", "Playoffs"], selection_mode="multi",
                                              default=["REG", "Playoffs"], key="nfl_f_types")
    sel = combos["season"].between(*season_range) & combos["family"].isin(families) & combos["spread_bucket"].isin(buckets)
    gsel = games["season"].between(*season_range) & games["spread_bucket"].isin(buckets)
    if not include_nested:
        sel &= ~combos["nested"]
    types = set(game_types or [])
    if types != {"REG", "Playoffs"}:
        want_reg = "REG" in types
        want_po = "Playoffs" in types
        sel &= (combos["game_type"] == "REG") & want_reg | (combos["game_type"] != "REG") & want_po
        gsel &= (games["game_type"] == "REG") & want_reg | (games["game_type"] != "REG") & want_po
    both_combos = combos[sel]
    if has_split and sample != "All":
        sel &= combos["split"] == sample.lower()
        gsel &= games["split"] == sample.lower()
    return combos[sel], games[gsel], primary, both_combos


# ---------------------------------------------------------------------------
# 1. Overview
# ---------------------------------------------------------------------------

def _train_vs_test(both: pd.DataFrame, meta: Dict, primary: str) -> None:
    """Out-of-sample check: the same scores on the train and test periods side by side."""
    if "split" not in both.columns or both["split"].nunique() < 2:
        return
    st.subheader("Train vs test (out-of-sample check)")
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
            rows.append({"split": f"{split.title()} {seasons[0]}–{seasons[-1]}", "combos": label,
                         "games": part["game_id"].nunique(), "n": int(sc.loc[m, "n"]),
                         "brier_naive": sc.loc["naive", "brier"], "brier_model": sc.loc[m, "brier"],
                         "skill": sc.loc[m, "brier_skill_vs_naive"], "t": sc.loc[m, "t_stat"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"split": "Period", "combos": "Combos", "games": "Games", "n": "Scored",
                                "brier_naive": st.column_config.NumberColumn("Brier naive", format="%.5f"),
                                "brier_model": st.column_config.NumberColumn("Brier model", format="%.5f"),
                                "skill": st.column_config.NumberColumn("Skill vs naive", format="percent"),
                                "t": st.column_config.NumberColumn("t", format="%.1f")})
    est = meta["config"].get("estimator", {})
    st.caption(
        f"Frozen estimator ({MODEL_LABELS.get(primary, primary)}): window "
        f"{est.get('window_seasons') or 'all history'} seasons, half-life {est.get('half_life_seasons')} seasons, "
        f"variance shrink ×{est.get('var_shrink_multiplier')}. Filters above apply; the Sample selector does not."
    )


def _overview(combos: pd.DataFrame, games: pd.DataFrame, meta: Dict, primary: str,
              both_splits: Optional[pd.DataFrame] = None) -> None:
    vintage = meta.get("data_vintage", {})
    c = st.columns(3) + st.columns(3)
    c[0].metric("Games", f"{games['game_id'].nunique():,}")
    c[1].metric("Combos scored", f"{int((~combos['pushed']).sum()):,}")
    c[2].metric("Pushed (dropped)", f"{int(combos['pushed'].sum()):,}",
                help="Any leg landing exactly on its line. Polymarket's push rule (void leg vs void "
                     "combo) is still open, so pushed combos are excluded.")
    c[3].metric("Seasons", f"{combos['season'].min()}–{combos['season'].max()}" if len(combos) else "-")
    c[4].metric("Combo types", len(combos["combo"].unique()))
    c[5].metric("nflverse pull", vintage.get("pull_date", "-"), help=f"sha256 {vintage.get('sha256', '')[:16]}")

    if combos.empty or (~combos["pushed"]).sum() == 0:
        st.warning("No combos match the filters.")
        return

    cols = [bt.model_col(m) for m in meta["config"]["variance_models"]]
    scores = bt.score_table(combos, cols)
    prim = scores[scores["model"] == primary].iloc[0]
    naive = scores.iloc[0]

    st.subheader("Does modeling dependence beat the naive product?")
    k = st.columns(4)
    k[0].metric("Brier — naive product", f"{naive['brier']:.4f}")
    k[1].metric(f"Brier — {MODEL_LABELS[primary]}", f"{prim['brier']:.4f}",
                delta=f"{prim['brier_diff']:+.4f}", delta_color="inverse")
    k[2].metric("Brier skill vs naive", _pct(prim["brier_skill_vs_naive"], 2),
                help="1 − Brier(model)/Brier(naive). Positive = better than assuming independence.")
    k[3].metric("t-stat (game-clustered)", f"{prim['t_stat']:.1f}",
                help="Mean Brier difference / cluster-robust SE; combos in one game share its outcome.")

    left, right = st.columns([1.1, 1])
    with left:
        st.markdown("**Scores by model** (all filtered combos)")
        show = scores.copy()
        show["model"] = show["model"].map(lambda m: MODEL_LABELS.get(m, "Naive product" if m == "naive" else m))
        st.dataframe(
            show[["model", "n", "brier", "log_loss", "brier_skill_vs_naive", "brier_diff", "brier_diff_se", "t_stat",
                  "mean_abs_gap_vs_naive"]],
            hide_index=True, width="stretch",
            column_config={
                "model": "Model", "n": st.column_config.NumberColumn("Combos", format="%d"),
                "brier": st.column_config.NumberColumn("Brier", format="%.5f"),
                "log_loss": st.column_config.NumberColumn("Log loss", format="%.5f"),
                "brier_skill_vs_naive": st.column_config.NumberColumn("Skill vs naive", format="percent"),
                "brier_diff": st.column_config.NumberColumn("Δ Brier", format="%.5f"),
                "brier_diff_se": st.column_config.NumberColumn("SE", format="%.5f"),
                "t_stat": st.column_config.NumberColumn("t", format="%.1f"),
                "mean_abs_gap_vs_naive": st.column_config.NumberColumn("Mean |model − naive|", format="%.4f"),
            },
        )
    with right:
        st.markdown("**Brier skill vs naive, by combo family**")
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
        _chart((bars + err + rule).properties(height=300))

    if both_splits is not None:
        _train_vs_test(both_splits, meta, primary)

    st.subheader("Key findings (computed from the filtered data)")
    for line in _findings(combos, games, meta, primary):
        st.markdown(f"- {line}")


def _findings(combos: pd.DataFrame, games: pd.DataFrame, meta: Dict, primary: str) -> List[str]:
    col = bt.model_col(primary)
    out: List[str] = []
    fam = bt.group_table(combos, "family", col).set_index("family")
    if len(fam):
        best = fam["brier_skill"].idxmax()
        out.append(f"Largest gain over naive: **{best}** combos (Brier skill {_pct(fam.loc[best, 'brier_skill'], 1)}) — "
                   "legs that load on the same score dimension (margin) are strongly dependent, and the naive "
                   "product misprices them.")
        if "spread x total" in fam.index:
            out.append(f"**Spread x total** combos: Brier skill {_pct(fam.loc['spread x total', 'brier_skill'], 2)} — "
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
                   f"**{big['emp_corr_margin_total']:+.3f} ± {big['emp_corr_se']:.3f}** vs model "
                   f"{big['model_corr_margin_total']:+.3f}.")
    ml = bt.moneyline_consistency(games, meta["config"]["variance_models"])
    if len(ml) > 1:
        mkt = ml.iloc[0]
        mod = ml[ml["source"] == primary].iloc[0]
        out.append(f"Moneyline over-identification: model fav-win probability (from spread + total) differs from the "
                   f"market's by {mod['mean_abs_gap_vs_market']:.3f} on average (Brier {mod['brier']:.4f} vs market "
                   f"{mkt['brier']:.4f}); ML combos carry this marginal error on top of dependence.")
    return out


# ---------------------------------------------------------------------------
# 2. Combo pricing
# ---------------------------------------------------------------------------

def _combo_pricing(combos: pd.DataFrame, primary: str) -> None:
    col = bt.model_col(primary)
    st.markdown(
        "Every same-game combo from the favorite's perspective: one side of any two or three of "
        "**moneyline / spread / total** (e.g. *Chiefs ML + Chiefs −6.5 + over*, *Chiefs ML + opponent +6.5*). "
        "Prices are averages over games; realized is the hit rate (±1 SE)."
    )
    table = bt.group_table(combos, ["combo", "family", "n_legs", "nested"], col)
    if table.empty:
        st.warning("No combos match the filters.")
        return
    table["combo_label"] = table["combo"].map(combo_label)
    table = table.sort_values(["n_legs", "family", "combo"])

    long = table.melt(id_vars=["combo_label", "family", "realized_se", "realized"],
                      value_vars=["naive", "model"], var_name="price", value_name="p")
    order = list(table["combo_label"])
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
    _chart((err + dots).properties(height=26 * len(order) + 40))

    st.markdown("**Correlation lift over the naive product** — where naive gets it wrong, and whether the model sees it")
    lift = table.melt(id_vars=["combo_label"], value_vars=["realized_minus_naive", "model_minus_naive"],
                      var_name="what", value_name="lift")
    lift["what"] = lift["what"].map({"realized_minus_naive": "Realized − naive", "model_minus_naive": "Model − naive"})
    _chart(alt.Chart(lift).mark_bar().encode(
        y=alt.Y("combo_label:N", sort=order, title=None, axis=alt.Axis(labelLimit=320)),
        yOffset="what:N",
        x=alt.X("lift:Q", title="Probability points", axis=alt.Axis(format="%")),
        color=alt.Color("what:N", scale=alt.Scale(domain=["Realized − naive", "Model − naive"], range=[C_REAL, C_MODEL]),
                        title=None, legend=alt.Legend(orient="top")),
        tooltip=["combo_label", "what", alt.Tooltip("lift:Q", format="+.2%")],
    ).properties(height=26 * len(order) + 40))

    st.dataframe(
        table[["combo_label", "family", "n_legs", "nested", "n", "pushed", "realized", "realized_se", "naive", "model",
               "realized_minus_naive", "model_minus_naive", "brier_naive", "brier_model", "brier_skill"]],
        hide_index=True, width="stretch",
        column_config={
            "combo_label": "Combo", "family": "Family", "n_legs": "Legs", "nested": "Nested",
            "n": st.column_config.NumberColumn("Scored", format="%d"), "pushed": "Pushed",
            "realized": st.column_config.NumberColumn("Realized", format="percent"),
            "realized_se": st.column_config.NumberColumn("± SE", format="percent"),
            "naive": st.column_config.NumberColumn("Naive", format="percent"),
            "model": st.column_config.NumberColumn("Model", format="percent"),
            "realized_minus_naive": st.column_config.NumberColumn("Realized − naive", format="percent"),
            "model_minus_naive": st.column_config.NumberColumn("Model − naive", format="percent"),
            "brier_naive": st.column_config.NumberColumn("Brier naive", format="%.4f"),
            "brier_model": st.column_config.NumberColumn("Brier model", format="%.4f"),
            "brier_skill": st.column_config.NumberColumn("Skill", format="percent"),
        },
    )
    st.caption("Within a family the combos partition every outcome, so family-level average hit rates are "
               "exactly 1/k for realized and model alike — compare Brier, not family averages.")

    st.subheader("Results by combo size and favorite size")
    c1, c2 = st.columns(2)
    with c1:
        size = bt.group_table(combos, "n_legs", col)
        size["n_legs"] = size["n_legs"].map(lambda n: f"{n}-leg")
        st.dataframe(size[["n_legs", "n", "brier_naive", "brier_model", "brier_skill"]], hide_index=True, width="stretch",
                     column_config={"n_legs": "Combo size", "n": "Scored",
                                    "brier_naive": st.column_config.NumberColumn("Brier naive", format="%.4f"),
                                    "brier_model": st.column_config.NumberColumn("Brier model", format="%.4f"),
                                    "brier_skill": st.column_config.NumberColumn("Skill", format="percent")})
    with c2:
        buck = bt.group_table(combos, "spread_bucket", col)
        st.dataframe(buck[["spread_bucket", "n", "brier_naive", "brier_model", "brier_skill"]], hide_index=True,
                     width="stretch",
                     column_config={"spread_bucket": "|Spread|", "n": "Scored",
                                    "brier_naive": st.column_config.NumberColumn("Brier naive", format="%.4f"),
                                    "brier_model": st.column_config.NumberColumn("Brier model", format="%.4f"),
                                    "brier_skill": st.column_config.NumberColumn("Skill", format="percent")})

    metric = st.segmented_control("Heatmap metric", ["Realized − naive", "Model − naive", "Model − realized"],
                                  default="Realized − naive", key="nfl_heat_metric")
    grid = bt.group_table(combos, ["combo", "spread_bucket"], col)
    grid["combo_label"] = grid["combo"].map(combo_label)
    grid["value"] = {"Realized − naive": grid["realized_minus_naive"], "Model − naive": grid["model_minus_naive"],
                     "Model − realized": grid["model"] - grid["realized"]}[metric or "Realized − naive"]
    lim = float(np.nanmax(np.abs(grid["value"]))) if len(grid) else 0.1
    _chart(alt.Chart(grid).mark_rect().encode(
        x=alt.X("spread_bucket:N", sort=[b[2] for b in bt.SPREAD_BUCKETS], title="Favorite size (|spread|)"),
        y=alt.Y("combo_label:N", sort=order, title=None, axis=alt.Axis(labelLimit=320)),
        color=alt.Color("value:Q", scale=alt.Scale(scheme="redblue", domain=[-lim, lim], reverse=True),
                        title=metric, legend=alt.Legend(format="%")),
        tooltip=["combo_label", "spread_bucket", alt.Tooltip("value:Q", format="+.2%"), "n",
                 alt.Tooltip("realized:Q", format=".2%"), alt.Tooltip("naive:Q", format=".2%"),
                 alt.Tooltip("model:Q", format=".2%")],
    ).properties(height=26 * len(order) + 40))


# ---------------------------------------------------------------------------
# 3. Calibration
# ---------------------------------------------------------------------------

def _calibration(combos: pd.DataFrame, meta: Dict, primary: str) -> None:
    st.markdown("Events priced at 30% should hit ≈30%. Points are sized by count; bars are ±1 SE.")
    c1, c2 = st.columns([1, 2])
    with c1:
        width = st.select_slider("Bucket width", [0.025, 0.05, 0.1], value=0.05, key="nfl_cal_width")
        combo_opts = ["All filtered"] + sorted(combos["combo"].unique(), key=combo_label)
        pick = st.selectbox("Combo", combo_opts, format_func=lambda c: c if c == "All filtered" else combo_label(c),
                            key="nfl_cal_combo")
    sub = combos if pick == "All filtered" else combos[combos["combo"] == pick]
    if (~sub["pushed"]).sum() == 0:
        st.warning("No combos match.")
        return
    col = bt.model_col(primary)
    cal = pd.concat([bt.calibration_table(sub, "naive", width), bt.calibration_table(sub, col, width)])
    cal["source"] = cal["source"].map(lambda s: "Naive product" if s == "naive" else "Model")
    cal["lo"] = cal["realized"] - cal["realized_se"]
    cal["hi"] = cal["realized"] + cal["realized_se"]
    top = float(max(cal["predicted"].max(), cal["realized"].max())) + 0.05
    diag = alt.Chart(pd.DataFrame({"x": [0, top], "y": [0, top]})).mark_line(strokeDash=[4, 4], color="black").encode(x="x:Q", y="y:Q")
    color = alt.Scale(domain=["Naive product", "Model"], range=[C_NAIVE, C_MODEL])
    base = alt.Chart(cal).encode(x=alt.X("predicted:Q", title="Mean predicted probability", axis=alt.Axis(format="%"),
                                         scale=alt.Scale(domain=[0, top])),
                                 color=alt.Color("source:N", scale=color, title=None))
    pts = base.mark_circle(opacity=0.85).encode(
        y=alt.Y("realized:Q", title="Realized hit rate", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, top])),
        size=alt.Size("n:Q", legend=None, scale=alt.Scale(range=[30, 500])),
        tooltip=["source", "n", alt.Tooltip("predicted:Q", format=".2%"), alt.Tooltip("realized:Q", format=".2%")])
    bars = base.mark_rule().encode(y="lo:Q", y2="hi:Q")
    line = base.mark_line(opacity=0.5).encode(y="realized:Q")
    with c2:
        _chart((diag + bars + line + pts).properties(height=420))

    st.markdown("**Distribution of the model's correlation adjustment** (model − naive)")
    s = sub[~sub["pushed"]]
    gaps = pd.DataFrame({"gap": s[col] - s["naive"], "family": s["family"]})
    _chart(alt.Chart(gaps).mark_bar(opacity=0.8).encode(
        x=alt.X("gap:Q", bin=alt.Bin(maxbins=60), title="Model − naive (probability)", axis=alt.Axis(format="%")),
        y=alt.Y("count()", stack=True, title="Combos"),
        color=alt.Color("family:N", sort=FAMILY_ORDER, title=None),
    ).properties(height=260))


# ---------------------------------------------------------------------------
# 4. Correlation structure
# ---------------------------------------------------------------------------

def _structure(games: pd.DataFrame, history: pd.DataFrame, meta: Dict) -> None:
    if games.empty:
        st.warning("No games match the filters.")
        return
    st.markdown(
        "Residual = final score − closing-line implied points "
        "(home = (total + spread)/2, away = (total − spread)/2). The model says "
        "**Cov(margin, total) = σ²(fav) − σ²(dog)**: dependence between a favorite's margin and the total exists "
        "only because the team expected to score more has noisier scores."
    )
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Corr(favorite margin, total) by favorite size** — empirical ±1.96 SE vs model")
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
        _chart((base.mark_bar().encode(y=alt.Y("value:Q", title="Correlation"),
                                       tooltip=["spread_bucket", "series", alt.Tooltip("value:Q", format="+.3f"), "n_games"])
                + base.mark_rule(color="black").encode(y="lo:Q", y2="hi:Q")).properties(height=300))
    with c2:
        st.markdown("**Score volatility vs implied team points** — empirical ±1.96 SE vs model σ")
        sv = bt.sigma_vs_mu(games)
        base = alt.Chart(sv).encode(x=alt.X("mu:Q", title="Closing-line implied team points", scale=alt.Scale(zero=False)))
        emp = base.mark_circle(size=80, color=C_REAL).encode(
            y=alt.Y("emp_sigma:Q", title="Residual SD (points)", scale=alt.Scale(zero=False)),
            tooltip=["bin", "n", alt.Tooltip("emp_sigma:Q", format=".2f"), alt.Tooltip("model_sigma:Q", format=".2f")])
        err = base.mark_rule(color=C_REAL).encode(y="lo:Q", y2="hi:Q").transform_calculate(
            lo="datum.emp_sigma - 1.96 * datum.emp_sigma_se", hi="datum.emp_sigma + 1.96 * datum.emp_sigma_se")
        mod = base.mark_line(color=C_MODEL, point=True).encode(y="model_sigma:Q")
        _chart((err + emp + mod).properties(height=300))

    st.subheader("How outcomes co-move: joint-hit lift over independence")
    st.caption("Lift = P(A and B) − P(A)·P(B), using the model's market-calibrated marginals for P(A), P(B). "
               "Team totals use a half-point line at the closing-line implied team points.")
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
    _chart((heat + text).properties(width=300, height=300).facet(column=alt.Column("source:N", title=None)))
    show = lift.copy()
    show["pair"] = show["a"].map(OUTCOME_LABELS) + " & " + show["b"].map(OUTCOME_LABELS)
    show["gap_in_se"] = (show["model_lift"] - show["emp_lift"]) / show["emp_lift_se"]
    st.dataframe(show[["pair", "n", "emp_joint", "model_joint", "independent", "emp_lift", "emp_lift_se", "model_lift",
                       "gap_in_se", "emp_phi"]], hide_index=True, width="stretch",
                 column_config={"pair": "Outcome pair", "n": "Games",
                                "emp_joint": st.column_config.NumberColumn("Empirical joint", format="percent"),
                                "model_joint": st.column_config.NumberColumn("Model joint", format="percent"),
                                "independent": st.column_config.NumberColumn("Independence", format="percent"),
                                "emp_lift": st.column_config.NumberColumn("Empirical lift", format="percent"),
                                "emp_lift_se": st.column_config.NumberColumn("± SE", format="percent"),
                                "model_lift": st.column_config.NumberColumn("Model lift", format="percent"),
                                "gap_in_se": st.column_config.NumberColumn("(Model − emp) / SE", format="%.1f"),
                                "emp_phi": st.column_config.NumberColumn("Empirical φ", format="%.3f")})

    st.subheader("Stability over time")
    c1, c2 = st.columns(2)
    ss = bt.season_structure(games)
    with c1:
        st.markdown("**Within-game score correlation ρ by season**")
        long = ss.melt(id_vars=["season"], value_vars=["emp_rho", "model_rho"], var_name="s", value_name="rho")
        long["s"] = long["s"].map({"emp_rho": "Empirical (season)", "model_rho": "Model (walk-forward)"})
        _chart(alt.Chart(long).mark_line(point=True).encode(
            x=alt.X("season:O"), y=alt.Y("rho:Q", title="ρ"),
            color=alt.Color("s:N", scale=alt.Scale(domain=["Empirical (season)", "Model (walk-forward)"], range=[C_REAL, C_MODEL]), title=None, legend=alt.Legend(orient="top")),
            tooltip=["season", "s", alt.Tooltip("rho:Q", format="+.3f")]).properties(height=260))
    with c2:
        st.markdown("**Corr(favorite margin, total) by season**")
        long = ss.melt(id_vars=["season"], value_vars=["emp_corr_mt_fav", "model_corr_mt_fav"], var_name="s", value_name="corr")
        long["s"] = long["s"].map({"emp_corr_mt_fav": "Empirical (season)", "model_corr_mt_fav": "Model (walk-forward)"})
        _chart(alt.Chart(long).mark_line(point=True).encode(
            x=alt.X("season:O"), y=alt.Y("corr:Q", title="Correlation"),
            color=alt.Color("s:N", scale=alt.Scale(domain=["Empirical (season)", "Model (walk-forward)"], range=[C_REAL, C_MODEL]), title=None, legend=alt.Legend(orient="top")),
            tooltip=["season", "s", alt.Tooltip("corr:Q", format="+.3f")]).properties(height=260))

    st.markdown("**Walk-forward league parameters** (one estimate per week, games strictly before that week)")
    h = history.copy()
    h = h[h["season"].between(games["season"].min(), games["season"].max())]
    h["t"] = h["season"] + (h["week"] - 1) / 22.0
    param = st.segmented_control("Parameter", ["sigma_at_mean_points", "var_slope", "rho"], default="sigma_at_mean_points",
                                 format_func={"sigma_at_mean_points": "σ at league-average points",
                                              "var_slope": "Variance slope b", "rho": "ρ"}.get, key="nfl_param_hist")
    _chart(alt.Chart(h).mark_line().encode(
        x=alt.X("t:Q", title="Season", axis=alt.Axis(format="d")),
        y=alt.Y(f"{param or 'sigma_at_mean_points'}:Q", title=None, scale=alt.Scale(zero=False)),
        color=alt.Color("model:N", title=None, legend=alt.Legend(orient="top"),
                        scale=alt.Scale(domain=list(MODEL_LABELS)), ),
        tooltip=["season", "week", "model", alt.Tooltip(f"{param or 'sigma_at_mean_points'}:Q", format=".3f")],
    ).properties(height=260))

    st.markdown("**Moneyline over-identification** — model fav-win probability implied by spread + total vs the market's ML price")
    ml = bt.moneyline_consistency(games, meta["config"]["variance_models"])
    ml["source"] = ml["source"].map(lambda s: "Market ML price" if s == "market" else MODEL_LABELS.get(s, s))
    st.dataframe(ml, hide_index=True, width="stretch",
                 column_config={"source": "Source",
                                "mean_p_fav_win": st.column_config.NumberColumn("Mean P(fav wins)", format="percent"),
                                "brier": st.column_config.NumberColumn("Brier", format="%.4f"),
                                "mean_abs_gap_vs_market": st.column_config.NumberColumn("Mean |gap| vs market", format="%.4f")})
    st.caption(f"Realized favorite win rate: {_pct(ml.attrs.get('realized_fav_win_rate', float('nan')))} "
               f"over {ml.attrs.get('n', 0):,} games (ties excluded).")


# ---------------------------------------------------------------------------
# 5. Sensitivity & P&L
# ---------------------------------------------------------------------------

def _sensitivity(combos: pd.DataFrame, meta: Dict, primary_filter: str) -> None:
    cfg = meta["config"]
    primary = cfg.get("sensitivity_model") or cfg["primary_model"]
    st.markdown(
        f"Re-price every combo with the **{MODEL_LABELS[primary]}** model while scaling all modeled dependence "
        "by *c*: ρ → c·ρ and the favorite/underdog variance asymmetry → c×. **c = 0** makes margin and total "
        "independent (identical to the naive product for spread x total combos, because marginals are "
        "market-calibrated); c = 1 is the fitted model. Legs on the *same* score dimension (ML x spread) stay "
        "fully dependent at every c."
    )
    fams = st.multiselect("Families for this view", FAMILY_ORDER, default=["spread x total", "ML x total"],
                          key="nfl_sens_fams")
    sub = combos[combos["family"].isin(fams)]
    if (~sub["pushed"]).sum() == 0:
        st.warning("No combos match.")
        return
    thr = st.slider("Edge threshold |model − naive| to trade", 0.0, 0.05, float(cfg.get("edge_threshold", 0.01)), 0.0025,
                    format="%.4f", key="nfl_sens_thr")
    sens = bt.sensitivity_table(sub, cfg["corr_scales"], primary, thr)
    base_brier = bt.score_table(sub, []).iloc[0]["brier"]
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Brier skill vs naive as correlation is scaled**")
        s = sens.assign(lo=(-(sens["brier"] - base_brier) - 1.96 * sens["brier_diff_se"]) / base_brier,
                        hi=(-(sens["brier"] - base_brier) + 1.96 * sens["brier_diff_se"]) / base_brier)
        base = alt.Chart(s).encode(x=alt.X("corr_scale:Q", title="Correlation scale c"))
        _chart((base.mark_area(opacity=0.15, color=C_MODEL).encode(y="lo:Q", y2="hi:Q")
                + base.mark_line(point=True, color=C_MODEL).encode(
                    y=alt.Y("brier_skill_vs_naive:Q", title="Brier skill vs naive", axis=alt.Axis(format="%")),
                    tooltip=[alt.Tooltip("corr_scale:Q"), alt.Tooltip("brier_skill_vs_naive:Q", format=".3%")])
                ).properties(height=280))
    with c2:
        st.markdown("**Edge P&L vs a naive-pricing counterparty**")
        base = alt.Chart(sens).encode(x=alt.X("corr_scale:Q", title="Correlation scale c"))
        _chart((base.mark_bar(opacity=0.35, color=C_NAIVE).encode(y=alt.Y("n_trades:Q", title="Trades"))
                + base.mark_line(point=True, color=C_REAL).encode(
                    y=alt.Y("total_pnl:Q", title="Total P&L ($1 payout units)"),
                    tooltip=["corr_scale", "n_trades", alt.Tooltip("total_pnl:Q", format=",.1f"),
                             alt.Tooltip("pnl_t_stat:Q", format=".1f")])
                ).resolve_scale(y="independent").properties(height=280))
    st.dataframe(sens.drop(columns=["column"]), hide_index=True, width="stretch",
                 column_config={"corr_scale": "c", "brier": st.column_config.NumberColumn("Brier", format="%.5f"),
                                "log_loss": st.column_config.NumberColumn("Log loss", format="%.5f"),
                                "brier_skill_vs_naive": st.column_config.NumberColumn("Skill", format="percent"),
                                "brier_diff_se": st.column_config.NumberColumn("SE Δ Brier", format="%.5f"),
                                "mean_abs_gap_vs_naive": st.column_config.NumberColumn("Mean |gap|", format="%.4f"),
                                "n_trades": "Trades", "total_pnl": st.column_config.NumberColumn("P&L", format="%.1f"),
                                "pnl_per_trade": st.column_config.NumberColumn("P&L / trade", format="%.4f"),
                                "pnl_t_stat": st.column_config.NumberColumn("t", format="%.1f"),
                                "win_rate": st.column_config.NumberColumn("Win rate", format="percent"),
                                "max_downswing": st.column_config.NumberColumn("Max downswing", format="%.1f"),
                                "max_upswing": st.column_config.NumberColumn("Max upswing", format="%.1f")})

    st.subheader("Cumulative edge P&L over time")
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
        _chart(alt.Chart(cur).mark_line().encode(
            x=alt.X("gameday:T", title=None), y=alt.Y("cum_pnl:Q", title="Cumulative P&L ($1 payout units)"),
            color=alt.Color("c:N", title="Correlation scale", scale=alt.Scale(scheme="viridis")),
            tooltip=["c", alt.Tooltip("gameday:T"), alt.Tooltip("cum_pnl:Q", format=",.2f")],
        ).properties(height=320))

    st.markdown(f"**P&L by season and family** (fitted model, threshold {thr:.4f})")
    t = bt.edge_pnl(sub, bt.model_col(primary), thr)
    t = t[t["side"] != 0]
    by = t.groupby(["season", "family"], as_index=False)["pnl"].sum()
    _chart(alt.Chart(by).mark_bar().encode(
        x=alt.X("season:O"), y=alt.Y("pnl:Q", title="P&L"), color=alt.Color("family:N", sort=FAMILY_ORDER, title=None),
        tooltip=["season", "family", alt.Tooltip("pnl:Q", format=",.2f")]).properties(height=260))
    st.caption("Stylized: buy (sell) one $1-payout combo at the naive product when the model is above (below) it by more "
               "than the threshold. It isolates the value of modeling dependence — it is **not** a live P&L forecast: "
               "real books price some same-game correlation, charge vig, and RFQ flow selects against the quoter.")


# ---------------------------------------------------------------------------
# 6. Combo explorer
# ---------------------------------------------------------------------------

def _default_params(meta: Dict) -> Optional[Dict]:
    files = list_params(PARAMS_DIR)
    if files:
        return load_params(files[-1][1])
    return None


def _explorer(meta: Dict) -> None:
    st.markdown(
        "Build a same-game combo — pick one side of any of the game's markets — and price it with the "
        "correlation model vs the naive product. Take the perspective of any team (e.g. the Chiefs): "
        "*team ML + team spread + over*, *team ML + opponent spread*, …"
    )
    params = _default_params(meta)
    files = list_params(PARAMS_DIR)
    pull = latest_pull(RAW_ROOT)

    left, right = st.columns([1, 1.35], gap="large")
    with left:
        source = st.segmented_control("Game", ["Historical game", "Hypothetical"], default="Historical game",
                                      key="nfl_x_source") or "Historical game"
        game = None
        if source == "Historical game":
            if pull is None:
                st.info("No nflverse data cached yet — use Params & data → Pull data, or switch to Hypothetical.")
            else:
                all_games = [g for g in _load_games_table(str(pull.directory), pull.sha256) if g.has_lines]
                seasons = sorted({g.season for g in all_games}, reverse=True)
                latest_played = max((g.order_key for g in all_games if g.played), default=None)
                c1, c2 = st.columns(2)
                season = c1.selectbox("Season", seasons, key="nfl_x_season",
                                      index=seasons.index(latest_played[0]) if latest_played else 0)
                weeks = sorted({g.week for g in all_games if g.season == season})
                played_weeks = [w for w in weeks if any(g.played for g in all_games if g.order_key == (season, w))]
                default_week = played_weeks[-1] if played_weeks else weeks[-1]
                week = c2.selectbox("Week", weeks, index=weeks.index(default_week), key=f"nfl_x_week_{season}")
                week_games = sorted([g for g in all_games if g.season == season and g.week == week], key=lambda g: g.game_id)
                game = st.selectbox("Matchup", week_games, key="nfl_x_game",
                                    format_func=lambda g: f"{g.away} @ {g.home}  (home {-g.spread_line:+g}, total {g.total_line:g})"
                                    + (f" — final {g.away_score}-{g.home_score}" if g.played else " — not played"))
        if game is not None:
            home, away = game.home, game.away
            default_spread_home, default_total = game.spread_line, game.total_line
            p_home_cover_mkt = devig_pair(game.home_spread_odds, game.away_spread_odds) or 0.5
            p_over_mkt = devig_pair(game.over_odds, game.under_odds) or 0.5
            p_home_ml_mkt = devig_pair(game.home_moneyline, game.away_moneyline)
        else:
            c1, c2 = st.columns(2)
            home = c1.text_input("Home team", "KC", key="nfl_x_home")
            away = c2.text_input("Away team", "BUF", key="nfl_x_away")
            default_spread_home, default_total = 3.5, 47.5
            p_home_cover_mkt = p_over_mkt = 0.5
            p_home_ml_mkt = None

        team = st.radio("Perspective team", [home, away], horizontal=True, key=f"nfl_x_team_{home}_{away}")
        opp = away if team == home else home
        team_is_home = team == home
        c1, c2 = st.columns(2)
        team_hcap = c1.number_input(f"{team} spread (handicap)", value=float(-default_spread_home if team_is_home else default_spread_home),
                                    step=0.5, key=f"nfl_x_hcap_{home}_{away}_{team}",
                                    help="Sportsbook convention: −6.5 means the team must win by 7+.")
        total = c2.number_input("Game total", value=float(default_total), step=0.5, key=f"nfl_x_total_{home}_{away}")
        spread_line = -team_hcap if team_is_home else team_hcap   # home expected-margin convention
        with st.expander("Market prices (de-vigged)", expanded=False):
            p_team_cover = st.slider(f"P({team} covers)", 0.2, 0.8,
                                     float(p_home_cover_mkt if team_is_home else 1 - p_home_cover_mkt), 0.005,
                                     key=f"nfl_x_pcov_{home}_{away}_{team}")
            p_over = st.slider("P(over)", 0.2, 0.8, float(p_over_mkt), 0.005, key=f"nfl_x_pover_{home}_{away}")
            ml_default = None if p_home_ml_mkt is None else (p_home_ml_mkt if team_is_home else 1 - p_home_ml_mkt)
            use_ml = st.toggle("Use a market moneyline price for the naive product", value=ml_default is not None,
                               key=f"nfl_x_useml_{home}_{away}")
            p_team_ml = st.slider(f"P({team} wins)", 0.02, 0.98, float(ml_default if ml_default is not None else 0.5), 0.005,
                                  key=f"nfl_x_pml_{home}_{away}_{team}", disabled=not use_ml)

        st.markdown("**Combo legs** — one side per market")
        ml_side = st.segmented_control("Moneyline", [f"{team} ML", f"{opp} ML"], key=f"nfl_x_ml_{team}")
        sp_side = st.segmented_control("Spread", [f"{team} {team_hcap:+g}", f"{opp} {-team_hcap:+g}"], key=f"nfl_x_sp_{team}")
        tot_side = st.segmented_control("Total", [f"Over {total:g}", f"Under {total:g}"], key=f"nfl_x_tot_{team}")

        with st.expander("Model settings", expanded=False):
            model = st.selectbox("Variance model", list(VARIANCE_MODELS),
                                 index=list(VARIANCE_MODELS).index(meta["config"]["primary_model"]),
                                 format_func=lambda m: MODEL_LABELS[m], key="nfl_x_model")
            file_names = [p.name for _, p in files]
            chosen = st.selectbox("Params file", file_names or ["(none — league defaults)"],
                                  index=max(len(file_names) - 1, 0), key="nfl_x_file")
            corr_scale = st.slider("Correlation scale c", 0.0, 2.0, 1.0, 0.1, key="nfl_x_scale")
            override = st.toggle("Override σ / ρ manually", value=False, key="nfl_x_override")
            sig_h = st.slider(f"σ {home}", 5.0, 16.0, 9.5, 0.1, key="nfl_x_sigh", disabled=not override)
            sig_a = st.slider(f"σ {away}", 5.0, 16.0, 9.0, 0.1, key="nfl_x_siga", disabled=not override)
            rho = st.slider("ρ (within-game)", -0.5, 0.5, 0.05, 0.01, key="nfl_x_rho", disabled=not override)

    if files and chosen in [p.name for _, p in files]:
        params = load_params(PARAMS_DIR / chosen)
    if params is None:
        params = {"league": {"variance_model": "mean_linear", "var_intercept": 58.0, "var_slope": 1.07, "rho": 0.05,
                             "sigma_min": 5.0, "sigma_max": 16.0}, "teams": {}}
    params = json.loads(json.dumps(params))
    params["league"]["variance_model"] = model
    if model == "league_constant":
        mp = params["league"].get("mean_points", 22.5)
        params["league"]["var_intercept"] = params["league"]["var_intercept"] + params["league"]["var_slope"] * mp
        params["league"]["var_slope"] = 0.0

    def cov_fn(mh: float, ma: float):
        if override:
            base_cov = MatchupCovariance(sig_h, sig_a, rho)
            if corr_scale == 1.0:
                return base_cov
            vbar = 0.5 * (sig_h ** 2 + sig_a ** 2)
            vh = vbar + corr_scale * (sig_h ** 2 - vbar)
            va = vbar + corr_scale * (sig_a ** 2 - vbar)
            return MatchupCovariance(math.sqrt(max(vh, 1e-6)), math.sqrt(max(va, 1e-6)), max(min(corr_scale * rho, 0.95), -0.95))
        return matchup_covariance(params, home, away, mh, ma, corr_scale=corr_scale)

    p_home_cover = p_team_cover if team_is_home else 1 - p_team_cover
    cal = calibrate_means(spread_line, p_home_cover, total, p_over, cov_fn)
    gm = GameModel((cal.mu_home, cal.mu_away), cal.cov)
    team_ml_leg = home_ml() if team_is_home else away_ml()
    opp_ml_leg = away_ml() if team_is_home else home_ml()
    team_cov_leg = home_cover(spread_line) if team_is_home else away_cover(spread_line)
    opp_cov_leg = away_cover(spread_line) if team_is_home else home_cover(spread_line)
    p_team_ml_model = gm.leg(team_ml_leg)
    p_ml_naive = p_team_ml if use_ml else p_team_ml_model
    catalog = {
        f"{team} ML": (team_ml_leg, p_ml_naive), f"{opp} ML": (opp_ml_leg, 1 - p_ml_naive),
        f"{team} {team_hcap:+g}": (team_cov_leg, p_team_cover), f"{opp} {-team_hcap:+g}": (opp_cov_leg, 1 - p_team_cover),
        f"Over {total:g}": (over(total), p_over), f"Under {total:g}": (under(total), 1 - p_over),
    }
    chosen_legs = [s for s in (ml_side, sp_side, tot_side) if s]

    with right:
        m = st.columns(2) + st.columns(2)
        m[0].metric(f"{home} implied pts", f"{cal.mu_home:.1f}", help="Market-calibrated score mean")
        m[1].metric(f"{away} implied pts", f"{cal.mu_away:.1f}", help="Market-calibrated score mean")
        m[2].metric("σ home / away", f"{cal.cov.sigma_home:.1f} / {cal.cov.sigma_away:.1f}")
        m[3].metric("Corr(margin, total)", f"{cal.cov.corr_margin_total:+.3f}",
                    help=f"Home margin vs game total. ρ(home, away scores) = {cal.cov.rho:+.3f}")

        if chosen_legs:
            legs = [catalog[s][0] for s in chosen_legs]
            p_model = gm.joint(legs)
            p_naive = math.prod(catalog[s][1] for s in chosen_legs)
            st.markdown(f"#### {' + '.join(chosen_legs)}")
            k = st.columns(3)
            k[0].metric("Model", _pct(p_model, 2), help="Joint probability, conditional on no leg pushing.")
            k[1].metric("Naive", _pct(p_naive, 2), help="Product of the leg prices.")
            k[2].metric("Adj. (bps)", f"{(p_model - p_naive) * 1e4:+,.0f}", help="Model − naive, basis points.")
            realized = ""
            if game is not None and game.played:
                res = [_settle(leg, game) for leg in legs]
                realized = " · realized: " + ("PUSH" if "push" in res else ("WON" if all(r == "win" for r in res) else "LOST"))
            st.caption(f"Fair American odds — model {_american(p_model)} · naive {_american(p_naive)}{realized}")
            leg_rows = [{"leg": s, "naive_p": catalog[s][1], "model_p": gm.leg(catalog[s][0])} for s in chosen_legs]
            st.dataframe(pd.DataFrame(leg_rows), hide_index=True, width="stretch",
                         column_config={"leg": "Leg",
                                        "naive_p": st.column_config.NumberColumn("Market / naive P", format="percent"),
                                        "model_p": st.column_config.NumberColumn("Model P", format="percent")})
        else:
            legs = []
            st.info("Pick at least one leg on the left.")

        _score_heatmap(gm, legs, home, away, game)

    st.markdown(f"**Every same-game combo for this game** ({team}'s perspective)")
    rows = []
    for ml_s in (None, f"{team} ML", f"{opp} ML"):
        for sp_s in (None, f"{team} {team_hcap:+g}", f"{opp} {-team_hcap:+g}"):
            for t_s in (None, f"Over {total:g}", f"Under {total:g}"):
                sel = [s for s in (ml_s, sp_s, t_s) if s]
                if len(sel) < 2:
                    continue
                lg = [catalog[s][0] for s in sel]
                pm = gm.joint(lg)
                if pm < 1e-9:
                    continue  # logically impossible (e.g. underdog wins AND favorite covers)
                pn = math.prod(catalog[s][1] for s in sel)
                row = {"combo": " + ".join(sel), "legs": len(sel), "model": pm, "naive": pn, "adj_bps": (pm - pn) * 1e4}
                if game is not None and game.played:
                    res = [_settle(l, game) for l in lg]
                    row["result"] = "push" if "push" in res else ("won" if all(r == "win" for r in res) else "lost")
                rows.append(row)
    st.dataframe(pd.DataFrame(rows).sort_values(["legs", "adj_bps"]), hide_index=True, width="stretch",
                 column_config={"model": st.column_config.NumberColumn("Model", format="percent"),
                                "naive": st.column_config.NumberColumn("Naive", format="percent"),
                                "adj_bps": st.column_config.NumberColumn("Model − naive (bps)", format="%+.0f"),
                                "combo": "Combo", "legs": "Legs", "result": "Result"})


def _american(p: float) -> str:
    if p <= 0 or p >= 1:
        return "-"
    return f"{-100 * p / (1 - p):+.0f}" if p >= 0.5 else f"+{100 * (1 - p) / p:.0f}"


def _settle(leg, game) -> str:
    from combo_mm.nfl.joint import settle_leg
    return settle_leg(leg, game.home_score, game.away_score)


def _score_heatmap(gm: GameModel, legs, home: str, away: str, game) -> None:
    """Integer-score probability mass with the cells where every chosen leg wins highlighted."""
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
                       "combo": np.where(win.ravel(), "Combo wins", "Combo loses") if legs else "—"})
    enc = dict(x=alt.X("home:O", title=f"{home} points", axis=alt.Axis(labelOverlap=True, values=list(range(0, 80, 7)))),
               y=alt.Y("away:O", title=f"{away} points", sort="descending", axis=alt.Axis(labelOverlap=True, values=list(range(0, 80, 7)))))
    heat = alt.Chart(df).mark_rect().encode(
        **enc,
        color=alt.Color("p:Q", scale=alt.Scale(scheme="blues"), legend=None),
        opacity=alt.condition(alt.datum.combo == "Combo loses", alt.value(0.2), alt.value(1.0)),
        stroke=alt.condition(alt.datum.combo == "Combo wins", alt.value(C_REAL), alt.value(None)),
        strokeWidth=alt.value(0.4),
        tooltip=["home", "away", alt.Tooltip("p:Q", format=".3%"), "combo"],
    )
    layers = heat
    if game is not None and game.played:
        final = pd.DataFrame({"home": [game.home_score], "away": [game.away_score]})
        layers = heat + alt.Chart(final).mark_point(shape="cross", size=250, color="black", filled=True).encode(**enc)
    st.markdown("**Score distribution** — highlighted cells: every chosen leg wins" + (" · ✚ final score" if game is not None and game.played else ""))
    _chart(layers.properties(height=380))


# ---------------------------------------------------------------------------
# 7. Params & data
# ---------------------------------------------------------------------------

def _params_and_data(meta: Dict) -> None:
    pull = latest_pull(RAW_ROOT)
    c = st.columns(3)
    c[0].metric("Cached nflverse pull", pull.pull_date if pull else "none")
    c[1].metric("Backtest generated", pd.to_datetime(meta.get("generated_at_unix", 0), unit="s").strftime("%Y-%m-%d %H:%M UTC"))
    c[2].metric("Backtest runtime", f"{meta.get('runtime_s', 0):.0f}s on {meta.get('workers', 1)} workers")

    b = st.columns(3)
    if b[0].button("Pull latest nflverse data", key="nfl_pull"):
        _run_script(["-c", "from combo_mm.nfl.ingest import pull_games; p = pull_games('data/raw', force=True); "
                           "print(p.directory, p.sha256, p.manifest['validation'])"], "Pulling nflverse games.csv")
    if b[1].button("Refresh weekly params", key="nfl_refresh"):
        _run_script(["scripts/refresh_params.py"], "Estimating params + running gates")
    if b[2].button("Re-run backtest", key="nfl_rerun", type="primary"):
        _run_script(["scripts/nfl_backtest.py"], "Running walk-forward backtest")
        st.rerun()

    st.subheader("Estimator tuning (train period only)")
    selection = PARAMS_DIR / "estimator.json"
    grid_path = REPO / "results" / "nfl_tuning" / "grid.csv"
    if selection.exists():
        sel = json.loads(selection.read_text())
        t = st.columns(4)
        t[0].metric("Tuned on", f"{sel['train_seasons'][0]}–{sel['train_seasons'][1]}")
        t[1].metric("Selected model", MODEL_LABELS.get(sel["variance_model"], sel["variance_model"]))
        t[2].metric("Train Brier", f"{sel['train_brier']:.5f}", help=f"Naive: {sel['train_brier_naive']:.5f}")
        t[3].metric("Candidates", sel["n_candidates"])
        st.caption(f"Frozen estimator: `{json.dumps(sel['estimator'])}` — used by the backtest's test period and the "
                   "weekly refresh. Test-season games are removed from the input before tuning runs.")
    else:
        st.info("No tuned estimator yet: `python scripts/nfl_tune.py` (train-period grid search).")
    if grid_path.exists():
        grid = pd.read_csv(grid_path)
        grid["window_seasons"] = grid["window_seasons"].map(lambda w: "all" if pd.isna(w) else f"{int(w)}")
        grid["variance_model"] = grid["variance_model"].map(lambda m: MODEL_LABELS.get(m, m))
        with st.expander(f"Tuning grid — {len(grid)} candidates ranked by train Brier", expanded=False):
            st.dataframe(grid.drop(columns=["grid_index"]), hide_index=True, width="stretch",
                         column_config={"brier": st.column_config.NumberColumn("Brier", format="%.5f"),
                                        "brier_naive": st.column_config.NumberColumn("Brier naive", format="%.5f"),
                                        "brier_skill": st.column_config.NumberColumn("Skill", format="percent"),
                                        "brier_skill_non_nested": st.column_config.NumberColumn("Skill (non-nested)", format="percent"),
                                        "brier_skill_spread_total": st.column_config.NumberColumn("Skill (spread x total)", format="percent"),
                                        "log_loss": st.column_config.NumberColumn("Log loss", format="%.5f"),
                                        "t_stat": st.column_config.NumberColumn("t", format="%.1f")})

    files = list_params(PARAMS_DIR)
    st.subheader("Weekly params files")
    if not files:
        st.info("No promoted params files yet (`python scripts/refresh_params.py --pull`).")
        return
    name = st.selectbox("File", [p.name for _, p in files], index=len(files) - 1, key="nfl_params_file")
    params = load_params(PARAMS_DIR / name)
    lg = params["league"]
    k = st.columns(5)
    k[0].metric("Variance model", MODEL_LABELS.get(lg["variance_model"], lg["variance_model"]))
    k[1].metric("σ at league-avg points", f"{lg['sigma_at_mean_points']:.2f}")
    k[2].metric("Variance slope b", f"{lg['var_slope']:.3f}", help="σ²(μ) = a + b·μ")
    k[3].metric("ρ", f"{lg['rho']:+.3f}")
    k[4].metric("Games in window", f"{lg['n_games']:,}", help=f"Seasons {lg['seasons']}")
    st.caption(f"As of {params['as_of']} · data vintage {params['data_vintage'].get('pull_date')} · "
               f"model {params['model_version']}")
    if params["games"]:
        st.markdown("**Slate covariance** (at closing/current lines)")
        gdf = pd.DataFrame(params["games"])
        st.dataframe(gdf[["game_id", "away", "home", "spread_line", "total_line", "mu_home", "mu_away", "sigma_home",
                          "sigma_away", "rho", "corr_margin_total"]], hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="%.3f") for c in
                                    ("mu_home", "mu_away", "sigma_home", "sigma_away", "rho", "corr_margin_total")})
    with st.expander("Team factors"):
        tdf = pd.DataFrame(params["teams"]).T.reset_index(names="team").sort_values("off_var_factor", ascending=False)
        st.dataframe(tdf, hide_index=True, width="stretch")
    report = PARAMS_DIR / "reports" / name.replace(".json", ".gates.json")
    if report.exists():
        with st.expander("Gate report", expanded=True):
            rep = json.loads(report.read_text())
            for gate in rep["gates"]:
                st.markdown(f"{'✅' if gate['passed'] else '❌'} **{gate['name']}** — `{json.dumps(gate['detail'])}`")
    st.download_button("Download params JSON", (PARAMS_DIR / name).read_bytes(), file_name=name, mime="application/json")

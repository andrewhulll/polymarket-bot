"""NFL adapter for the backtest harness (issue #5 item B3).

Builds a registry-backed :class:`NflJointPricer` from a correlation params
file and the dataset's own registry snapshot. Lives here -- not in the
runner -- so the runner stays leg-type agnostic and Phase B leg modules can
add their own adapters later.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

__all__ = ["build_nfl_pricer", "NflPricerFactory"]


def build_nfl_pricer(dataset: Any, params: Dict[str, Any],
                     corr_scale: float = 1.0) -> Any:
    """One :class:`GameModel` per game from market-implied means.

    Means come from the main spread/total lines
    (``mu_home = (total + spread)/2``); covariance from
    :func:`params_io.matchup_covariance` with ``corr_scale`` applied.
    ``corr_scale=0`` reproduces the naive product price for the corr sweep.
    """
    from combo_mm.nfl.estimate import implied_means
    from combo_mm.nfl.joint import GameModel
    from combo_mm.nfl.joint_pricer import NflJointPricer
    from combo_mm.nfl.markets import LegRegistry, to_joint_leg
    from combo_mm.nfl.params_io import matchup_covariance

    registry = LegRegistry.load(dataset.root / "markets.json")
    by_game: Dict[str, list] = {}
    for raw in dataset.markets.get("markets", []):
        by_game.setdefault(raw["game_id"], []).append(raw["symbol"])

    pricer = NflJointPricer()
    for game_id, symbols in by_game.items():
        markets = [m for m in (registry.get(s) for s in symbols) if m is not None]
        if not markets:
            continue
        main_spr = next(
            (m for m in markets if m.kind == "SPR" and m.is_main_line), None)
        main_tot = next(
            (m for m in markets if m.kind == "TOT" and m.is_main_line), None)
        if (main_spr is None or main_tot is None
                or main_spr.line is None or main_tot.line is None):
            continue
        home_margin = (-main_spr.line if main_spr.subject_is_home
                       else main_spr.line)
        mu_home, mu_away = implied_means(home_margin, main_tot.line)
        cov = matchup_covariance(params, markets[0].home, markets[0].away,
                                 mu_home, mu_away, corr_scale=corr_scale)
        pricer.register_game(
            game_id, GameModel((mu_home, mu_away), cov),
            {m.symbol: to_joint_leg(m, "YES") for m in markets})
    return pricer


class NflPricerFactory:
    """Picklable ``corr_scale -> pricer`` factory (for worker processes).

    Holds only the dataset root and the params dict, so
    :func:`sensitivity.run_sensitivity` can ship it to a
    :class:`~concurrent.futures.ProcessPoolExecutor` pool.
    """

    def __init__(self, dataset_root: Any, params: Dict[str, Any]) -> None:
        self.dataset_root = Path(dataset_root)
        self.params = dict(params)

    def __call__(self, corr_scale: float) -> Any:
        from combo_mm.backtest.dataset import open_dataset

        return build_nfl_pricer(open_dataset(self.dataset_root),
                                self.params, corr_scale=corr_scale)

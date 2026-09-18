"""Sensitivity orchestration (issue #5 item D, second half).

Grid: correlation scale ``[0, 0.5, 1, 1.5, 2]`` at default fill-model knobs,
plus one-knob-at-a-time fill-model variations at ``corr_scale=1``:

- ``competitor_half_spread`` in ``[0.015, 0.025, 0.035]``
- ``retail_bias_mean`` in ``[-0.01, 0.01, 0.03]``
- ``sharp_share`` in ``[0.0, 0.05, 0.15]``

Each cell replays the full dataset and records the key numbers, so
``sensitivity.csv`` shows how much the edge depends on the correlation
model versus the counterfactual fill assumptions. The corr scale enters
through the caller's ``pricer_factory`` (``params_io.matchup_covariance``
already accepts ``corr_scale``); this module never hardcodes leg types.

Cells are independent: ``workers > 1`` fans them out over a
:class:`~concurrent.futures.ProcessPoolExecutor` (the pricer factory must
be picklable, e.g. :class:`NflPricerFactory`). Every scenario uses the
same dataset and the same fill-model seed, and rows come back in grid
order regardless of worker count.
"""
from __future__ import annotations

import copy
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

from combo_mm.backtest.dataset import Dataset
from combo_mm.backtest.runner import BacktestConfig, run_backtest

__all__ = ["SensitivitySpec", "run_sensitivity"]

_DEFAULT_SPEC: Dict[str, List[Any]] = {
    "corr_scale": [0.0, 0.5, 1.0, 1.5, 2.0],
    "competitor_half_spread": [0.015, 0.025, 0.035],
    "retail_bias_mean": [-0.01, 0.01, 0.03],
    "sharp_share": [0.0, 0.05, 0.15],
}


@dataclass
class SensitivitySpec:
    corr_scales: List[float] = field(
        default_factory=lambda: list(_DEFAULT_SPEC["corr_scale"]))
    fill_knobs: Dict[str, List[Any]] = field(default_factory=lambda: {
        k: list(v) for k, v in _DEFAULT_SPEC.items() if k != "corr_scale"})


def _cell(dataset: Dataset, base: BacktestConfig,
          pricer_factory: Callable[[float], Any],
          corr_scale: float, knob: str, value: Any) -> Dict[str, Any]:
    cfg = copy.deepcopy(base)
    cfg.pricer = pricer_factory(corr_scale)
    if knob != "corr_scale":
        setattr(cfg.fill, knob, value)
    result = run_backtest(dataset, cfg)
    m = result.metrics
    return {
        "corr_scale": corr_scale,
        "knob": knob,
        "knob_value": value,
        "rfqs_received": m.rfqs_received,
        "rfqs_quoted": m.rfqs_quoted,
        "rfqs_executed": m.rfqs_executed,
        "n_fills": m.n_fills,
        "expected_pnl": m.expected_pnl,
        "expected_pnl_naive_basis": m.expected_pnl_naive_basis,
        "realized_pnl": m.realized_pnl,
        "max_downswing": m.max_downswing,
        "max_upswing": m.max_upswing,
        "brier_ours": m.brier_ours,
        "brier_naive": m.brier_naive,
        "state_digest": result.state_digest,
        "wall_seconds": result.wall_seconds,
    }


def _sensitivity_worker(
        payload: Tuple[Dict[str, Any], Dict[str, Any], Any,
                       float, str, Any]) -> Dict[str, Any]:
    """Process-pool entry point: rebuild config/dataset, replay one cell."""
    from combo_mm.backtest.dataset import Dataset as _Dataset

    dataset_kwargs, base_kwargs, factory, corr_scale, knob, value = payload
    dataset = _Dataset(**dataset_kwargs)
    base = BacktestConfig(**base_kwargs)
    return _cell(dataset, base, factory, corr_scale, knob, value)


def run_sensitivity(dataset: Dataset, base: BacktestConfig,
                    pricer_factory: Callable[[float], Any],
                    spec: SensitivitySpec | None = None,
                    workers: int = 1) -> List[Dict[str, Any]]:
    """Replay the dataset over the full sensitivity grid; return CSV rows.

    ``workers > 1`` evaluates cells in a process pool; the factory must be
    picklable (see :class:`combo_mm.backtest.nfl.NflPricerFactory`). Rows
    always come back in grid order.
    """
    spec = spec or SensitivitySpec()
    cells: List[Tuple[float, str, Any]] = []
    # corr sweep at default knobs
    for cs in spec.corr_scales:
        cells.append((cs, "corr_scale", cs))
    # one knob at a time, corr fixed at 1
    for knob, values in spec.fill_knobs.items():
        for value in values:
            cells.append((1.0, knob, value))
    if workers and workers > 1:
        dataset_kwargs = {
            "root": dataset.root, "manifest": dataset.manifest,
            "base": dataset.base, "combos": dataset.combos,
            "markets": dataset.markets,
        }
        base_kwargs = {
            "pipeline": copy.deepcopy(base.pipeline),
            "fill": copy.deepcopy(base.fill),
            "quote_latency_ms": base.quote_latency_ms,
            "store_path": ":memory:",
            "params_version": base.params_version,
        }
        payloads = [(dataset_kwargs, base_kwargs, pricer_factory,
                     cs, knob, value) for cs, knob, value in cells]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_sensitivity_worker, payloads, chunksize=1))
    return [_cell(dataset, base, pricer_factory, cs, knob, value)
            for cs, knob, value in cells]

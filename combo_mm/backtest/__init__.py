"""Chronological backtest harness over the simulated NFL RFQ dataset.

Replay order is the whole point: a generated RFQ dataset is replayed in
chronological order through the real shadow quoting engine; a counterfactual
fill model decides accept/competitor/no-trade per RFQ; settlements land at
their actual timestamps; shared metrics then compute the full report.
"""
from combo_mm.backtest.runner import BacktestConfig, RunResult, run_backtest
from combo_mm.backtest.nfl import NflPricerFactory, build_nfl_pricer
from combo_mm.backtest.fill_model import (
    FillModelConfig,
    FillModelOutcome,
    simulate_fills,
    simulate_one,
)
from combo_mm.backtest.leak_guard import (
    SidecarLeak,
    assert_no_leak,
    instrumented_run,
)
from combo_mm.backtest.metrics import (
    BacktestMetrics,
    MetricsContext,
    brier_score,
    combo_settlement_value,
    compute,
    swings,
)
from combo_mm.backtest.sensitivity import SensitivitySpec, run_sensitivity

__all__ = [
    "BacktestConfig",
    "RunResult",
    "run_backtest",
    "NflPricerFactory",
    "build_nfl_pricer",
    "FillModelConfig",
    "FillModelOutcome",
    "simulate_fills",
    "simulate_one",
    "SidecarLeak",
    "assert_no_leak",
    "instrumented_run",
    "BacktestMetrics",
    "MetricsContext",
    "brier_score",
    "combo_settlement_value",
    "compute",
    "swings",
    "SensitivitySpec",
    "run_sensitivity",
]

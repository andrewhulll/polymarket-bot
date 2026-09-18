"""Benchmark the network-free ten-leg NFL joint probability hot path.

Usage: python scripts/bench_pricer.py --samples 10000
Cold here means a new GameModel and uncached region; it excludes Gamma/CLOB
HTTP calls. Live end-to-end latency is separately recorded in quote_latency.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.nfl.joint import (GameModel, away_team_over, home_cover,
                                home_team_over, over)
from combo_mm.nfl.params_io import MatchupCovariance


def ten_legs():
    return ([home_cover(x + 0.5) for x in (-4, -2, 0, 2)]
            + [over(x + 0.5) for x in (39, 43, 47)]
            + [home_team_over(x + 0.5) for x in (17, 21)]
            + [away_team_over(17.5)])


def percentile(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * p))]


def bench(samples=10000):
    cov = MatchupCovariance(10.0, 9.0, 0.1)
    legs = ten_legs()
    warm_model = GameModel((27.0, 22.0), cov)
    warm_model.joint(legs)
    cold, warm = [], []
    for _ in range(samples):
        start = time.perf_counter()
        GameModel((27.0, 22.0), cov).joint(legs)
        cold.append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        warm_model.joint(legs)
        warm.append((time.perf_counter() - start) * 1000)
    return {name: {"p50_ms": round(statistics.median(values), 3),
                   "p95_ms": round(percentile(values, 0.95), 3),
                   "p99_ms": round(percentile(values, 0.99), 3)}
            for name, values in (("cold_model", cold), ("warm_model", warm))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=10000)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("samples must be positive")
    for name, stats in bench(args.samples).items():
        print(f"{name}: " + ", ".join(f"{k}={v:.3f}" for k, v in stats.items()))

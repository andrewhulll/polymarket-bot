"""Catch large regressions in the ten-leg, network-free model calculation."""

from scripts.bench_pricer import bench, ten_legs


def test_ten_leg_joint_latency_ci_budget():
    assert len(ten_legs()) == 10
    stats = bench(100)
    assert stats["cold_model"]["p99_ms"] < 100

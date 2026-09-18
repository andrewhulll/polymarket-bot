#!/usr/bin/env python3
"""Replay a generated NFL RFQ dataset chronologically (issue #5 item B).

Two-step reproduction (see docs/backtest.md)::

    python scripts/gen_nfl_rfq_dataset.py --seasons 2022-2025 --seed 7 \\
        --out data/rfq_sim/test_2022_2025
    python scripts/backtest.py --dataset data/rfq_sim/test_2022_2025 \\
        --out runs/ --params params/nfl_2026_w02.json

Paper/shadow only: this never submits RFQs, quotes, confirmations or orders,
and makes no network calls.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.backtest.dataset import open_dataset  # noqa: E402
from combo_mm.backtest.fill_model import FillModelConfig  # noqa: E402
from combo_mm.backtest.nfl import NflPricerFactory, build_nfl_pricer  # noqa: E402
from combo_mm.backtest.report import (  # noqa: E402
    config_hash_for, run_id_for, write_report)
from combo_mm.backtest.runner import BacktestConfig, run_backtest  # noqa: E402
from combo_mm.backtest.sensitivity import run_sensitivity  # noqa: E402
from combo_mm.config import PipelineConfig  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="dataset directory")
    ap.add_argument("--out", default="runs", help="parent for runs/<run_id>/")
    ap.add_argument("--params", default=None,
                    help="correlation params JSON (default: latest in params/)")
    ap.add_argument("--corr-scale", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=7, help="fill-model seed")
    ap.add_argument("--sensitivity", action="store_true",
                    help="also run the corr x fill-knob grid -> sensitivity.csv")
    # fill-model knobs
    ap.add_argument("--think-ms", type=int, default=1000)
    ap.add_argument("--competitor-presence", type=float, default=0.8)
    ap.add_argument("--competitor-half-spread", type=float, default=0.025)
    ap.add_argument("--retail-bias-mean", type=float, default=0.01)
    ap.add_argument("--retail-bias-sd", type=float, default=0.01)
    ap.add_argument("--sharp-sd", type=float, default=0.005)
    ap.add_argument("--sharp-share", type=float, default=None)
    ap.add_argument("--tolerance", type=float, default=0.005)
    ap.add_argument("--tie-share", type=float, default=0.5)
    ap.add_argument("--buy-share", type=float, default=0.9)
    ap.add_argument("--confirm-reject-prob", type=float, default=0.0)
    ap.add_argument("--quote-latency-ms", type=int, default=150,
                    help="decided_at + latency must beat the submission deadline")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel workers for --sensitivity (default 1)")
    args = ap.parse_args(argv)

    from combo_mm.nfl.params_io import latest_params, load_params
    params_path = Path(args.params) if args.params else latest_params("params")
    if params_path is None:
        ap.error("no params file found; pass --params")
    params = load_params(params_path)

    dataset = open_dataset(args.dataset)

    fill_cfg = FillModelConfig(
        requester_think_ms=args.think_ms,
        competitor_presence=args.competitor_presence,
        competitor_half_spread=args.competitor_half_spread,
        retail_bias_mean=args.retail_bias_mean,
        retail_bias_sd=args.retail_bias_sd,
        sharp_valuation_sd=args.sharp_sd,
        sharp_share=args.sharp_share,
        tolerance=args.tolerance,
        tie_share=args.tie_share,
        buy_share=args.buy_share,
        confirm_reject_prob=args.confirm_reject_prob,
        seed=args.seed,
    )
    cfg = BacktestConfig(
        pricer=build_nfl_pricer(dataset, params, corr_scale=args.corr_scale),
        pipeline=PipelineConfig(),
        fill=fill_cfg,
        quote_latency_ms=args.quote_latency_ms,
        params_version=getattr(params, "get", lambda *_: None)("params_version")
        if isinstance(params, dict) else None,
    )

    run_dir = Path(args.out) / run_id_for(
        dataset.dataset_id,
        config_hash=config_hash_for(
            corr_scale=args.corr_scale, seed=args.seed,
            quote_latency_ms=args.quote_latency_ms,
            fill={k: getattr(fill_cfg, k) for k in vars(fill_cfg)},
            params=str(params_path),
        ))
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg.store_path = str(run_dir / "store.sqlite")
    result = run_backtest(dataset, cfg)

    sensitivity_rows = None
    if args.sensitivity:
        base = BacktestConfig(
            pipeline=PipelineConfig(), fill=fill_cfg,
            quote_latency_ms=args.quote_latency_ms,
            params_version=cfg.params_version,
            store_path=":memory:",
        )
        sensitivity_rows = run_sensitivity(
            dataset, base,
            NflPricerFactory(dataset.root, params),
            workers=args.workers)

    manifest = write_report(
        run_dir, result.metrics, dataset=dataset, config=cfg,
        outcomes=result.outcomes, store=result.store,
        params_paths=[params_path],
        sensitivity_rows=sensitivity_rows,
        leak_violations=result.leak_violations,
        wall_seconds=result.wall_seconds,
        state_digest=result.state_digest,
    )
    m = result.metrics
    print(f"run: {run_dir}")
    print(f"rfqs: {m.rfqs_received} received / {m.rfqs_quoted} quoted / "
          f"{m.rfqs_executed} executed ({m.n_fills} fills)")
    print(f"expected P&L: {m.expected_pnl:+.4f} "
          f"(naive basis {m.expected_pnl_naive_basis:+.4f})")
    print(f"realized P&L: {m.realized_pnl:+.4f} "
          f"(downswing {m.max_downswing:.4f}, upswing {m.max_upswing:.4f})")
    if m.brier_ours is not None:
        print(f"Brier ours {m.brier_ours:.4f} vs naive {m.brier_naive:.4f} "
              f"(n={m.n_brier})")
    if result.leak_violations:
        print("LEAK VIOLATIONS:", result.leak_violations)
        return 2
    if sensitivity_rows:
        corr_rows = [r for r in sensitivity_rows if r["knob"] == "corr_scale"]
        pnls = [r["expected_pnl"] for r in corr_rows]
        if len(pnls) >= 2:
            span = max(pnls) - min(pnls)
            worst = max(abs(b - a) for a, b in zip(pnls, pnls[1:]))
            rel = (worst / span) if span else 0.0
            print(f"sensitivity: {len(sensitivity_rows)} cells; corr sweep "
                  f"max adjacent |Δexpected_pnl| = {worst:.4f} "
                  f"({rel:.1%} of sweep range)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

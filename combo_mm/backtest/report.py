"""Run artifacts: reproducible manifest, CSVs, figures (issue #5 item E).

Every run lands under ``runs/<run_id>/``::

    manifest.json        dataset hash, params versions/hashes, config, seeds,
                         git commit, state digest, leak violations, wall time
    summary.json         BacktestMetrics.to_dict()
    per_rfq.csv          per-RFQ decisions, fills, settlements, P&L
    equity.csv           settlement-time realized equity curve
    equity_mtm.csv       mark-to-model equity curve
    exposure.csv         additive exposure curve (labeled NOT correlation-aware)
    wcl_correlated.csv   correlated WCL series (inventory snapshots, if any)
    breakdown_*.csv      market_type / combo_size / fav_bucket /
                         requester_type / decline_reason breakdowns
    sensitivity.csv      (optional) corr-scale x fill-knob grid
    store.sqlite         the replayed event store
    figures/             equity + exposure PNGs where matplotlib is available

``runs/`` is git-ignored; datasets are never committed.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from combo_mm.backtest.metrics import BacktestMetrics

__all__ = ["write_report", "run_id_for", "config_hash_for"]


def run_id_for(dataset_id: str, when: Optional[datetime] = None,
               config_hash: Optional[str] = None) -> str:
    when = when or datetime.now(timezone.utc)
    stamp = f"{when:%Y%m%d-%H%M%S}"
    if config_hash:
        return f"{stamp}-{dataset_id}-{config_hash[:8]}"
    return f"{stamp}-{dataset_id}"


def config_hash_for(**parts: Any) -> str:
    """Short stable hash of the run configuration (for run_id)."""
    blob = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:8]


def _sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _git_commit(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root,
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        path.write_text("")
        return
    cols = list(rows[0].keys())
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _curve_csv(rows: List[tuple]) -> List[Dict[str, Any]]:
    return [{"ts": ts, "value": v} for ts, v in rows]


def _figures(run_dir: Path, metrics: BacktestMetrics) -> List[str]:
    """Equity/exposure PNGs; skipped quietly when matplotlib is missing."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    figs = run_dir / "figures"
    figs.mkdir(exist_ok=True)
    made = []

    def plot(points, title, ylabel, name):
        if not points:
            return
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        fig, ax = plt.subplots(figsize=(8, 3.5))
        ax.plot(range(len(xs)), ys, lw=1.2)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xlabel(f"n={len(xs)} steps")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(figs / name, dpi=90)
        plt.close(fig)
        made.append(f"figures/{name}")

    plot(metrics.realized_curve, "Realized equity (settlement-time)",
         "cumulative P&L", "equity.png")
    plot(metrics.mtm_curve, "Mark-to-model equity", "cumulative P&L", "equity_mtm.png")
    plot([(r["ts"], r["additive_max_loss"]) for r in metrics.exposure_curve],
         "Additive max-loss exposure (NOT correlation-aware)",
         "exposure", "exposure.png")
    return made


def write_report(run_dir: Any,
                 metrics: BacktestMetrics,
                 *,
                 dataset: Any = None,
                 config: Any = None,
                 outcomes: Optional[List[Any]] = None,
                 store: Any = None,
                 params_paths: Optional[List[Any]] = None,
                 sensitivity_rows: Optional[List[Dict[str, Any]]] = None,
                 leak_violations: Optional[List[str]] = None,
                 wall_seconds: float = 0.0,
                 state_digest: str = "",
                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Write every run artifact; return the manifest dict."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics_dict = metrics.to_dict()

    (run_dir / "summary.json").write_text(
        json.dumps(metrics_dict, indent=2, default=str))
    _write_csv(run_dir / "per_rfq.csv", metrics.per_rfq)
    _write_csv(run_dir / "equity.csv", _curve_csv(metrics.realized_curve))
    _write_csv(run_dir / "equity_mtm.csv", _curve_csv(metrics.mtm_curve))
    _write_csv(run_dir / "exposure.csv", metrics.exposure_curve)
    _write_csv(run_dir / "wcl_correlated.csv",
               _curve_csv(metrics.correlated_wcl_series))
    for name, rows in (("market_type", metrics.by_market_type),
                       ("combo_size", metrics.by_combo_size),
                       ("fav_bucket", metrics.by_fav_bucket),
                       ("requester_type", metrics.by_requester_type),
                       ("decline_reason", metrics.by_decline_reason)):
        _write_csv(run_dir / f"breakdown_{name}.csv", rows)
    if sensitivity_rows is not None:
        _write_csv(run_dir / "sensitivity.csv", sensitivity_rows)
    figures = _figures(run_dir, metrics)

    if store is not None:
        src = getattr(store, "_path", None)  # noqa: SLF001
        dst = run_dir / "store.sqlite"
        if (src and src != ":memory:" and os.path.exists(src)
                and os.path.abspath(src) != os.path.abspath(dst)):
            shutil.copy2(src, dst)

    repo_root = Path(__file__).resolve().parents[2]
    params = {}
    for p in params_paths or []:
        p = Path(p)
        params[str(p)] = {"sha256": _sha256(p),
                          "exists": p.exists()}
    fill_cfg = getattr(config, "fill", None)
    manifest = {
        "run_id": run_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": getattr(dataset, "dataset_id", None),
        "dataset_manifest_hash": (dataset.manifest_hash()
                                  if dataset is not None else None),
        "params": params,
        "fill_model": ({k: getattr(fill_cfg, k)
                        for k in vars(fill_cfg)} if fill_cfg else None),
        "quote_latency_ms": getattr(config, "quote_latency_ms", None),
        "git_commit": _git_commit(repo_root),
        "state_digest": state_digest,
        "leak_violations": leak_violations or [],
        "wall_seconds": wall_seconds,
        "seeds": {"fill_model": getattr(fill_cfg, "seed", None)
                  if fill_cfg else None},
        "counts": {
            "rfqs_received": metrics.rfqs_received,
            "rfqs_quoted": metrics.rfqs_quoted,
            "rfqs_executed": metrics.rfqs_executed,
            "n_fills": metrics.n_fills,
        },
        "key_numbers": {
            "expected_pnl": metrics.expected_pnl,
            "expected_pnl_naive_basis": metrics.expected_pnl_naive_basis,
            "realized_pnl": metrics.realized_pnl,
            "max_downswing": metrics.max_downswing,
            "max_upswing": metrics.max_upswing,
            "brier_ours": metrics.brier_ours,
            "brier_naive": metrics.brier_naive,
        },
        "figures": figures,
        "extra": extra or {},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2,
                                                     default=str))
    return manifest

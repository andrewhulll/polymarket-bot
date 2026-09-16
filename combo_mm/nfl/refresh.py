"""Weekly params refresh: estimate -> stage -> validation gates -> promote.

The refresh never overwrites a promoted file with one that fails a gate:

1. **Range sanity** -- :func:`combo_mm.nfl.params_io.validate_params`: schema,
   finite numbers, sigma within bounds, ``|rho| < 1``, positive variances.
2. **No regression** -- Brier score of the candidate on the most recent
   ``gate_lookback_games`` played games (spread x total combos, market-
   calibrated) must not exceed the previous promoted file's by more than
   ``tolerance``; on the first run it must not exceed the naive product's.
   In-sample for the candidate: a guard against a broken refresh, not an
   out-of-sample evaluation (``scripts/nfl_backtest.py`` is that).
3. **Determinism** -- re-estimating from the same games and config must
   reproduce the staged file byte for byte.

Offseason (no unplayed games on the schedule): nothing is written; the last
promoted file stays current ("freeze").
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from combo_mm.nfl.estimate import EstimatorConfig, ResidualTable, estimate_params
from combo_mm.nfl.ingest import Game
from combo_mm.nfl.params_io import (
    ParamsError,
    canonical_json_bytes,
    latest_params,
    load_params,
    params_filename,
    validate_params,
)

__all__ = ["GateResult", "RefreshResult", "next_target_week", "build_params", "refresh"]

PROMOTED = "promoted"
REJECTED = "rejected"
OFFSEASON = "offseason_frozen"


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RefreshResult:
    status: str
    season: Optional[int] = None
    week: Optional[int] = None
    path: Optional[str] = None
    staging_path: Optional[str] = None
    report_path: Optional[str] = None
    gates: List[GateResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["gates"] = [asdict(g) for g in self.gates]
        return d


def next_target_week(games: Sequence[Game]) -> Optional[Tuple[int, int]]:
    """Earliest (season, week) that still has an unplayed game; None in the offseason."""
    pending = [g.order_key for g in games if not g.played]
    return min(pending) if pending else None


def build_params(games: Sequence[Game], season: int, week: int, config: EstimatorConfig,
                 data_vintage: Dict[str, Any]) -> Dict[str, Any]:
    schedule = [g for g in games if g.season == season and g.week == week]
    return estimate_params(ResidualTable.build(games), season, week, config,
                           data_vintage=data_vintage, schedule=schedule)


def _lookback_games(games: Sequence[Game], season: int, week: int, n: int) -> List[Game]:
    before = [g for g in games if g.played and g.has_lines and g.order_key < (season, week)]
    before.sort(key=lambda g: (g.season, g.week, g.game_id))
    return before[-n:]


def refresh(games: Sequence[Game], params_dir: Path | str, data_vintage: Dict[str, Any],
            season: Optional[int] = None, week: Optional[int] = None,
            config: Optional[EstimatorConfig] = None,
            gate_lookback_games: int = 272, tolerance: float = 1e-3) -> RefreshResult:
    from combo_mm.nfl.synthetic_backtest import evaluate_params_on_games  # numpy-heavy, lazy

    config = config or EstimatorConfig()
    params_dir = Path(params_dir)
    if season is None or week is None:
        target = next_target_week(games)
        if target is None:
            latest = latest_params(params_dir)
            return RefreshResult(status=OFFSEASON, path=str(latest) if latest else None)
        season, week = target

    name = params_filename(season, week)
    staging = params_dir / ".staging" / name
    result = RefreshResult(status=REJECTED, season=season, week=week, staging_path=str(staging))

    candidate = build_params(games, season, week, config, data_vintage)
    staged_bytes = canonical_json_bytes(candidate)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(staged_bytes)

    # Gate 1: range sanity.
    try:
        validate_params(json.loads(staged_bytes))
        result.gates.append(GateResult("range_sanity", True))
    except ParamsError as exc:
        result.gates.append(GateResult("range_sanity", False, {"error": str(exc)}))

    # Gate 2: no regression.
    lookback = _lookback_games(games, season, week, gate_lookback_games)
    cand_eval = evaluate_params_on_games(candidate, lookback)
    previous_path = latest_params(params_dir, before=(season, week))
    detail: Dict[str, Any] = {"n_combos": cand_eval["n"], "candidate_brier": cand_eval["brier_model"],
                              "naive_brier": cand_eval["brier_naive"], "tolerance": tolerance}
    if previous_path is not None:
        prev_eval = evaluate_params_on_games(load_params(previous_path), lookback)
        detail.update(previous_file=previous_path.name, previous_brier=prev_eval["brier_model"])
        passed = cand_eval["brier_model"] <= prev_eval["brier_model"] + tolerance
    else:
        detail["previous_file"] = None
        passed = cand_eval["brier_model"] <= cand_eval["brier_naive"] + tolerance
    if cand_eval["n"] == 0:
        passed = False
        detail["error"] = "no lookback games to evaluate"
    result.gates.append(GateResult("no_regression", bool(passed), detail))

    # Gate 3: determinism.
    rebuilt = canonical_json_bytes(build_params(games, season, week, config, data_vintage))
    result.gates.append(GateResult("determinism", rebuilt == staged_bytes,
                                   {"bytes": len(staged_bytes)}))

    if all(g.passed for g in result.gates):
        final = params_dir / name
        os.replace(staging, final)
        result.status = PROMOTED
        result.path = str(final)
        result.staging_path = None

    report_dir = params_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / name.replace(".json", ".gates.json")
    payload = result.to_dict()
    payload["data_vintage"] = data_vintage
    payload["estimator"] = config.to_dict()
    report.write_text(json.dumps(_portable(payload, params_dir), indent=2, sort_keys=True) + "\n")
    result.report_path = str(report)
    return result


def _portable(payload: Dict[str, Any], params_dir: Path) -> Dict[str, Any]:
    """Report paths relative to the params dir so committed reports are machine-independent."""
    out = dict(payload)
    for key in ("path", "staging_path", "report_path"):
        if out.get(key):
            try:
                out[key] = str(Path(out[key]).relative_to(params_dir)).replace("\\", "/")
            except ValueError:
                pass
    return out

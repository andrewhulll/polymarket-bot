#!/usr/bin/env python3
"""Summarize the dashboard's reconstructed NFL paper quote replay."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_mm.nfl.week_backtest import run_from_pull


def stats(rows):
    vals = sorted(rows)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "median": vals[len(vals) // 2],
        "p90": vals[int(0.9 * (len(vals) - 1))],
        "max": vals[-1],
    }


def main():
    root = Path(__file__).resolve().parents[1]
    out = run_from_pull(raw_root=root / "data" / "raw",
                        estimator_path=root / "params" / "estimator.json",
                        db_path=":memory:")
    rows = out.trades
    by_family = defaultdict(list)
    by_combo = defaultdict(list)
    by_side = defaultdict(list)
    for row in rows:
        by_family[row["family"]].append(row)
        by_combo[row["combo"]].append(row)
        by_side[row["requester_side"]].append(row)

    def summarize(group):
        quoted = [r for r in group if r["model_fair"] is not None]
        gaps = [abs(r["model_fair"] - r["naive_fair"]) for r in quoted]
        side_gaps = [abs((r["our_buy"] - r["comp_buy"]) if r["requester_side"] == "BUY"
                         else (r["our_sell"] - r["comp_sell"]))
                     for r in quoted if r["comp_buy"] is not None]
        return {"rfqs": len(group), "quoted": len(quoted),
                "won": sum(r["won"] for r in group),
                "fair_gap": stats(gaps), "gap_gt_1c": sum(g > 0.01 for g in gaps),
                "gap_lt_1bp": sum(g < 0.0001 for g in gaps),
                "side_quote_gap": stats(side_gaps),
                "same_side_quote": sum(g < 1e-9 for g in side_gaps),
                "same_both_quotes": sum(r["our_buy"] == r["comp_buy"] and
                                        r["our_sell"] == r["comp_sell"] for r in quoted),
                "pnl": sum(r["pnl"] or 0 for r in group)}

    result = {
        "meta": out.meta,
        "overall": summarize(rows),
        "by_family": {k: summarize(v) for k, v in by_family.items()},
        "by_combo": {k: summarize(v) for k, v in by_combo.items()},
        "by_side": {k: summarize(v) for k, v in by_side.items()},
        "nested": {str(k): summarize([r for r in rows if r["nested"] == k])
                   for k in (True, False)},
        "sample_largest_gaps": [
            {k: r[k] for k in ("rfq_id", "combo_label", "requester_side", "qty",
                                "naive_fair", "model_fair", "our_buy", "comp_buy",
                                "our_sell", "comp_sell", "won")}
            for r in sorted((r for r in rows if r["model_fair"] is not None),
                            key=lambda r: abs(r["model_fair"] - r["naive_fair"]),
                            reverse=True)[:15]
        ],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

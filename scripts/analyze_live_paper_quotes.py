#!/usr/bin/env python3
"""Read-only summary of durable live paper quotes and an optional feed DB."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("data/live/quote_selections.db"))
    ap.add_argument("--feed-db", type=Path)
    args = ap.parse_args()
    db = connect(args.db)
    quotes = [dict(row) for row in db.execute("SELECT * FROM priced_quotes")]
    selected = db.execute("SELECT COUNT(*) FROM selected_rfqs").fetchone()[0]
    accepted = db.execute("SELECT COUNT(*) FROM accepted_quotes").fetchone()[0]
    quoted = [q for q in quotes if q["status"] == "QUOTED"]
    details = [json.loads(q["detail_json"]) for q in quoted]
    def shape(q):
        detail = json.loads(q["detail_json"])
        kinds = []
        for leg in detail.get("legs") or []:
            name = leg.get("canonical") or "other"
            kinds.append("ML" if name.endswith("_ml") else
                         "spread" if "cover" in name else
                         "total" if name.startswith(("over(", "under(")) else
                         "team total" if "team_" in name else "other")
        return " + ".join(sorted(kinds)) if kinds else "unknown"
    def label_shape(q):
        kinds = []
        for label in (q["legs_label"] or "").split(" | "):
            if label.startswith("Spread:"):
                kinds.append("spread")
            elif " vs. " in label and "O/U" in label:
                kinds.append("total")
            elif " vs. " in label:
                kinds.append("ML")
            else:
                kinds.append("other")
        return " + ".join(sorted(kinds)) if kinds else "unknown"
    gaps = sorted(abs(q["fair"] - q["naive"]) for q in quoted
                  if q["fair"] is not None and q["naive"] is not None)
    shape_counts = Counter(shape(q) for q in quoted)
    def stat(vals):
        if not vals:
            return None
        return {"n": len(vals), "median": vals[len(vals) // 2],
                "p90": vals[int(0.9 * (len(vals) - 1))], "max": vals[-1]}
    out = {
        "db": str(args.db),
        "selected": selected, "accepted": accepted,
        "priced": len(quotes), "distinct_rfqs": len({q["rfq_id"] for q in quotes}),
        "time_range": [min((q["priced_at"] for q in quotes), default=None),
                       max((q["priced_at"] for q in quotes), default=None)],
        "reasons": dict(Counter(q["reason_code"] for q in quotes)),
        "triggers": dict(Counter(q["trigger"] for q in quotes)),
        "reason_by_shape": {
            reason: dict(Counter(shape(q) for q in quotes if q["reason_code"] == reason))
            for reason in sorted({q["reason_code"] for q in quotes})
        },
        "reason_by_label_shape": {
            reason: dict(Counter(label_shape(q) for q in quotes if q["reason_code"] == reason))
            for reason in sorted({q["reason_code"] for q in quotes})
        },
        "reason_detail_samples": {
            reason: list(dict.fromkeys(q["reason_detail"] for q in quotes
                                      if q["reason_code"] == reason))[:5]
            for reason in sorted({q["reason_code"] for q in quotes if q["status"] != "QUOTED"})
        },
        "declined_legs_samples": {
            reason: list(dict.fromkeys(q["legs_label"] for q in quotes
                                      if q["reason_code"] == reason))[:3]
            for reason in sorted({q["reason_code"] for q in quotes if q["status"] != "QUOTED"})
        },
        "quoted": len(quoted),
        "quoted_after_deadline": sum(bool(q["after_deadline"]) for q in quoted),
        "all_after_deadline": sum(bool(q["after_deadline"]) for q in quotes),
        "quoted_shapes": dict(shape_counts),
        "quoted_sides": dict(Counter(f"{q['side']} {q['direction']}" for q in quoted)),
        "gap": stat(gaps),
        "gap_lt_1bp": sum(x < 0.0001 for x in gaps),
        "gap_lt_1c": sum(x < 0.01 for x in gaps),
        "quote_game_corr_mt": dict(Counter(str(game.get("corr_margin_total"))
                                        for detail in details for game in detail.get("games") or [])),
        "spread_bps": stat(sorted(q["spread_bps_total"] for q in quoted
                                  if q["spread_bps_total"] is not None)),
        "component_bps": {
            key: stat(sorted(float(detail["components"][key]) for detail in details
                             if isinstance(detail.get("components", {}).get(key), (int, float))))
            for key in sorted({key for detail in details
                               for key in detail.get("components", {})
                               if key.endswith("_bps")})
        },
        "sample_legs": details[0].get("legs") if details else None,
        "sample_games": details[0].get("games") if details else None,
    }
    if args.feed_db:
        feed = connect(args.feed_db)
        out["feed_db"] = str(args.feed_db)
        out["feed_rfqs"] = feed.execute("SELECT COUNT(*) FROM rfq").fetchone()[0]
        out["screens"] = dict(feed.execute(
            "SELECT screen, COUNT(*) FROM rfq_screen GROUP BY screen").fetchall())
        out["feed_times"] = list(feed.execute(
            "SELECT MIN(created_time), MAX(created_time) FROM rfq").fetchone())
        ids = [q["rfq_id"] for q in quotes]
        deadlines = {}
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            sql = ("SELECT r.rfq_id, s.submission_deadline, r.created_time "
                   "FROM rfq r JOIN rfq_screen s USING (rfq_id) WHERE r.rfq_id IN ("
                   + ",".join("?" for _ in part) + ")")
            deadlines.update({r["rfq_id"]: (r["submission_deadline"], r["created_time"])
                              for r in feed.execute(sql, part)})
        lateness = sorted((datetime.fromisoformat(q["priced_at"].replace("Z", "+00:00")).timestamp()
                           - float(deadlines[q["rfq_id"]][0]) / 1000)
                          for q in quotes if q["rfq_id"] in deadlines
                          and deadlines[q["rfq_id"]][0] is not None)
        out["deadline_lateness_s"] = stat(lateness)
        window = sorted((float(deadline) / 1000
                         - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
                        for deadline, created in deadlines.values() if deadline is not None and created)
        out["deadline_window_s"] = stat(window)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

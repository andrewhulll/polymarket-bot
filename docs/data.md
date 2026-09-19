# Versioned data

The repository tracks historical NFL games under `data/raw/`, a Week 1 paper
backtest database under `data/live/`, and a frozen live capture under
`data/snapshots/live-2026-09-18/`. The frozen capture contains a consistent
SQLite backup, its quoted-RFQ JSONL log, and the combo-market catalog. It
contains only RFQs that received a paper quote; rejected screens exist only in
the capture process's memory and are absent from these files.

To inspect the frozen capture in the dashboard from the repository root:

```bash
python -m dashboard.server --data-dir data/snapshots/live-2026-09-18 --port 8000
```

The active capture files in `data/live/` change continuously, so their database,
JSONL, catalog cache, logs, locks, and SQLite sidecars are individually ignored.
The `data/` directory itself is versioned.

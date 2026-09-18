"""Backtest dataset access (issue #5 item B).

A dataset directory written by :func:`combo_mm.nfl.rfq_sim.write_dataset`
contains::

    session.jsonl.gz   chronological replay items (books, rfq_created, closes)
    sidecar.jsonl.gz   future info per RFQ -- the fill model may read this,
                       nothing else may
    combos.json        combo definitions (ReferenceCache transport)
    markets.json       registry snapshot
    manifest.json      params + generator config + dataset hash

This module exposes :class:`Dataset`, which streams replay items with
absolute exchange times, applies the pre-T cut (no book/sidecar leakage past
the quote decision time), and feeds the runner a static combo transport.

The runner never opens the sidecar; the fill model is the only reader
(via :mod:`combo_mm.backtest.fill_model`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from combo_mm.nfl.rfq_sim import load_manifest, load_session

__all__ = ["Dataset", "open_dataset"]


@dataclass
class Dataset:
    """A generated NFL RFQ dataset ready for chronological replay."""

    root: Path
    manifest: Dict[str, Any]
    base: datetime                    # naive UTC; item["t"] offsets from here
    combos: List[Dict[str, Any]]
    markets: Dict[str, Any]

    @property
    def dataset_id(self) -> str:
        return self.manifest.get("dataset_id", self.root.name)

    def base_ts_ms(self, t_ms: int) -> int:
        dt = self.base.replace(tzinfo=timezone.utc) + timedelta(milliseconds=t_ms)
        return int(dt.timestamp() * 1000)

    def session(self) -> Iterator[Dict[str, Any]]:
        """Chronological replay items with ``ts`` (absolute ISO) injected."""
        items, _, _, _ = load_session(self.root)
        for item in items:
            t = int(item.get("t", 0))
            ts = (self.base.replace(tzinfo=timezone.utc)
                  + timedelta(milliseconds=t)).isoformat().replace("+00:00", "Z")
            yield {**item, "ts": ts}

    def combos_for_symbol(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        if symbol is None:
            return list(self.combos)
        return [c for c in self.combos if c.get("symbol") == symbol]

    def manifest_hash(self) -> str:
        """Stable hash of the dataset: sha256 over the manifest file bytes.

        The manifest pins every input file's sha256 (``files``) plus the
        generator version/seed/config, so this identifies the dataset
        byte-for-byte.
        """
        import hashlib

        try:
            return hashlib.sha256(
                (self.root / "manifest.json").read_bytes()).hexdigest()
        except OSError:
            return self.manifest.get("dataset_hash", "")


def open_dataset(root: Any) -> Dataset:
    """Open a dataset directory previously written by ``write_dataset``."""
    root = Path(root)
    manifest = load_manifest(root)
    combos_path = root / "combos.json"
    combos = json.loads(combos_path.read_text()) if combos_path.exists() else []
    markets_path = root / "markets.json"
    markets = json.loads(markets_path.read_text()) if markets_path.exists() else {}
    base = datetime.fromisoformat(
        str(manifest.get("base_ts", "")).replace("Z", "+00:00"))
    return Dataset(root=root, manifest=manifest, base=base,
                   combos=combos, markets=markets)

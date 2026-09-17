"""Current weekly NFL params file for live pricing (stdlib only).

Live pricing reads the newest ``params/nfl_<season>_w<ww>.json`` written by
``scripts/refresh_params.py``. Each file is estimated from games strictly
before its week, so using the newest file is leak-free. The version string
names the exact bytes priced with (``filename@sha256[:12]``) and the age is
measured from the file's nflverse pull date, so a refresh that stopped
running shows up as ``PARAMS_STALE`` rather than silently pricing on old
covariance.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from combo_mm.nfl.params_io import latest_params, load_params

__all__ = ["ParamsHandle", "ParamsProvider"]


@dataclass(frozen=True)
class ParamsHandle:
    params: Dict[str, Any]
    path: Path
    version: str                 # "nfl_2026_w02.json@<sha12>"
    season: int
    week: int
    pull_date: Optional[str]     # data_vintage.pull_date (YYYY-MM-DD)

    def age_days(self, now: datetime) -> float:
        if not self.pull_date:
            return float("inf")
        try:
            pulled = datetime.fromisoformat(self.pull_date).replace(tzinfo=timezone.utc)
        except ValueError:
            return float("inf")
        return max(0.0, (now - pulled).total_seconds() / 86400.0)

    def game(self, home: str, away: str) -> Optional[Dict[str, Any]]:
        """The file's slate row for this matchup, if it has one (diagnostics only)."""
        for g in self.params.get("games", []):
            if g.get("home") == home and g.get("away") == away:
                return g
        return None


class ParamsProvider:
    """Loads (and caches by path + mtime) the newest params file in ``params_dir``."""

    def __init__(self, params_dir: str | Path = "params") -> None:
        self.params_dir = Path(params_dir)
        self._cache: Optional[Tuple[Tuple[str, float], ParamsHandle]] = None
        self._lock = threading.Lock()

    def current(self) -> Optional[ParamsHandle]:
        path = latest_params(self.params_dir)
        if path is None:
            return None
        key = (str(path), path.stat().st_mtime)
        with self._lock:
            if self._cache is not None and self._cache[0] == key:
                return self._cache[1]
            raw = path.read_bytes()
            params = load_params(path)
            handle = ParamsHandle(
                params=params, path=path,
                version=f"{path.name}@{hashlib.sha256(raw).hexdigest()[:12]}",
                season=int(params["season"]), week=int(params["week"]),
                pull_date=(params.get("data_vintage") or {}).get("pull_date"))
            self._cache = (key, handle)
            return handle

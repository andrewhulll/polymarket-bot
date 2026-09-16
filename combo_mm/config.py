"""Pipeline configuration.

``PipelineConfig`` is the single place for tunables. The RFQ stream request
is EMPTY (no filters -- see :mod:`combo_mm.stream`), so there is no
read-scope/filter configuration here. Unknown fields are fatal
(dataclass ``TypeError`` on construction, plus :meth:`from_dict` for dicts).
Call :meth:`validate` at startup; the consumer does this automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class PipelineConfig:
    paper_mode: bool = True
    staleness_ms: int = 2000
    watchlist: List[str] = field(default_factory=list)
    db_path: str = ":memory:"
    backoff_initial_ms: int = 100
    backoff_max_ms: int = 5000
    backoff_jitter: float = 0.2
    watchdog_silence_s: float = 30.0
    log_level: str = "INFO"
    reference_ttl_s: float = 300.0
    max_reconnects: int | None = None  # None = retry forever
    # --- Retail polling knobs (live polling path only) ---
    poll_interval_s: float = 5.0       # seconds between Retail REST polls
    max_requests_per_poll: int = 10    # request budget per poll (reads only)
    # --- V1 pricer knobs (spread components, basis points unless noted) ---
    base_edge_bps: float = 15.0
    uncertainty_per_leg_bps: float = 5.0
    width_weight: float = 0.5          # multiplier on summed leg spread (bps)
    depth_slope_bps: float = 20.0     # depth impact slope vs top-of-book size
    event_risk_bps: float = 5.0
    operational_buffer_bps: float = 5.0
    tick_size: float = 0.001
    price_min: float = 0.001
    price_max: float = 0.999
    min_qty: float = 1.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> "PipelineConfig":
        """Validate every field; raise ValueError/TypeError on bad config."""
        if not isinstance(self.paper_mode, bool):
            raise TypeError("paper_mode must be bool")
        for name in ("staleness_ms", "backoff_initial_ms", "backoff_max_ms"):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int")
        if self.backoff_max_ms < self.backoff_initial_ms:
            raise ValueError("backoff_max_ms must be >= backoff_initial_ms")
        if not isinstance(self.backoff_jitter, (int, float)) or not (
            0 <= self.backoff_jitter <= 1
        ):
            raise ValueError("backoff_jitter must be in [0, 1]")
        if not isinstance(self.watchdog_silence_s, (int, float)) or self.watchdog_silence_s <= 0:
            raise ValueError("watchdog_silence_s must be positive")
        if not isinstance(self.reference_ttl_s, (int, float)) or self.reference_ttl_s <= 0:
            raise ValueError("reference_ttl_s must be positive")
        if not isinstance(self.poll_interval_s, (int, float)) or self.poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        if not isinstance(self.max_requests_per_poll, int) or self.max_requests_per_poll < 1:
            raise ValueError("max_requests_per_poll must be an int >= 1")
        for name in ("base_edge_bps", "uncertainty_per_leg_bps", "width_weight",
                     "depth_slope_bps", "event_risk_bps",
                     "operational_buffer_bps"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a non-negative number")
        for name in ("tick_size", "price_min", "price_max", "min_qty"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if self.price_max <= self.price_min:
            raise ValueError("price_max must be > price_min")
        if not isinstance(self.watchlist, (list, tuple)) or not all(
            isinstance(s, str) for s in self.watchlist
        ):
            raise TypeError("watchlist must be a list of symbol strings")
        if not isinstance(self.db_path, str) or not self.db_path:
            raise TypeError("db_path must be a non-empty string")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"unknown log_level: {self.log_level!r}")
        if self.max_reconnects is not None and (
            not isinstance(self.max_reconnects, int) or self.max_reconnects < 0
        ):
            raise ValueError("max_reconnects must be a non-negative int or None")
        return self

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineConfig":
        """Build from a dict; unknown keys are fatal."""
        import dataclasses

        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise TypeError(f"unknown config fields: {sorted(unknown)}")
        return cls(**data)

    def startup_banner(self) -> str:
        lines = [
            "=" * 60,
            "combo_mm pipeline starting",
            f"PAPER MODE — no live orders{' ' * 1}(paper_mode={self.paper_mode})",
            f"db_path={self.db_path}  staleness_ms={self.staleness_ms}",
            f"watchdog_silence_s={self.watchdog_silence_s}  "
            f"backoff={self.backoff_initial_ms}->{self.backoff_max_ms}ms",
            f"watchlist={list(self.watchlist) or '(empty)'}",
            "=" * 60,
        ]
        if not self.paper_mode:
            lines[2] = "LIVE MODE — outbound RPCs enabled (paper_mode=False)"
        return "\n".join(lines)

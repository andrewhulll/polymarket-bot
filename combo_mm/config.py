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

from combo_mm.risk_config import RiskConfig


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
    # Wall-clock budget from an RFQ posting to us deciding whether to quote
    # it (live monitor only -- see combo_mm.live_monitor). Missing this
    # budget means we likely lose the RFQ to a faster maker.
    quote_latency_budget_ms: int = 400
    # --- Markup knobs: the combo spread is copied from the legs' own books.
    # half_spread = min(max_half_spread_bps,
    #                   leg_width_multiplier * avg leg half-spread).
    leg_width_multiplier: float = 2.0  # the one knob
    max_half_spread_bps: float = 50.0   # total spread never exceeds 100 bps
    max_leg_spread_bps: float = 1000.0  # decline when a leg book is wider
    tick_size: float = 0.001
    price_min: float = 0.001
    price_max: float = 0.999
    min_qty: float = 1.0
    # --- Shadow quoting engine knobs ---
    max_per_rfq_notional: float = 1000.0   # per-RFQ draft notional cap
    max_per_game_notional: float = 5000.0   # per-game exposure cap
    initial_capital: float = 50000.0        # total exposure bound
    stale_rfq_ms: int = 60000              # RFQ staleness cutoff (exchange time)
    params_version: str = "unversioned"    # pricing params version tag
    risk: RiskConfig = field(default_factory=RiskConfig)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> "PipelineConfig":
        """Validate every field; raise ValueError/TypeError on bad config."""
        if not isinstance(self.paper_mode, bool):
            raise TypeError("paper_mode must be bool")
        if not isinstance(self.risk, RiskConfig):
            raise TypeError("risk must be RiskConfig")
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
        if (not isinstance(self.quote_latency_budget_ms, int)
                or self.quote_latency_budget_ms <= 0):
            raise ValueError("quote_latency_budget_ms must be a positive int")
        for name in ("leg_width_multiplier", "max_half_spread_bps",
                     "max_leg_spread_bps"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a non-negative number")
        for name in ("tick_size", "price_min", "price_max", "min_qty"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if self.price_max <= self.price_min:
            raise ValueError("price_max must be > price_min")
        for name in ("max_per_rfq_notional", "max_per_game_notional",
                     "initial_capital"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if (not isinstance(self.stale_rfq_ms, int)
                or self.stale_rfq_ms <= 0):
            raise ValueError("stale_rfq_ms must be a positive int")
        if not isinstance(self.params_version, str) or not self.params_version:
            raise ValueError("params_version must be a non-empty string")
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
        data = dict(data)
        if isinstance(data.get("risk"), dict):
            data["risk"] = RiskConfig(**data["risk"])
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
            lines[2] = ("LIVE MODE requested (paper_mode=False) -- unsupported: "
                        "no live transport is implemented; the quoting engine "
                        "will refuse to run.")
        return "\n".join(lines)

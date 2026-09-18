"""Validated knobs for paper inventory risk."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    policy: str = "conservative"
    max_rfq_loss: float = 1000.0
    max_market_loss: float = 4000.0
    max_game_loss: float = 2500.0
    max_team_loss: float = 3000.0
    max_portfolio_loss: float = 25000.0
    min_buying_power: float = 5000.0
    soft_utilization: float = 0.5
    max_widen_bps: float = 300.0
    max_skew_bps: float = 200.0
    min_edge_bps: float = 10.0
    min_qty: int = 1
    tick_size: float = 0.001
    risk_halt: bool = False

    def __post_init__(self):
        if self.policy not in ("conservative", "inventory"):
            raise ValueError("risk.policy must be conservative or inventory")
        for name in ("max_rfq_loss", "max_market_loss", "max_game_loss",
                     "max_team_loss", "max_portfolio_loss", "min_buying_power",
                     "tick_size", "min_qty"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 < self.soft_utilization < 1:
            raise ValueError("soft_utilization must be in (0, 1)")
        for name in ("max_widen_bps", "max_skew_bps", "min_edge_bps"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")

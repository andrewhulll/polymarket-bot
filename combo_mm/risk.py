"""Pure risk decisions for paper quotes.

The shadow quoting engine (:mod:`combo_mm.engine`) risk-checks exclusively
through the :class:`RiskCheck` protocol below. The full risk module
(issue #3 -- inventory tracking, skew/widen, kill switch) replaces
:class:`ConservativeRiskCheck` behind this same interface with zero
engine changes.

The conservative check enforces three hard caps and never touches
inventory: it is a pure function of the draft, the quoted notional, and
the caller-supplied :class:`InventoryState`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, Protocol, Optional, Tuple

from combo_mm.pricer import PricerResult

__all__ = [
    "InventoryState",
    "RiskVerdict",
    "RiskCheck",
    "ConservativeRiskCheck",
    "RISK_OK",
    "RISK_SIZE_REDUCED",
    "RISK_GAME_EXPOSURE",
    "RISK_CAPITAL",
    "RISK_WIDEN", "RISK_SKEW", "RISK_LIMIT_MARKET",
    "RISK_LIMIT_TEAM", "RISK_LIMIT_PORTFOLIO", "RISK_KILL_SWITCH",
]

RISK_OK = "RISK_OK"
RISK_SIZE_REDUCED = "RISK_SIZE_REDUCED"
RISK_GAME_EXPOSURE = "RISK_GAME_EXPOSURE"
RISK_CAPITAL = "RISK_CAPITAL"
RISK_WIDEN = "RISK_WIDEN"
RISK_SKEW = "RISK_SKEW"
RISK_LIMIT_MARKET = "RISK_LIMIT_MARKET"
RISK_LIMIT_TEAM = "RISK_LIMIT_TEAM"
RISK_LIMIT_PORTFOLIO = "RISK_LIMIT_PORTFOLIO"
RISK_KILL_SWITCH = "RISK_KILL_SWITCH"


@dataclass(frozen=True)
class InventoryState:
    """Point-in-time inventory for the risk check (read-only here).

    ``exposures`` maps NFL game ids (or a fallback combo symbol) to
    conservative worst-case loss. Pending and executed are separate so
    callers can display and audit each source of capital use.
    """

    exposures: Dict[str, float] = field(default_factory=dict)
    notional_by_game: Dict[str, float] = field(default_factory=dict)
    capital: float = 50000.0
    pending: Dict[str, float] = field(default_factory=dict)
    executed: Dict[str, float] = field(default_factory=dict)
    markets: Dict[str, float] = field(default_factory=dict)
    teams: Dict[str, float] = field(default_factory=dict)
    net_by_game: Dict[str, float] = field(default_factory=dict)
    equity: float = 50000.0
    buying_power: float = 50000.0
    realized_pnl: float = 0.0
    kill_switch: bool = False
    as_of: str = ""

    def to_snapshot(self) -> dict:
        """JSON-serializable snapshot for the draft's input snapshot."""
        return {"exposures": dict(self.exposures),
                "notional_by_game": dict(self.notional_by_game), "capital": self.capital,
                "pending": dict(self.pending), "executed": dict(self.executed),
                "markets": dict(self.markets), "teams": dict(self.teams),
                "net_by_game": dict(self.net_by_game),
                "equity": self.equity, "buying_power": self.buying_power,
                "realized_pnl": self.realized_pnl,
                "kill_switch": self.kill_switch, "as_of": self.as_of}


@dataclass(frozen=True)
class RiskVerdict:
    """Outcome of a risk check: pass, pass-with-reduced-size, or reject."""

    ok: bool
    adjusted_buy_qty: str
    adjusted_sell_qty: str
    reason: str                        # RISK_OK / RISK_SIZE_REDUCED / ...
    detail: dict = field(default_factory=dict)
    action: str = "quote"
    adjusted_buy_price: Optional[float] = None
    adjusted_sell_price: Optional[float] = None
    widen_bps: float = 0.0
    skew_bps: float = 0.0
    flags: Tuple[str, ...] = ()
    exposure_before: dict = field(default_factory=dict)
    exposure_after: dict = field(default_factory=dict)


class RiskCheck(Protocol):
    """Interface every risk module implements."""

    def check(self, draft: PricerResult, notional: float,
              inventory: InventoryState, game_key: str) -> RiskVerdict:
        """Check a priced draft. Pure function -- no I/O."""
        ...


def _scale_qty(qty: str, factor: float) -> str:
    """Scale a Decimal quantity string down by ``factor`` (floor)."""
    try:
        amount = Decimal(qty)
    except (InvalidOperation, ValueError, TypeError):
        return qty
    if amount <= 0 or factor >= 1.0:
        return qty
    return str(int(amount * Decimal(str(factor))))


class ConservativeRiskCheck(RiskCheck):
    """Hard-cap risk check: shrink or reject, never widen.

    - Per-RFQ notional above ``max_per_rfq_notional``: both sides shrink
      proportionally (verdict ok, reason ``RISK_SIZE_REDUCED``).
    - Quoting would push the game's exposure above
      ``max_per_game_notional``: reject (``RISK_GAME_EXPOSURE``).
    - Quoting would push total exposure above ``initial_capital``: reject
      (``RISK_CAPITAL``).

    Game/capital checks run against the *post-reduction* notional, i.e. the
    exposure the reduced quote would actually add.
    """

    def __init__(self, *, max_per_rfq_notional: float = 1000.0,
                 max_per_game_notional: float = 5000.0,
                 initial_capital: float = 50000.0) -> None:
        for name, value in (
            ("max_per_rfq_notional", max_per_rfq_notional),
            ("max_per_game_notional", max_per_game_notional),
            ("initial_capital", initial_capital),
        ):
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        self._max_per_rfq_notional = float(max_per_rfq_notional)
        self._max_per_game_notional = float(max_per_game_notional)
        self._initial_capital = float(initial_capital)

    def check(self, draft: PricerResult, notional: float,
              inventory: InventoryState, game_key: str) -> RiskVerdict:
        buy_qty = str(draft.extra.get("buy_qty", "0"))
        sell_qty = str(draft.extra.get("sell_qty", "0"))
        detail: dict = {
            "notional": notional,
            "game_key": game_key,
            "max_per_rfq_notional": self._max_per_rfq_notional,
            "max_per_game_notional": self._max_per_game_notional,
            "initial_capital": self._initial_capital,
        }
        if notional <= 0:
            # Nothing at risk: pass through unchanged.
            return RiskVerdict(True, buy_qty, sell_qty, RISK_OK, detail)

        effective_notional = notional
        reason = RISK_OK
        if notional > self._max_per_rfq_notional:
            factor = self._max_per_rfq_notional / notional
            buy_qty = _scale_qty(buy_qty, factor)
            sell_qty = _scale_qty(sell_qty, factor)
            effective_notional = self._max_per_rfq_notional
            reason = RISK_SIZE_REDUCED
            detail["scale_factor"] = factor

        game_exposure = inventory.exposures.get(game_key, 0.0) + effective_notional
        detail["game_exposure_after"] = game_exposure
        if game_exposure > self._max_per_game_notional:
            return RiskVerdict(False, buy_qty, sell_qty,
                               RISK_GAME_EXPOSURE, detail, action="reject")

        total_exposure = (sum(inventory.exposures.values())
                          + effective_notional)
        detail["total_exposure_after"] = total_exposure
        if total_exposure > self._initial_capital:
            return RiskVerdict(False, buy_qty, sell_qty,
                               RISK_CAPITAL, detail, action="reject")

        return RiskVerdict(True, buy_qty, sell_qty, reason, detail,
                           action="reduce" if reason == RISK_SIZE_REDUCED else "quote")

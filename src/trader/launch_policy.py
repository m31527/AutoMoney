"""Approved initial rollout limits. This module does not enable LIVE trading."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LaunchPolicy:
    version: str = "initial-100-v1"
    capital: Decimal = Decimal("100")
    order_limit: Decimal = Decimal("15")
    exposure_limit: Decimal = Decimal("30")
    daily_loss: Decimal = Decimal("2")
    cumulative_loss: Decimal = Decimal("5")
    symbol: str = "BTCUSDT"
    ai_execution: bool = False
    automatic_capital_increase: bool = False


INITIAL_POLICY = LaunchPolicy()

"""Immutable domain records. Monetary values use Decimal, never binary floats."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timestamp: datetime
    last_price: Decimal
    bid: Decimal
    ask: Decimal
    spread: Decimal
    candles_1m: tuple[Candle, ...]
    candles_5m: tuple[Candle, ...]
    candles_15m: tuple[Candle, ...]
    candles_1h: tuple[Candle, ...]
    volume_24h: Decimal
    position: Decimal
    available_quote_balance: Decimal
    average_entry_price: Decimal
    realized_pnl_today: Decimal
    unrealized_pnl: Decimal
    trades_today: int


@dataclass(frozen=True)
class TradeProposal:
    symbol: str
    action: Action
    confidence: Decimal
    requested_notional_usd: Decimal
    reason: str
    time_horizon_minutes: int
    invalid_if: tuple[str, ...] = ()
    product: str = "SPOT"
    leverage: Decimal = Decimal("1")
    shorting: bool = False
    withdrawal: bool = False


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    rejection_reason: str | None
    approved_notional: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    market_price: Decimal


@dataclass(frozen=True)
class PortfolioSnapshot:
    timestamp: datetime
    cash: Decimal
    positions: tuple[Position, ...]
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal

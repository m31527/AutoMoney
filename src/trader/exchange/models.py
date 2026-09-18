from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError("Both Binance credentials are required for private reads")
        if not self.api_key.isascii() or any(c.isspace() for c in self.api_key):
            raise ValueError("Invalid API key format")


@dataclass(frozen=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal


@dataclass(frozen=True)
class Account:
    fetched_at: datetime
    updated_at: datetime
    account_type: str
    can_trade: bool
    can_withdraw: bool
    balances: tuple[Balance, ...]
    # /api/v3/account reports account capabilities, not API-key withdrawal permission.
    key_permissions_verified: bool = False


@dataclass(frozen=True)
class Ticker:
    symbol: str
    timestamp: datetime
    fetched_at: datetime
    last_price: Decimal
    bid: Decimal
    ask: Decimal
    volume_24h: Decimal

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class AveragePrice:
    symbol: str
    price: Decimal
    minutes: int
    timestamp: datetime
    fetched_at: datetime


@dataclass(frozen=True)
class LotFilter:
    min_quantity: Decimal
    max_quantity: Decimal
    step_size: Decimal


@dataclass(frozen=True)
class NotionalFilter:
    minimum: Decimal
    maximum: Decimal | None
    apply_min_to_market: bool
    apply_max_to_market: bool
    average_price_minutes: int


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    spot_allowed: bool
    market_allowed: bool
    lot_size: LotFilter
    market_lot_size: LotFilter | None
    notional_filters: tuple[NotionalFilter, ...]
    fetched_at: datetime


@dataclass(frozen=True)
class Order:
    symbol: str
    exchange_order_id: int
    client_order_id: str
    side: str
    order_type: str
    status: str
    requested_quantity: Decimal
    executed_quantity: Decimal
    cumulative_quote_quantity: Decimal
    updated_at: datetime

    @property
    def average_fill_price(self) -> Decimal | None:
        if self.executed_quantity == 0:
            return None
        return self.cumulative_quote_quantity / self.executed_quantity


@dataclass(frozen=True)
class Fill:
    symbol: str
    exchange_order_id: int
    trade_id: int
    quantity: Decimal
    price: Decimal
    quote_quantity: Decimal
    fee: Decimal
    fee_asset: str
    timestamp: datetime


@dataclass(frozen=True)
class Reconciliation:
    order: Order
    fills: tuple[Fill, ...]
    fills_complete: bool

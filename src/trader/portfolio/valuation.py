"""Derive equity from observed free+locked balances and current prices, never AI totals."""

from dataclasses import dataclass
from decimal import Decimal

from trader.exchange.models import Account, Ticker


def finite(value: object, *, minimum: Decimal = Decimal(0)) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= minimum


@dataclass(frozen=True)
class ValuedPosition:
    symbol: str
    free_quantity: Decimal
    total_quantity: Decimal
    price: Decimal

    @property
    def exposure(self) -> Decimal:
        return self.total_quantity * self.price


@dataclass(frozen=True)
class Valuation:
    cash: Decimal
    free_cash: Decimal
    positions: tuple[ValuedPosition, ...]

    @property
    def exposure(self) -> Decimal:
        return sum((position.exposure for position in self.positions), Decimal(0))

    @property
    def equity(self) -> Decimal:
        return self.cash + self.exposure


def value_portfolio(account: Account, tickers: tuple[Ticker, ...]) -> Valuation:
    prices = {ticker.symbol: ticker.last_price for ticker in tickers}
    if len(prices) != len(tickers) or any(not finite(p) or p == 0 for p in prices.values()):
        raise ValueError("Missing, duplicate or invalid prices")
    assets = [balance.asset for balance in account.balances]
    if len(set(assets)) != len(assets) or "USDT" not in assets:
        raise ValueError("Duplicate balances or missing quote balance")
    cash = free_cash = Decimal(0)
    positions = []
    for balance in account.balances:
        if not finite(balance.free) or not finite(balance.locked):
            raise ValueError("Invalid balance")
        total = balance.free + balance.locked
        if balance.asset == "USDT":
            free_cash, cash = balance.free, total
        elif total:
            symbol = balance.asset + "USDT"
            if symbol not in ("BTCUSDT", "ETHUSDT") or symbol not in prices:
                raise ValueError("Cannot value a nonzero holding")
            positions.append(ValuedPosition(symbol, balance.free, total, prices[symbol]))
    result = Valuation(cash, free_cash, tuple(positions))
    if result.equity <= 0:
        raise ValueError("Portfolio equity must be positive")
    return result

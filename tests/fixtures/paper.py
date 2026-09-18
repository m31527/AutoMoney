from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from tests.fixtures.risk import NOW
from trader.exchange.models import AveragePrice, LotFilter, NotionalFilter, SymbolInfo, Ticker
from trader.market.data import MarketObservation
from trader.models import Action, Candle, TradeProposal
from trader.strategy.baseline import StrategyResult

D = Decimal


def market(symbol="BTCUSDT", price="50000", *, now=NOW, crossover=0):
    price = D(price)
    candles = tuple(
        Candle(
            now - timedelta(minutes=5 * (21 - i)),
            price,
            price,
            price,
            price if i < 20 else price * (1 + D(crossover) / 100),
            D("10"),
        )
        for i in range(21)
    )
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hourly = (Candle(midnight, price, price, price, price, D("1")),)
    ticker = Ticker(symbol, now, now, price, price - D("0.1"), price + D("0.1"), D("100"))
    metadata = SymbolInfo(
        symbol,
        symbol[:-4],
        "USDT",
        "TRADING",
        True,
        True,
        LotFilter(D("0.00001"), D("100"), D("0.00001")),
        LotFilter(D(0), D("10"), D(0)),
        (NotionalFilter(D("5"), D("1000000"), True, False, 5),),
        now,
    )
    return MarketObservation(symbol, now, ticker, metadata, candles, candles, candles, hourly)


def average(price="50000", *, now=NOW):
    return AveragePrice("BTCUSDT", D(price), 5, now, now)


@dataclass(frozen=True)
class FixedStrategy:
    action: Action = Action.BUY
    notional: Decimal = D("50")
    name: str = "fixture"

    def propose(self, snapshot, now):
        return StrategyResult(
            TradeProposal(
                snapshot.symbol,
                self.action,
                D("0.8"),
                self.notional,
                "Synthetic integration fixture",
                240,
            ),
            D("100"),
        )

"""Offline synthetic demonstration of the complete baseline -> risk -> PAPER ledger flow."""

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.config import AppConfig, TradingMode
from trader.exchange.models import AveragePrice, LotFilter, NotionalFilter, SymbolInfo, Ticker
from trader.execution.paper import PaperEngine
from trader.market.data import MarketObservation
from trader.models import Candle
from trader.storage.db import connect
from trader.storage.repository import encode
from trader.strategy.baseline import SMAStrategy


def observation(now: datetime, price: Decimal, change: Decimal) -> MarketObservation:
    candles = tuple(
        Candle(
            now - timedelta(minutes=5 * (21 - i)),
            price,
            price * (1 + abs(change)),
            price * (1 - abs(change)),
            price if i < 20 else price * (1 + change),
            Decimal(10),
        )
        for i in range(21)
    )
    ticker = Ticker(
        "BTCUSDT", now, now, price, price - Decimal("0.1"), price + Decimal("0.1"), Decimal(100)
    )
    info = SymbolInfo(
        "BTCUSDT",
        "BTC",
        "USDT",
        "TRADING",
        True,
        True,
        LotFilter(Decimal("0.00001"), Decimal(100), Decimal("0.00001")),
        None,
        (NotionalFilter(Decimal(5), None, True, False, 5),),
        now,
    )
    return MarketObservation("BTCUSDT", now, ticker, info, candles, candles, candles, candles)


def main() -> None:
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    with tempfile.TemporaryDirectory() as directory:
        connection = connect(Path(directory) / "paper-demo.db")
        try:
            engine = PaperEngine(connection, AppConfig(mode=TradingMode.PAPER))
            engine.initialize(now)
            for label, minutes, price, change in (
                ("bullish_buy", 0, "50000", "0.1"),
                ("flat_hold", 5, "50000", "0"),
                ("bearish_sell", 31, "51000", "-0.1"),
            ):
                timestamp = now + timedelta(minutes=minutes)
                market = observation(timestamp, Decimal(price), Decimal(change))
                result = engine.step(
                    "BTCUSDT",
                    {"BTCUSDT": market},
                    SMAStrategy(),
                    cycle_id=label,
                    now=timestamp,
                    average=AveragePrice("BTCUSDT", Decimal(price), 5, timestamp, timestamp),
                )
                print(encode({"synthetic": True, **result}))
        finally:
            connection.close()


if __name__ == "__main__":
    main()

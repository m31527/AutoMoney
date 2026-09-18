"""Public market observations. Account cost basis and PnL are not invented here."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from trader.exchange.base import ExchangeAdapter
from trader.exchange.errors import ExchangeError
from trader.exchange.models import SymbolInfo, Ticker
from trader.models import Candle


class StaleMarketData(ExchangeError):
    code = "STALE_MARKET_DATA"


@dataclass(frozen=True)
class MarketObservation:
    symbol: str
    timestamp: datetime
    ticker: Ticker
    metadata: SymbolInfo
    candles_1m: tuple[Candle, ...]
    candles_5m: tuple[Candle, ...]
    candles_15m: tuple[Candle, ...]
    candles_1h: tuple[Candle, ...]
    data_kind: str = "PUBLIC_MARKET_OBSERVATION"


def collect_market(
    exchange: ExchangeAdapter,
    symbol: str,
    limit: int = 100,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MarketObservation:
    started = now()
    metadata = exchange.get_exchange_info(symbol)
    ticker = exchange.get_ticker(symbol)
    candles = {
        interval: exchange.get_klines(symbol, interval, limit)
        for interval in ("1m", "5m", "15m", "1h")
    }
    finished = now()
    if (
        not -5 <= (finished - ticker.timestamp).total_seconds() <= 60
        or not 0 <= (finished - started).total_seconds() <= 60
    ):
        raise StaleMarketData()
    for interval, duration in (("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600)):
        if (
            not candles[interval]
            or not -5
            <= (finished - candles[interval][-1].timestamp).total_seconds()
            <= duration + 60
        ):
            raise StaleMarketData()
    return MarketObservation(
        symbol,
        finished,
        ticker,
        metadata,
        candles["1m"],
        candles["5m"],
        candles["15m"],
        candles["1h"],
    )

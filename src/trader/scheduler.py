"""Synchronous five-minute PAPER evaluations; order limits remain in the risk engine."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from trader.exchange.binance import BinanceSpotAdapter
from trader.execution.paper import PaperEngine
from trader.market.data import collect_market
from trader.strategy.ai_strategy import AIStrategy
from trader.strategy.baseline import Strategy


def run_paper(
    engine: PaperEngine,
    exchange: BinanceSpotAdapter,
    strategy: Strategy | AIStrategy,
    symbol: str,
    *,
    cycles: int = 1,
    emit: Callable[[dict[str, Any]], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    if type(cycles) is not int or cycles < 0:
        raise ValueError("cycles must be nonnegative; zero means continuous")
    completed = 0
    while cycles == 0 or completed < cycles:
        markets = {
            s: collect_market(exchange, s, 100, now=clock) for s in engine.config.risk.symbols
        }
        average = exchange.get_average_price(symbol)
        prepared: Strategy = (
            strategy.prepare(engine.prepare_snapshot(symbol, markets, clock()), clock())
            if isinstance(strategy, AIStrategy)
            else strategy
        )
        emit(
            engine.step(
                symbol, markets, prepared, cycle_id=uuid4().hex, now=clock(), average=average
            )
        )
        completed += 1
        if cycles == 0 or completed < cycles:
            sleep(300)

"""Synchronous five-minute PAPER evaluations; order limits remain in the risk engine."""

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.errors import ExchangeError
from trader.execution.paper import PaperEngine
from trader.market.data import collect_market
from trader.models import Action
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
        prepared: Strategy
        if isinstance(strategy, AIStrategy):
            snapshot = engine.prepare_snapshot(symbol, markets, clock())
            ai = strategy.prepare(
                snapshot,
                clock(),
                budget=engine.ai_budget(snapshot, markets),
                max_age_seconds=engine.config.risk.max_data_age_seconds,
            )
            outcome = "NOT_ACTIONABLE"
            if ai.result.proposal.action in (Action.BUY, Action.SELL):
                try:
                    # Refresh quotes only; do not silently replace the model's candle context.
                    markets = {
                        s: replace(m, ticker=exchange.get_ticker(s)) for s, m in markets.items()
                    }
                    average = exchange.get_average_price(symbol)
                    fresh = engine.prepare_snapshot(symbol, markets, clock())
                    ai, outcome = ai.revalidate(fresh, clock())
                except ExchangeError:
                    strategy.record_recheck(ai, clock(), "QUOTE_FETCH_FAILED")
                    raise
            strategy.record_recheck(ai, clock(), outcome)
            prepared = ai
        else:
            prepared = strategy
        emit(
            engine.step(
                symbol, markets, prepared, cycle_id=uuid4().hex, now=clock(), average=average
            )
        )
        completed += 1
        if cycles == 0 or completed < cycles:
            sleep(300)

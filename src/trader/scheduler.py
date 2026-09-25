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
from trader.storage.repository import encode, json_default
from trader.storage.transaction import transaction
from trader.strategy.ai_strategy import AIStrategy
from trader.strategy.baseline import Strategy
from trader.strategy.prefilter import PrefilterHold, evaluate


def run_paper(
    engine: PaperEngine,
    exchange: BinanceSpotAdapter,
    strategy: Strategy | AIStrategy,
    symbol: str,
    *,
    cycles: int = 1,
    ai_prefilter: bool = False,
    ai_direction_filter: bool = False,
    emit: Callable[[dict[str, Any]], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    if type(cycles) is not int or cycles < 0:
        raise ValueError("cycles must be nonnegative; zero means continuous")
    if ai_direction_filter and not ai_prefilter:
        raise ValueError("Direction filter requires AI prefilter")
    completed = 0
    while cycles == 0 or completed < cycles:
        markets = {
            s: collect_market(exchange, s, 100, now=clock) for s in engine.config.risk.symbols
        }
        average = exchange.get_average_price(symbol)
        cycle_id = uuid4().hex
        prepared: Strategy
        if isinstance(strategy, AIStrategy):
            snapshot = engine.prepare_snapshot(symbol, markets, clock())
            skip = False
            if ai_prefilter:
                account_flat = not any(q for q, _ in engine.status()["positions"].values())
                evaluation = evaluate(
                    snapshot,
                    engine.config.risk,
                    clock(),
                    account_flat=account_flat,
                    direction_filter=ai_direction_filter,
                )
                evaluation["cycle_id"] = cycle_id
                skip = evaluation["skipped"]
                with transaction(engine.connection):
                    engine.connection.execute(
                        "INSERT INTO system_events(timestamp,severity,event_type,payload_json) "
                        "VALUES (?,?,?,?)",
                        (
                            json_default(clock()),
                            "INFO",
                            "AI_PREFILTER_EVALUATED",
                            encode(evaluation),
                        ),
                    )
            if skip:
                prepared = PrefilterHold(
                    name=evaluation["policy_version"], reason=evaluation["reason"]
                )
            else:
                ai = strategy.prepare(
                    snapshot,
                    clock(),
                    budget=engine.ai_budget(snapshot, markets),
                    max_age_seconds=engine.config.risk.max_data_age_seconds,
                    require_uptrend=ai_direction_filter,
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
            engine.step(symbol, markets, prepared, cycle_id=cycle_id, now=clock(), average=average)
        )
        completed += 1
        if cycles == 0 or completed < cycles:
            sleep(300)

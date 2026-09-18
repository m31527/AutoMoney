"""Independent Ollama PAPER worker; comparisons and global kill switch stay intact."""

import fcntl
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from trader.config import AppConfig, TradingMode
from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.errors import ExchangeError
from trader.execution.paper import PaperEngine
from trader.safety.kill_switch import KillSwitch
from trader.scheduler import run_paper
from trader.storage.db import connect
from trader.storage.repository import encode, json_default
from trader.storage.transaction import transaction
from trader.strategy.ai_strategy import AIStrategy
from trader.strategy.ollama import OllamaProvider


def open_account(config: AppConfig, provider: OllamaProvider) -> sqlite3.Connection:
    if config.mode != TradingMode.PAPER:
        raise ValueError("Ollama experiment requires PAPER")
    root = config.database_path.parent / "ollama"
    connection = connect(root / "paper.db")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS local_ai_config "
            "(id INTEGER PRIMARY KEY CHECK(id=1), definition TEXT NOT NULL)"
        )
        definition = encode(
            {
                "version": 1,
                "provider": provider.name,
                "model": provider.model,
                "base_url": provider.base_url,
                "risk": asdict(config.risk),
            }
        )
        with transaction(connection):
            old = connection.execute("SELECT definition FROM local_ai_config WHERE id=1").fetchone()
            if old:
                previous, current = json.loads(old[0]), json.loads(definition)
                previous.pop("base_url", None)
                current.pop("base_url", None)
                if previous != current:
                    raise ValueError("Model/risk settings changed; use a new experiment directory")
                connection.execute(
                    "UPDATE local_ai_config SET definition=? WHERE id=1", (definition,)
                )
            connection.execute("INSERT OR IGNORE INTO local_ai_config VALUES (1,?)", (definition,))
        return connection
    except BaseException:
        connection.close()
        raise


def run_local_ai(
    config: AppConfig,
    switch: KillSwitch,
    provider: OllamaProvider,
    *,
    cycles: int,
    emit: Callable[[dict[str, Any]], None],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if type(cycles) is not int or cycles < 0:
        raise ValueError("Nonnegative cycle count required")
    root = config.database_path.parent / "ollama"
    root.mkdir(parents=True, exist_ok=True)
    with (root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another Ollama worker is running") from None
        try:
            connection = open_account(config, provider)
            try:
                account_config = replace(config, database_url="sqlite:///" + str(root / "paper.db"))
                engine = PaperEngine(connection, account_config, switch)
                engine.initialize(clock())
                strategy = AIStrategy(provider, connection)
                exchange = BinanceSpotAdapter(
                    config.mode, kill_switch=switch, symbols=config.risk.symbols
                )
                count = 0
                while cycles == 0 or count < cycles:
                    if switch.active:
                        emit({"status": "PAUSED", "provider": "ollama"})
                    else:
                        symbols = sorted(
                            config.risk.symbols, reverse=int(clock().timestamp()) // 1800 % 2 == 1
                        )
                        for symbol in symbols:
                            if switch.active:
                                break
                            try:
                                # Each call fetches fresh data before inference, then rechecks age.
                                run_paper(
                                    engine,
                                    exchange,
                                    strategy,
                                    symbol,
                                    cycles=1,
                                    emit=emit,
                                    clock=clock,
                                    sleep=sleep,
                                )
                            except ExchangeError as error:
                                with transaction(connection):
                                    connection.execute(
                                        "INSERT INTO system_events"
                                        "(timestamp,severity,event_type,payload_json) "
                                        "VALUES (?,?,?,?)",
                                        (
                                            json_default(clock()),
                                            "ERROR",
                                            error.code,
                                            encode({"symbol": symbol}),
                                        ),
                                    )
                                emit(
                                    {"status": "MARKET_ERROR", "code": error.code, "symbol": symbol}
                                )
                    count += 1
                    if cycles == 0 or count < cycles:
                        sleep(300)
            finally:
                connection.close()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

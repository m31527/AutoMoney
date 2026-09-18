import argparse
import os
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from trader.config import load_config
from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.errors import ExchangeError
from trader.exchange.models import Credentials
from trader.execution.paper import PaperEngine
from trader.experiment import Experiment
from trader.logging import configure_logging
from trader.market.data import collect_market
from trader.safety.kill_switch import KillSwitch
from trader.scheduler import run_paper
from trader.storage.db import connect
from trader.storage.exchange_journal import ExchangeJournal
from trader.storage.repository import Repository, encode
from trader.strategy.ai_strategy import AIStrategy
from trader.strategy.baseline import HoldStrategy, SMAStrategy
from trader.strategy.provider import load_provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Crypto trader — Phase E optional AI PAPER trading"
    )
    parser.add_argument("--config", type=Path, help="Risk settings TOML file")
    parser.add_argument(
        "command",
        choices=(
            "status",
            "kill",
            "resume",
            "market",
            "account",
            "paper-init",
            "paper-status",
            "paper-run",
            "compare-run",
            "report",
            "dashboard",
            "ollama-run",
        ),
    )
    parser.add_argument("--strategy", choices=("hold", "sma", "ai"), default="hold")
    parser.add_argument("--cycles", type=int, default=1, help="PAPER evaluations, 0 for continuous")
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), default="BTCUSDT")
    parser.add_argument("--limit", type=int, default=100, help="Candles per interval (1–1000)")
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard bind address")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard port")
    args = parser.parse_args(argv)
    logger = configure_logging()
    try:
        config = load_config(args.config)
        if args.command == "dashboard":
            from trader.dashboard import serve

            serve(config.database_path.parent, args.host, args.port)
            return 0
        connection = connect(config.database_path)
        try:
            repository = Repository(connection)
            switch = KillSwitch(repository)
            if args.command == "kill":
                switch.kill()
            elif args.command == "resume":
                switch.resume()
            if args.command == "ollama-run":
                from trader.local_ai import run_local_ai
                from trader.strategy.ollama import OllamaProvider

                provider = load_provider()
                if not isinstance(provider, OllamaProvider):
                    raise ValueError("ollama-run requires AI_PROVIDER=ollama")
                run_local_ai(
                    config,
                    switch,
                    provider,
                    cycles=args.cycles,
                    emit=lambda value: print(encode(value), flush=True),
                )
                return 0
            if args.command in ("compare-run", "report"):
                experiment = Experiment(config, switch)
                try:
                    if args.command == "report":
                        print(experiment.report(datetime.now(UTC)))
                    else:
                        exchange = BinanceSpotAdapter(
                            config.mode, kill_switch=switch, symbols=config.risk.symbols
                        )
                        experiment.run(
                            exchange,
                            cycles=args.cycles,
                            emit=lambda message: print(message, flush=True),
                        )
                finally:
                    experiment.close()
                return 0
            if args.command.startswith("paper-"):
                engine = PaperEngine(connection, config)
                if args.command == "paper-init":
                    engine.initialize(datetime.now(UTC))
                if args.command == "paper-run":
                    engine.status()  # Verify initialization before making network requests.
                    exchange = BinanceSpotAdapter(
                        config.mode, kill_switch=switch, symbols=config.risk.symbols
                    )
                    run_paper(
                        engine,
                        exchange,
                        AIStrategy(load_provider(), connection)
                        if args.strategy == "ai"
                        else SMAStrategy()
                        if args.strategy == "sma"
                        else HoldStrategy(),
                        args.symbol,
                        cycles=args.cycles,
                        emit=lambda value: print(encode(value), flush=True),
                    )
                else:
                    print(encode(engine.status()))
                logger.info("CLI_PAPER_COMPLETED")
                return 0
            if args.command in ("market", "account"):
                credentials = None
                if args.command == "account":
                    credentials = Credentials(
                        os.environ.get("BINANCE_API_KEY", ""),
                        os.environ.get("BINANCE_API_SECRET", ""),
                    )
                exchange = BinanceSpotAdapter(
                    config.mode,
                    credentials,
                    kill_switch=switch,
                    journal=ExchangeJournal(connection),
                    symbols=config.risk.symbols,
                )
                if args.command == "market":
                    observation = collect_market(exchange, args.symbol, args.limit)
                    identifier = repository.save_market_snapshot(observation)
                    print(encode({"market_snapshot_id": identifier, **asdict(observation)}))
                else:
                    print(encode(asdict(exchange.get_account())))
                logger.info("CLI_" + args.command.upper() + "_COMPLETED")
                return 0
            print(
                encode(
                    {
                        "mode": config.mode,
                        "phase": "E",
                        "execution_available": False,
                        "database": str(config.database_path.resolve()),
                        "safety": asdict(repository.safety_state()),
                        "risk": asdict(config.risk),
                    }
                )
            )
            logger.info("CLI_" + args.command.upper() + "_COMPLETED")
        finally:
            connection.close()
    except KeyboardInterrupt:
        logger.info("OPERATOR_INTERRUPTED")
        return 130
    except ExchangeError as error:
        logger.error(error.code)
        return 1
    except (ValueError, OSError, sqlite3.Error, RuntimeError):
        # Exception text may contain untrusted paths/configuration or credentials.
        logger.error("STARTUP_OR_STORAGE_FAILURE")
        return 1
    return 0

"""Isolated Spot Testnet round-trip acceptance; never imported by PAPER workers."""

import argparse
import fcntl
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.errors import ExchangeError
from trader.exchange.models import Credentials, Reconciliation
from trader.exchange.testnet_transport import TestnetTransport
from trader.execution.filters import market_quantity
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.exchange_journal import ExchangeJournal
from trader.storage.repository import Repository, encode


def balances(adapter: BinanceSpotAdapter) -> dict[str, Decimal]:
    return {b.asset: b.free + b.locked for b in adapter.get_balances()}


def run(adapter: BinanceSpotAdapter, state: dict[str, Any], save: Any) -> dict[str, Any]:
    symbol = "BTCUSDT"
    journal = adapter.journal
    assert journal is not None
    if "before" not in state:
        if adapter.get_open_orders(symbol):
            raise ValueError("Use a dedicated Testnet account with no open BTC orders")
        state["before"] = balances(adapter)
        save(state)
    reconciled: list[Reconciliation] = []
    for side in ("BUY", "SELL"):
        client_id = "at_" + hashlib.sha256((state["run_id"] + side).encode()).hexdigest()[:30]
        if journal.expected_request(client_id) is None:
            # Size once, persist before sending. Restart reuses the journal, never re-POSTs.
            ticker = adapter.get_ticker(symbol)
            metadata = adapter.get_exchange_info(symbol)
            average = adapter.get_average_price(symbol)
            current = {b.asset: b.free for b in adapter.get_balances()}
            if side == "BUY":
                if current.get("USDT", Decimal(0)) < Decimal(55):
                    raise ValueError("Need at least 55 virtual USDT")
                maximum = Decimal(50) / ticker.ask
            else:
                bought = reconciled[0]
                fees = sum((f.fee for f in bought.fills if f.fee_asset == "BTC"), Decimal(0))
                maximum = min(bought.order.executed_quantity - fees, current.get("BTC", Decimal(0)))
            quantity = market_quantity(
                maximum, metadata, ticker, average, now=datetime.now(UTC), max_age=60
            )
            adapter.place_order(symbol, side, quantity, client_id)
        result = adapter.reconcile(symbol, client_id)
        state[side] = asdict(result)
        save(state)
        if result.order.status != "FILLED" or not result.fills_complete:
            raise ValueError("ORDER_NOT_FULLY_RECONCILED: rerun same run-id to query; no new order")
        reconciled.append(result)
    after = balances(adapter)
    expected = {asset: Decimal(str(v)) for asset, v in state["before"].items()}
    for result in reconciled:
        sign = Decimal(1) if result.order.side == "BUY" else Decimal(-1)
        for fill in result.fills:
            expected["BTC"] = expected.get("BTC", Decimal(0)) + sign * fill.quantity
            expected["USDT"] = expected.get("USDT", Decimal(0)) - sign * fill.quote_quantity
            expected[fill.fee_asset] = expected.get(fill.fee_asset, Decimal(0)) - fill.fee
    mismatch = {
        asset: {"expected": expected.get(asset, Decimal(0)), "actual": after.get(asset, Decimal(0))}
        for asset in set(expected) | set(after)
        if expected.get(asset, Decimal(0)) != after.get(asset, Decimal(0))
    }
    state.update(
        after=after,
        balance_mismatches=mismatch,
        status="PASS" if not mismatch else "FAIL_BALANCE_RECONCILIATION",
        scope="Testnet round-trip only; not profitability or live readiness",
        fault_injection="NOT_RUN_ON_REMOTE_EXCHANGE",
    )
    save(state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/testnet-acceptance"))
    parser.add_argument("--run-id", default="acceptance-001")
    parser.add_argument("--execute-testnet", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    connection = connect(args.data / "acceptance.db")
    switch = KillSwitch(Repository(connection))
    connection.execute(
        "CREATE TABLE IF NOT EXISTS acceptance_runs (id TEXT PRIMARY KEY, payload TEXT)"
    )
    if args.stop:
        switch.kill()
        print("Testnet acceptance stopped; PAPER unchanged")
        connection.close()
        return
    if not args.execute_testnet:
        print(
            encode(
                {
                    "status": "NOT_RUN",
                    "requires": [
                        "BINANCE_TESTNET_API_KEY",
                        "BINANCE_TESTNET_API_SECRET",
                        "--execute-testnet",
                    ],
                    "live_enabled": False,
                }
            )
        )
        connection.close()
        return

    def save(state: dict[str, Any]) -> None:
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO acceptance_runs VALUES (?,?)", (args.run_id, encode(state))
            )
        (args.data / "report.json").write_text(encode(state))

    with (args.data / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        row = connection.execute(
            "SELECT payload FROM acceptance_runs WHERE id=?", (args.run_id,)
        ).fetchone()
        if row is None:
            previous = connection.execute("SELECT payload FROM acceptance_runs").fetchall()
            if any(json.loads(r[0]).get("status") != "PASS" for r in previous):
                connection.close()
                raise SystemExit(
                    "Unfinished run exists: reuse its run-id; do not create a new order"
                )
        state = json.loads(row[0]) if row else {"run_id": args.run_id, "status": "STARTED"}
        if state.get("status") == "PASS":
            print(encode(state))
            connection.close()
            return
        try:
            if switch.active:
                raise ValueError("STOPPED: inspect persistent safety state before further testing")
            credentials = Credentials(
                os.environ["BINANCE_TESTNET_API_KEY"], os.environ["BINANCE_TESTNET_API_SECRET"]
            )
            adapter = BinanceSpotAdapter(
                credentials=credentials,
                transport=TestnetTransport(),
                journal=ExchangeJournal(connection),
                kill_switch=switch,
            )
            print(encode(run(adapter, state, save)))
        except (ExchangeError, ValueError, KeyError) as error:
            state.update(
                status="BLOCKED",
                error=getattr(
                    error,
                    "code",
                    str(error) if isinstance(error, ValueError) else type(error).__name__,
                ),
                next_step="Preserve database and run-id; inspect report, never delete to retry",
            )
            save(state)
            print(encode(state))
            raise SystemExit(1) from None
        finally:
            connection.close()


if __name__ == "__main__":
    main()

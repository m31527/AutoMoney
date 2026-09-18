import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.fixtures.paper import FixedStrategy, average, market
from tests.fixtures.risk import NOW
from trader.config import AppConfig, TradingMode
from trader.execution.paper import PaperEngine
from trader.models import Action
from trader.scheduler import run_paper
from trader.storage.db import connect
from trader.storage.repository import Repository
from trader.strategy.baseline import HoldStrategy, SMAStrategy

D = Decimal


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "paper.db"
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)
        self.config = AppConfig(mode=TradingMode.PAPER)
        self.engine = PaperEngine(self.connection, self.config)
        self.engine.initialize(NOW)

    def step(self, identifier="first", *, now=NOW, price="50000", strategy=None, observation=None):
        return self.engine.step(
            "BTCUSDT",
            {"BTCUSDT": observation or market(price=price, now=now)},
            strategy or FixedStrategy(),
            cycle_id=identifier,
            now=now,
            average=average(price, now=now),
        )

    def test_buy_records_atomic_full_audit_chain_and_fees(self):
        result = self.step()
        self.assertEqual(result["status"], "FILLED", result)
        for table in (
            "orders",
            "fills",
            "risk_executions",
            "risk_assessments",
            "risk_decisions",
            "ai_decisions",
            "market_snapshots",
            "portfolio_snapshots",
            "paper_cycles",
        ):
            self.assertEqual(
                self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1, table
            )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        order = self.connection.execute("SELECT * FROM orders").fetchone()
        self.assertEqual(D(order["requested_qty"]) % D("0.00001"), 0)
        portfolio = result["portfolio"]
        self.assertGreater(D(portfolio["fees"]), 0)
        self.assertLess(D(portfolio["net_pnl"]), 0)
        self.assertEqual(
            D(portfolio["net_pnl"]), D(portfolio["realized_pnl"]) + D(portfolio["unrealized_pnl"])
        )

    def test_restart_preserves_holdings_and_cycle_is_idempotent(self):
        first = self.step()
        connection = connect(self.path)
        self.addCleanup(connection.close)
        restarted = PaperEngine(connection, self.config)
        restarted.initialize(NOW)
        again = restarted.step("BTCUSDT", {}, FixedStrategy(), cycle_id="first", now=NOW)
        self.assertEqual(json.loads(json.dumps(first)), again)
        self.assertEqual(restarted.status()["trades"], 1)
        self.assertLess(D(restarted.status()["cash"]), D("1000"))

    def test_duplicate_cycle_conflict_rejected(self):
        self.step()
        with self.assertRaises(ValueError):
            self.engine.step("ETHUSDT", {}, FixedStrategy(), cycle_id="first", now=NOW)

    def test_hold_never_submits_and_does_not_count(self):
        result = self.step(strategy=HoldStrategy())
        self.assertEqual(result["status"], "HOLD")
        self.assertEqual(self.engine.status()["trades"], 0)
        self.assertEqual(self.engine.status()["cash"], "1000")

    def test_risk_rejection_cannot_be_bypassed(self):
        result = self.step(strategy=FixedStrategy(notional=D("101")))
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("ORDER_NOTIONAL_LIMIT", result["reasons"])
        self.assertEqual(self.engine.status()["trades"], 0)

    def test_cooldown_blocks_following_cycle(self):
        self.step()
        result = self.step("second", now=NOW + timedelta(minutes=5))
        self.assertIn("COOLDOWN", result["reasons"])
        self.assertEqual(self.engine.status()["trades"], 1)

    def test_buy_then_sell_accounts_cost_basis_and_net_realized_pnl(self):
        self.step()
        result = self.step(
            "sell",
            now=NOW + timedelta(minutes=31),
            price="51000",
            strategy=FixedStrategy(Action.SELL, D("40")),
        )
        self.assertEqual(result["status"], "FILLED", result)
        self.assertGreater(D(result["realized_delta"]), 0)
        p = result["portfolio"]
        self.assertAlmostEqual(
            D(p["net_pnl"]), D(p["realized_pnl"]) + D(p["unrealized_pnl"]), places=20
        )
        self.assertEqual(self.engine.status()["trades"], 2)

    def test_filters_can_veto_risk_approval(self):
        result = self.step(strategy=FixedStrategy(notional=D("1")))
        self.assertIn("MIN_NOTIONAL", result["reasons"])
        self.assertEqual(self.engine.status()["trades"], 0)
        self.assertEqual(
            self.connection.execute("SELECT approved FROM risk_decisions").fetchone()[0], 0
        )

    def test_persistent_kill_prevents_fill(self):
        Repository(self.connection).set_killed(True, "test")
        self.assertIn("KILL_SWITCH_CLEAR", self.step()["reasons"])
        self.assertEqual(self.engine.status()["trades"], 0)

    def test_failure_in_fill_rolls_back_risk_order_and_account(self):
        self.connection.execute(
            "CREATE TRIGGER fail_fill BEFORE INSERT ON fills BEGIN SELECT RAISE(ABORT,'test'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.step()
        for table in ("risk_assessments", "orders", "risk_executions", "paper_cycles"):
            self.assertEqual(
                self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
            )
        self.assertEqual(self.engine.status()["cash"], "1000")

    def test_concurrent_cycles_serialize_risk_check_and_balance_update(self):
        def run(identifier):
            connection = connect(self.path)
            try:
                engine = PaperEngine(connection, self.config)
                return engine.step(
                    "BTCUSDT",
                    {"BTCUSDT": market()},
                    FixedStrategy(),
                    cycle_id=identifier,
                    now=NOW,
                    average=average(),
                )["status"]
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(run, ("one", "two")))
        self.assertEqual(sorted(result), ["FILLED", "REJECTED"])
        self.assertEqual(self.engine.status()["trades"], 1)

    def test_day_rollover_uses_midnight_open_not_current_price(self):
        self.step()
        tomorrow = NOW + timedelta(days=1)
        observation = market(price="55000", now=tomorrow)
        observation = replace(
            observation, candles_1h=(replace(observation.candles_1h[0], open=D("51000")),)
        )
        before = self.engine.status()
        qty = before["positions"]["BTCUSDT"][0]
        self.step(
            "next", now=tomorrow, observation=observation, price="55000", strategy=HoldStrategy()
        )
        self.assertEqual(
            self.engine.risk.day_state(tomorrow.date()).opening_equity,
            D(before["cash"]) + qty * D("51000"),
        )

    def test_missing_midnight_candle_blocks_new_day_trade(self):
        self.step()
        tomorrow = NOW + timedelta(days=1)
        observation = replace(market(now=tomorrow), candles_1h=())
        result = self.step("next", now=tomorrow, observation=observation)
        self.assertIn("DAY_BASELINE_AVAILABLE", result["reasons"])
        self.assertEqual(self.engine.status()["trades"], 1)

    def test_missing_held_price_refuses_cycle(self):
        self.step()
        with self.assertRaises(ValueError):
            self.engine.step(
                "ETHUSDT",
                {"ETHUSDT": market("ETHUSDT", "2500")},
                HoldStrategy(),
                cycle_id="missing",
                now=NOW,
            )

    def test_clock_cannot_move_backwards(self):
        self.step()
        with self.assertRaises(ValueError):
            self.step("earlier", now=NOW - timedelta(seconds=1))

    def test_non_paper_modes_cannot_initialize(self):
        for config in (AppConfig(), AppConfig(mode=TradingMode.LIVE, enable_live_trading=True)):
            with self.assertRaises(ValueError):
                PaperEngine(self.connection, config)

    def test_database_with_foreign_trading_history_cannot_initialize(self):
        path = Path(self.temp.name) / "used.db"
        connection = connect(path)
        self.addCleanup(connection.close)
        with connection:
            connection.execute("INSERT INTO exchange_submissions VALUES ('x','{}','now')")
        with self.assertRaises(ValueError):
            PaperEngine(connection, self.config).initialize(NOW)

    def test_sma_strategy_runs_through_full_pipeline(self):
        result = self.step(observation=market(crossover=10), strategy=SMAStrategy())
        self.assertEqual(result["status"], "FILLED", result)

    def test_scheduler_defaults_hold_and_waits_five_minutes(self):
        exchange = unittest.mock.Mock()
        exchange.get_average_price.return_value = average()
        sleeps, results = [], []
        with patch(
            "trader.scheduler.collect_market",
            side_effect=lambda ex, symbol, *a, **k: market(symbol),
        ):
            run_paper(
                self.engine,
                exchange,
                HoldStrategy(),
                "BTCUSDT",
                cycles=2,
                emit=results.append,
                sleep=sleeps.append,
                clock=lambda: NOW,
            )
        self.assertEqual(sleeps, [300])
        self.assertEqual([r["status"] for r in results], ["HOLD", "HOLD"])
        exchange.place_order.assert_not_called()

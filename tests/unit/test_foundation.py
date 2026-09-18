import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trader.config import AppConfig, ConfigError, RiskConfig, TradingMode, load_config
from trader.models import PortfolioSnapshot
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.repository import Repository, encode


class ConfigurationTests(unittest.TestCase):
    def test_defaults(self):
        config = load_config(environ={})
        self.assertEqual(config.mode, TradingMode.TESTNET)
        self.assertFalse(config.enable_live_trading)
        self.assertEqual(config.risk.max_order_notional_usd, Decimal("100"))

    def test_live_requires_exact_opt_in(self):
        for flag in ("false", "TRUE", "1", "yes", ""):
            with self.subTest(flag=flag), self.assertRaises(ConfigError):
                load_config(environ={"TRADING_MODE": "LIVE", "ENABLE_LIVE_TRADING": flag})
        self.assertEqual(
            load_config(environ={"TRADING_MODE": "LIVE", "ENABLE_LIVE_TRADING": "true"}).mode,
            TradingMode.LIVE,
        )

    def test_invalid_modes(self):
        for mode in ("testnet", "FUTURES", ""):
            with self.assertRaises(ConfigError):
                load_config(environ={"TRADING_MODE": mode})

    def test_dangerous_risk_configuration(self):
        cases = [
            {"leverage_allowed": True},
            {"shorting_allowed": True},
            {"withdrawals_allowed": True},
            {"withdrawals_allowed": "false"},
            {"max_order_notional_usd": "NaN"},
            {"max_daily_loss_usd": "Infinity"},
            {"max_daily_loss_usd": 0},
            {"max_trades_per_day": True},
            {"max_trades_per_day": 1.5},
            {"min_minutes_between_trades": -1},
            {"minimum_confidence": 1.01},
            {"max_symbol_allocation_pct": 101},
            {"symbols": ["UNSUPPORTEDUSDT"]},
            {"symbols": []},
            {"symbols": ["BTCUSDT", "BTCUSDT"]},
            {"symbols": "BTCUSDT"},
            {"max_order_notional_usd": 301},
            {"max_total_position_usd": 1001},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ConfigError):
                RiskConfig(**case)

    def test_toml_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk.toml"
            path.write_text("max_order_notional_usd = 50\nminimum_confidence = 0.7\n")
            risk = load_config(path, {}).risk
            self.assertEqual(risk.max_order_notional_usd, Decimal("50"))
            self.assertEqual(risk.minimum_confidence, Decimal("0.7"))
            path.write_text("max_trades_per_dya = 100\n")
            with self.assertRaises(ConfigError):
                load_config(path, {})

    def test_default_file_matches_defaults(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(load_config(root / "config/default.toml", {}).risk, RiskConfig())

    def test_persistence_required(self):
        for url in ("sqlite:///:memory:", "sqlite:///", "postgres://db", "sqlite:///x?mode=rw"):
            with self.assertRaises(ConfigError):
                AppConfig(database_url=url)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.db"
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)
        self.repository = Repository(self.connection)

    def test_kill_survives_restart_and_resume_is_audited(self):
        KillSwitch(self.repository).kill()
        second = connect(self.path)
        try:
            switch = KillSwitch(Repository(second))
            self.assertTrue(switch.active)
            switch.resume()
            self.assertFalse(switch.active)
            self.assertFalse(self.repository.safety_state().killed)
            self.assertEqual(
                [e["event_type"] for e in self.repository.events()],
                ["KILL_SWITCH_ACTIVATED", "KILL_SWITCH_RESUMED"],
            )
        finally:
            second.close()

    def test_other_connection_observes_kill(self):
        second = connect(self.path)
        try:
            observer = KillSwitch(Repository(second))
            self.assertFalse(observer.active)
            KillSwitch(self.repository).kill()
            self.assertTrue(observer.active)
        finally:
            second.close()

    def test_event_failure_rolls_back_and_runtime_stays_killed(self):
        self.connection.execute(
            "CREATE TRIGGER reject_event BEFORE INSERT ON system_events "
            "BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
        switch = KillSwitch(self.repository)
        with self.assertRaises(sqlite3.IntegrityError):
            switch.kill()
        self.assertTrue(switch.active)
        self.assertFalse(self.repository.safety_state().killed)
        self.assertEqual(self.repository.events(), [])

    def test_missing_state_fails_closed(self):
        with self.connection:
            self.connection.execute("DELETE FROM system_state")
        with self.assertRaises(RuntimeError):
            self.assertTrue(KillSwitch(self.repository).active)
        second = connect(self.path)
        try:
            with self.assertRaises(RuntimeError):
                Repository(second).safety_state()
        finally:
            second.close()

    def test_decimal_snapshot_round_trip(self):
        snapshot = PortfolioSnapshot(
            datetime.now(UTC),
            Decimal("999.123456789123456789"),
            (),
            Decimal("999.123456789123456789"),
            Decimal("0"),
            Decimal("0"),
        )
        self.repository.save_portfolio_snapshot(snapshot)
        row = self.connection.execute("SELECT * FROM portfolio_snapshots").fetchone()
        self.assertEqual(Decimal(row["cash"]), snapshot.cash)
        self.assertEqual(json.loads(row["positions_json"]), [])

    def test_naive_timestamps_rejected(self):
        with self.assertRaises(ValueError):
            encode({"timestamp": datetime(2026, 1, 1)})

    def test_foreign_keys_enabled(self):
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_cli_separate_processes(self):
        environment = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{self.path}",
            "TRADING_MODE": "TESTNET",
            "ENABLE_LIVE_TRADING": "false",
        }
        for command, expected in (
            ("status", False),
            ("kill", True),
            ("status", True),
            ("resume", False),
            ("status", False),
        ):
            result = subprocess.run(
                [sys.executable, "-m", "trader", command],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            state = json.loads(result.stdout)
            self.assertEqual(state["safety"]["killed"], expected)
            self.assertFalse(state["execution_available"])
            self.assertEqual(json.loads(result.stderr)["severity"], "INFO")

    def test_cli_invalid_config_never_logs_secrets(self):
        secret = "sensitive-example-must-not-appear"
        environment = {**os.environ, "TRADING_MODE": secret, "BINANCE_API_SECRET": secret}
        result = subprocess.run(
            [sys.executable, "-m", "trader", "status"],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(secret, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

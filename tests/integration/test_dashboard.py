import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from tests.fixtures.risk import NOW
from tests.integration.test_experiment import averages, observations
from trader.config import AppConfig, TradingMode
from trader.dashboard import DashboardHandler, reader, records, summary
from trader.experiment import Experiment
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.repository import Repository


class DashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.control = connect(self.root / "paper.db")
        self.addCleanup(self.control.close)
        self.switch = KillSwitch(Repository(self.control))
        self.exp = Experiment(
            AppConfig(
                mode=TradingMode.PAPER, database_url="sqlite:///" + str(self.root / "paper.db")
            ),
            self.switch,
        )
        self.addCleanup(self.exp.close)

    def test_empty_and_missing_data_do_not_create_accounts(self):
        self.assertFalse(summary(self.root)["ready"])
        missing = self.root / "missing"
        self.assertFalse(summary(missing)["ready"])
        self.assertEqual(records(missing, "sma5m", "ALL", 1)["total"], 0)
        self.assertFalse(missing.exists())

    def test_summary_matches_published_snapshot_and_accounts_are_independent(self):
        published = self.exp.sample(observations(), averages(), NOW)
        data = summary(self.root)
        self.assertEqual(data["samples"], 1)
        self.assertEqual(data["capital"], "1000")
        self.assertEqual(
            data["portfolios"]["sma5m"]["equity"], published["portfolios"]["sma5m"]["equity"]
        )
        self.assertEqual(data["portfolios"]["trend1h"]["stats"]["fills"], 1)
        self.assertEqual(data["portfolios"]["cash"]["drawdown_pct"], "0")
        self.assertFalse(data["killed"])
        self.switch.kill()
        self.assertTrue(summary(self.root)["killed"])

    def test_hold_reasons_hidden_and_fill_details_present(self):
        self.exp.sample(observations(), averages(), NOW)
        filled = records(self.root, "sma5m", "FILLED", 1)
        self.assertEqual(filled["total"], 1)
        self.assertIsNotNone(filled["rows"][0]["order"]["average_fill_price"])
        self.assertNotIn("raw_response", filled["rows"][0])
        self.assertEqual(records(self.root, "sma5m", "REJECTED", 1)["total"], 1)

    def test_pagination_filters_and_newest_first(self):
        db = self.exp.engines["sma5m"].connection
        for i in range(65):
            result = {
                "symbol": "BTCUSDT",
                "action": "HOLD",
                "status": "HOLD",
                "reasons": ["BALANCE_AND_NO_SHORTING"],
                "assessment_id": 1,
            }
            db.execute(
                "INSERT INTO paper_cycles VALUES (?,?,?,?)",
                (str(i), "key", NOW.isoformat(), json.dumps(result)),
            )
        db.commit()
        first, last = records(self.root, "sma5m", "ALL", 1), records(self.root, "sma5m", "HOLD", 3)
        self.assertEqual(first["total"], 65)
        self.assertEqual(first["pages"], 3)
        self.assertEqual(len(first["rows"]), 30)
        self.assertEqual(first["rows"][0]["id"], "64")
        self.assertEqual(len(last["rows"]), 5)
        self.assertEqual(last["rows"][0]["reasons"], [])

    def test_reader_is_read_only_and_missing_file_is_not_created(self):
        db = reader(self.root / "paper.db")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM system_state")
        finally:
            db.close()
        with self.assertRaises(sqlite3.OperationalError):
            reader(self.root / "unknown.db")
        self.assertFalse((self.root / "unknown.db").exists())

    def test_bad_source_status_and_page_are_rejected(self):
        for source, status, page in (
            ("../../paper.db", "ALL", 1),
            ("sma5m", "' OR 1=1", 1),
            ("sma5m", "ALL", 0),
            ("sma5m", "ALL", 1000001),
        ):
            with self.assertRaises(ValueError):
                records(self.root, source, status, page)

    def test_events_and_http_routes(self):
        self.switch.kill()
        events = records(self.root, "events", "ALL", 1)
        self.assertEqual(events["rows"][0]["reason"], "KILL_SWITCH_ACTIVATED")
        handler = object.__new__(DashboardHandler)
        handler.root = self.root
        handler.send = MagicMock()
        for path, expected in (
            ("/", 200),
            ("/app.js", 200),
            ("/api/summary", 200),
            ("/api/records?source=events", 200),
            ("/api/records?page=no", 400),
            ("/../.env", 404),
        ):
            handler.path = path
            handler.do_GET()
            self.assertEqual(handler.send.call_args.args[0], expected)

    def test_market_prices_and_holdings_use_saved_valuations(self):
        self.exp.sample(observations(), averages(), NOW)
        data = summary(self.root)
        self.assertEqual(data["markets"]["BTCUSDT"]["price"], "50000")
        accounts = {p["strategy"]: p for p in data["holdings"]}
        self.assertEqual(set(accounts), {"sma5m", "trend1h"})
        for account in accounts.values():
            total = sum((Decimal(p["market_value"]) for p in account["positions"]), Decimal(0))
            self.assertEqual(total + Decimal(account["cash"]), Decimal(account["equity"]))
            self.assertTrue(account["timestamp"])

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from tests.fixtures.risk import NOW
from tests.integration.test_experiment import averages, observations
from trader.config import AppConfig, TradingMode
from trader.dashboard import DashboardHandler
from trader.execution.paper import PaperEngine
from trader.experiment import Experiment
from trader.export_results import write_export
from trader.local_ai import open_account
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.repository import Repository
from trader.strategy.ollama import OllamaProvider


class ExportTests(unittest.TestCase):
    def test_complete_export_and_endpoint_change_preserve_account(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AppConfig(
                mode=TradingMode.PAPER, database_url="sqlite:///" + str(root / "paper.db")
            )
            control = connect(root / "paper.db")
            self.addCleanup(control.close)
            exp = Experiment(config, KillSwitch(Repository(control)))
            self.addCleanup(exp.close)
            exp.sample(observations(), averages(), NOW)
            provider = OllamaProvider(model="qwen3.8:27b", base_url="http://private-endpoint:11434")
            db = open_account(config, provider)
            engine = PaperEngine(db, config)
            engine.initialize(NOW)
            engine.mark(observations(), NOW)
            db.execute(
                "INSERT INTO ai_calls(timestamp,provider,model,prompt,response,error) "
                "VALUES (?,?,?,?,?,?)",
                (
                    NOW.isoformat(),
                    "ollama",
                    provider.model,
                    "private-prompt",
                    '{"action":"HOLD"}',
                    None,
                ),
            )
            db.commit()
            db.close()
            changed = open_account(
                config, OllamaProvider(model=provider.model, base_url="http://new:11434")
            )
            self.assertEqual(
                changed.execute("SELECT cash FROM paper_account").fetchone()[0], "1000"
            )
            changed.close()
            output = io.BytesIO()
            manifest = write_export(root, output)
            self.assertIsNotNone(manifest["common_period"])
            self.assertEqual(manifest["files"]["ollama/ai_calls.jsonl"], 1)
            with zipfile.ZipFile(output) as archive:
                contents = b"\n".join(archive.read(name) for name in archive.namelist())
                self.assertNotIn(b"private-prompt", contents)
                self.assertNotIn(b"http://new", contents)
                self.assertNotIn(b"private-endpoint", contents)
                self.assertEqual(json.loads(archive.read("manifest.json"))["mode"], "PAPER")
                self.assertGreater(len(archive.read("comparison/samples.jsonl")), 0)
                self.assertTrue(all(not name.endswith(".db") for name in archive.namelist()))

    def test_missing_accounts_are_explicit_without_creating_databases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = write_export(root, io.BytesIO())
            self.assertIn("ollama", result["missing"])
            self.assertIsNone(result["common_period"])
            self.assertEqual(list(root.iterdir()), [])

    def test_http_download_is_valid_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            handler = object.__new__(DashboardHandler)
            handler.root = Path(temp)
            handler.path = "/api/export"
            handler.wfile = io.BytesIO()
            handler.send_response = MagicMock()
            handler.send_header = MagicMock()
            handler.end_headers = MagicMock()
            handler.do_GET()
            handler.send_response.assert_called_once_with(200)
            headers = dict(call.args for call in handler.send_header.call_args_list)
            self.assertEqual(headers["Content-Type"], "application/zip")
            self.assertEqual(int(headers["Content-Length"]), len(handler.wfile.getvalue()))
            self.assertIn("attachment", headers["Content-Disposition"])
            with zipfile.ZipFile(handler.wfile) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn("manifest.json", archive.namelist())

    def test_window_filters_rows_and_summary_uses_interval_not_inception(self):
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AppConfig(
                mode=TradingMode.PAPER, database_url="sqlite:///" + str(root / "paper.db")
            )
            db = connect(root / "paper.db")
            self.addCleanup(db.close)
            exp = Experiment(config, KillSwitch(Repository(db)))
            self.addCleanup(exp.close)
            for minutes, price in ((0, "50000"), (5, "51000"), (10, "52000")):
                now = NOW + timedelta(minutes=minutes)
                exp.sample(observations(now, price), averages(now, price), now)
            out = io.BytesIO()
            result = write_export(
                root, out, NOW + timedelta(minutes=5), NOW + timedelta(minutes=10)
            )
            self.assertEqual(result["files"]["comparison/samples.jsonl"], 2)
            with zipfile.ZipFile(out) as z:
                data = json.loads(z.read("summary.json"))
                self.assertEqual(data["portfolios"]["hold"]["observations"], 2)
                self.assertGreater(float(data["portfolios"]["hold"]["opening_equity"]), 1000)
                self.assertIn("START_HERE.md", z.namelist())
            with self.assertRaises(ValueError):
                write_export(root, io.BytesIO(), NOW, NOW)

    def test_http_rejects_naive_or_inverted_time(self):
        with tempfile.TemporaryDirectory() as temp:
            handler = object.__new__(DashboardHandler)
            handler.root = Path(temp)
            handler.send = MagicMock()
            for query in (
                "start=2026-09-20T15:00",
                "start=2026-09-20T15:00%2B08:00&end=2026-09-19T15:00%2B08:00",
            ):
                handler.path = "/api/export?" + query
                handler.do_GET()
                self.assertEqual(handler.send.call_args.args[0], 400)

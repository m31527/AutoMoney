import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures.risk import NOW
from tests.integration.test_ai import proposal
from tests.integration.test_experiment import averages, observations
from trader.config import AppConfig, TradingMode
from trader.dashboard import ai_summary, records
from trader.exchange.errors import NetworkError
from trader.local_ai import open_account, run_local_ai
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.repository import Repository
from trader.strategy.ollama import OllamaProvider
from trader.strategy.provider import ProviderError, load_provider


class OllamaProviderTests(unittest.TestCase):
    def response(self, data):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(data).encode()
        opener = MagicMock()
        opener.open.return_value = response
        return opener

    def test_native_schema_no_key_no_tools_and_nonstreaming(self):
        provider = load_provider(
            {"AI_PROVIDER": "ollama", "AI_MODEL": "qwen3.8:27b", "AI_API_KEY": "should-not-be-sent"}
        )
        self.assertIsInstance(provider, OllamaProvider)
        opener = self.response(
            {
                "done": True,
                "done_reason": "stop",
                "message": {"role": "assistant", "content": proposal()},
            }
        )
        with patch("trader.strategy.ollama.urllib.request.build_opener", return_value=opener):
            self.assertEqual(provider.complete("instructions", "context"), proposal())
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://ollama:11434/api/chat")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertFalse(payload["format"]["additionalProperties"])
        self.assertNotIn("tools", payload)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertNotIn("should-not-be-sent", request.data.decode())
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 120)

    def test_reject_truncated_missing_tools_errors_or_empty_response(self):
        valid = {
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": proposal()},
        }
        cases = [
            {**valid, "done": False},
            {**valid, "done_reason": "length"},
            {**valid, "error": "private server detail"},
            {"done": True},
            {**valid, "message": {"role": "assistant", "content": proposal(), "tool_calls": [{}]}},
            {**valid, "message": {"role": "assistant", "content": ""}},
            [],
        ]
        for data in cases:
            with (
                self.subTest(data=data),
                patch(
                    "trader.strategy.ollama.urllib.request.build_opener",
                    return_value=self.response(data),
                ),
            ):
                with self.assertRaises(ProviderError):
                    OllamaProvider("qwen3.8:27b").complete("", "")

    def test_timeout_is_sanitized(self):
        opener = MagicMock()
        opener.open.side_effect = TimeoutError("private server detail")
        with patch("trader.strategy.ollama.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(
                ProviderError, "AI_PROVIDER_UNAVAILABLE_OR_REFUSED"
            ) as error:
                OllamaProvider("qwen3.8:27b").complete("", "")
        self.assertNotIn("private", str(error.exception))

    def test_explicit_model_and_origin_validation(self):
        for url in (
            "file:///tmp/model",
            "http://name:password@host",
            "http://host/api/chat",
            "http://host?key=x",
            "http://host:bad",
            "http://host/#x",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OllamaProvider("qwen3.8:27b", url)
        for name in ("", "model with spaces", "model\n"):
            with self.assertRaises(ValueError):
                OllamaProvider(name)
        for timeout in (0, 601, True):
            with self.assertRaises(ValueError):
                OllamaProvider("qwen3.8:27b", timeout_seconds=timeout)


class OllamaWorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = AppConfig(
            mode=TradingMode.PAPER, database_url="sqlite:///" + str(self.root / "paper.db")
        )
        self.control = connect(self.root / "paper.db")
        self.addCleanup(self.control.close)
        self.switch = KillSwitch(Repository(self.control))
        self.provider = OllamaProvider("qwen3.8:27b")

    def run_worker(self, **kwargs):
        exchange = MagicMock()
        exchange.get_average_price.side_effect = lambda s: averages()[s]
        exchange.get_ticker.side_effect = lambda s: observations()[s].ticker
        outputs = []
        with (
            patch("trader.local_ai.BinanceSpotAdapter", return_value=exchange),
            patch(
                "trader.scheduler.collect_market",
                side_effect=lambda e, s, *a, **k: observations()[s],
            ),
        ):
            run_local_ai(
                self.config,
                self.switch,
                self.provider,
                cycles=1,
                emit=outputs.append,
                clock=lambda: NOW,
                sleep=lambda _: None,
                **kwargs,
            )
        return outputs

    def test_two_symbols_audit_and_ui_use_independent_account(self):
        with patch.object(OllamaProvider, "complete", return_value=proposal()):
            outputs = self.run_worker()
        self.assertEqual(len(outputs), 2)
        self.assertEqual(sum(o["status"] == "FILLED" for o in outputs), 1)
        data = ai_summary(self.root)
        self.assertEqual(data["model"], "qwen3.8:27b")
        self.assertEqual(data["calls"], 2)
        self.assertEqual(
            data["failures"], 1
        )  # BTC proposal is invalid for ETH, falls back to HOLD.
        self.assertEqual(records(self.root, "ollama", "ALL", 1)["total"], 2)
        self.assertEqual(records(self.root, "ai-calls", "ALL", 1)["total"], 2)
        self.assertEqual(self.control.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_global_kill_prevents_model_calls_and_orders(self):
        self.switch.kill()
        with patch.object(OllamaProvider, "complete") as complete:
            result = self.run_worker()
        complete.assert_not_called()
        self.assertEqual(result[0]["status"], "PAUSED")

    def test_provider_failure_holds_and_is_visible(self):
        with patch.object(OllamaProvider, "complete", side_effect=ProviderError()):
            result = self.run_worker()
        self.assertTrue(all(o["status"] == "HOLD" for o in result))
        self.assertEqual(ai_summary(self.root)["failures"], 2)
        self.assertEqual(ai_summary(self.root)["trades"], 0)

    def test_model_and_risk_are_frozen_across_restarts(self):
        db = open_account(self.config, self.provider)
        db.close()
        with self.assertRaises(ValueError):
            open_account(self.config, OllamaProvider("different:27b"))
        with self.assertRaises(ValueError):
            open_account(replace(self.config, mode=TradingMode.TESTNET), self.provider)

    def test_market_failures_are_recorded_without_calling_model(self):
        with (
            patch("trader.scheduler.collect_market", side_effect=NetworkError()),
            patch.object(OllamaProvider, "complete") as complete,
        ):
            out = []
            run_local_ai(
                self.config,
                self.switch,
                self.provider,
                cycles=1,
                emit=out.append,
                clock=lambda: NOW,
            )
        complete.assert_not_called()
        self.assertEqual(len(out), 2)
        self.assertEqual(records(self.root, "ai-events", "ALL", 1)["total"], 3)
        self.assertEqual(
            sum(r["status"] == "ERROR" for r in records(self.root, "ai-events", "ALL", 1)["rows"]),
            2,
        )

    def test_prefilter_flag_is_explicit_visible_and_reversible(self):
        for enabled in ("true", "false"):
            with (
                patch.dict("os.environ", {"AI_PREFILTER_ENABLED": enabled}),
                patch.object(OllamaProvider, "complete", return_value=proposal()),
            ):
                self.run_worker()
            self.assertEqual(ai_summary(self.root)["prefilter_enabled"], enabled == "true")
        with (
            patch.dict("os.environ", {"AI_PREFILTER_ENABLED": "tru"}),
            self.assertRaises(ValueError),
        ):
            self.run_worker()

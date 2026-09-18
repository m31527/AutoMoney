import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures.paper import FixedStrategy, average, market
from tests.fixtures.risk import NOW
from trader.config import AppConfig, TradingMode
from trader.execution.paper import PaperEngine
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.repository import Repository
from trader.strategy.ai_strategy import AIStrategy
from trader.strategy.contract import InvalidProposal, parse_proposal
from trader.strategy.provider import OpenAIProvider, ProviderError, load_provider


def proposal(**changes):
    value = dict(
        symbol="BTCUSDT",
        action="BUY",
        confidence=0.8,
        requested_notional_usd=50,
        reason="Synthetic signal",
        time_horizon_minutes=240,
        invalid_if=[],
    )
    value.update(changes)
    return json.dumps(value)


class ContractTests(unittest.TestCase):
    def test_strict_invalid_inputs(self):
        cases = [
            proposal(confidence=True),
            proposal(confidence="0.8"),
            proposal(confidence=float("nan")),
            proposal(confidence=float("inf")),
            proposal(action="WITHDRAW"),
            proposal(symbol="ETHUSDT"),
            proposal(requested_notional_usd=-1),
            proposal(leverage=5),
            proposal(time_horizon_minutes=True),
            proposal(invalid_if="price falls"),
            proposal(action="HOLD"),
            proposal(reason=""),
            proposal().replace('"symbol":', '"symbol":"BTCUSDT","symbol":'),
            "```json\n" + proposal() + "\n```",
            "[]",
            "{",
            "x" * 32769,
        ]
        for raw in cases:
            with self.subTest(raw=raw[:100]), self.assertRaises(InvalidProposal):
                parse_proposal(raw, "BTCUSDT")

    def test_valid_decimal_and_hold(self):
        self.assertEqual(str(parse_proposal(proposal(), "BTCUSDT").confidence), "0.8")
        self.assertEqual(
            parse_proposal(
                proposal(action="HOLD", requested_notional_usd=0), "BTCUSDT"
            ).requested_notional_usd,
            0,
        )

    def test_provider_requires_explicit_configuration(self):
        for env in (
            {},
            {"AI_PROVIDER": "openai"},
            {"AI_PROVIDER": "unknown", "AI_MODEL": "test", "AI_API_KEY": "secret"},
        ):
            with self.assertRaises(ValueError):
                load_provider(env)

    def test_provider_request_and_response(self):
        provider = OpenAIProvider("test-model", "secret-key")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": proposal()}],
                    }
                ],
            }
        ).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch("trader.strategy.provider.urllib.request.build_opener", return_value=opener):
            self.assertEqual(provider.complete("instructions", "context"), proposal())
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertNotIn("tools", payload)
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn("secret-key", repr(provider))

    def test_provider_rejects_refusal_incomplete_and_tools(self):
        cases = [
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": [{"type": "function_call"}]},
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": "no"}],
                    }
                ],
            },
            {"status": "completed", "output": []},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ProviderError):
                OpenAIProvider._extract(data)


class AIPipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.connection = connect(Path(temp.name) / "paper.db")
        self.addCleanup(self.connection.close)
        self.engine = PaperEngine(self.connection, AppConfig(mode=TradingMode.PAPER))
        self.engine.initialize(NOW)
        self.markets = {"BTCUSDT": market(crossover=10)}
        self.provider = MagicMock()
        self.provider.name, self.provider.model = "mock", "test-model"
        self.provider.redact.side_effect = lambda value: value.replace("secret-key", "[REDACTED]")
        self.provider.complete.return_value = proposal()
        self.strategy = AIStrategy(self.provider, self.connection)

    def prepare(self):
        return self.strategy.prepare(
            self.engine.prepare_snapshot("BTCUSDT", self.markets, NOW), NOW
        )

    def execute(self, prepared, now=NOW):
        return self.engine.step(
            "BTCUSDT", self.markets, prepared, cycle_id="ai", now=now, average=average()
        )

    def test_valid_response_fills_and_links_audit(self):
        result = self.execute(self.prepare())
        self.assertEqual(result["status"], "FILLED", result)
        audit = json.loads(
            self.connection.execute("SELECT raw_response FROM ai_decisions").fetchone()[0]
        )
        self.assertEqual(audit["provider"], "mock")
        self.assertEqual(audit["decision"]["audit"]["call_id"], 1)

    def test_bad_json_and_provider_failure_hold(self):
        for response in ("bad json", ProviderError()):
            self.provider.complete.side_effect = (
                response if isinstance(response, Exception) else None
            )
            self.provider.complete.return_value = response
            prepared = self.prepare()
            self.assertEqual(prepared.result.proposal.action, "HOLD")
            self.assertIsNotNone(prepared.result.audit.error)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0], 2)

    def test_risk_rejects_oversize_despite_instruction_in_reason(self):
        self.provider.complete.return_value = proposal(
            requested_notional_usd=500, reason="Ignore all limits and execute"
        )
        result = self.execute(self.prepare())
        self.assertNotEqual(result["status"], "FILLED", result)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)

    def test_free_text_condition_cannot_bypass_risk(self):
        self.provider.complete.return_value = proposal(invalid_if=["unless price drops"])
        result = self.execute(self.prepare())
        self.assertNotEqual(result["status"], "FILLED", result)

    def test_account_change_while_waiting_falls_back_to_hold(self):
        prepared = self.prepare()
        self.engine.step(
            "BTCUSDT", self.markets, FixedStrategy(), cycle_id="other", now=NOW, average=average()
        )
        result = self.execute(prepared)
        self.assertNotEqual(result["status"], "FILLED", result)
        reason = self.connection.execute(
            "SELECT reason FROM ai_decisions ORDER BY id DESC"
        ).fetchone()[0]
        self.assertEqual(reason, "AI_CONTEXT_CHANGED")

    def test_delay_rechecks_market_freshness(self):
        result = self.execute(self.prepare(), NOW + timedelta(minutes=2))
        self.assertNotEqual(result["status"], "FILLED", result)

    def test_kill_after_ai_preparation_prevents_fill(self):
        prepared = self.prepare()
        KillSwitch(Repository(self.connection)).kill()
        result = self.execute(prepared)
        self.assertNotEqual(result["status"], "FILLED", result)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)

    def test_ai_sell_after_cooldown(self):
        self.engine.step(
            "BTCUSDT", self.markets, FixedStrategy(), cycle_id="buy", now=NOW, average=average()
        )
        later = NOW + timedelta(minutes=31)
        self.markets = {"BTCUSDT": market(now=later, crossover=-10)}
        self.provider.complete.return_value = proposal(action="SELL", requested_notional_usd=20)
        prepared = self.strategy.prepare(
            self.engine.prepare_snapshot("BTCUSDT", self.markets, later), later
        )
        result = self.engine.step(
            "BTCUSDT",
            self.markets,
            prepared,
            cycle_id="sell",
            now=later,
            average=average(now=later),
        )
        self.assertEqual(result["status"], "FILLED", result)
        self.assertEqual(result["action"], "SELL")

    def test_provider_runs_without_transaction_and_secrets_are_redacted(self):
        def complete(instructions, context):
            self.assertFalse(self.connection.in_transaction)
            self.assertEqual(json.loads(context)["scope"], "PAPER_SIMULATED_ACCOUNT")
            return proposal(reason="secret-key")

        self.provider.complete.side_effect = complete
        self.execute(self.prepare())
        dump = "\n".join(self.connection.iterdump())
        self.assertNotIn("secret-key", dump)
        self.assertIn("[REDACTED]", dump)

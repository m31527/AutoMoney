import json
import os
import unittest
from unittest.mock import MagicMock, patch

from tests.integration import test_shadow
from trader.strategy.openteddy import OpenTeddyProvider
from trader.strategy.provider import ProviderError


class OpenTeddyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(
            os.environ,
            OPENTEDDY_MODEL="test:1",
            OPENTEDDY_PROVIDER="ollama",
            OPENTEDDY_URL="http://localhost:8000",
            OPENTEDDY_TOKEN="x" * 64,
        )
        env.start()
        self.addCleanup(env.stop)
        self.provider = OpenTeddyProvider()

    def test_metadata_and_no_token_in_result(self):
        result = dict(
            provider="ollama",
            requested_model="test:1",
            model="test:1",
            proposal={"action": "HOLD"},
            request_id="123",
            usage={"input_tokens": 20},
            latency_seconds=1,
        )
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
            result
        ).encode()
        with patch("trader.strategy.openteddy.urllib.request.build_opener", return_value=opener):
            self.assertEqual(
                json.loads(self.provider.complete("instructions", "{}")), {"action": "HOLD"}
            )
        self.assertEqual(self.provider.last_metadata["usage"], {"input_tokens": 20})
        self.assertNotIn("x" * 64, json.dumps(self.provider.last_metadata))

    def test_no_retry_or_fallback(self):
        opener = MagicMock()
        opener.open.side_effect = TimeoutError()
        with patch("trader.strategy.openteddy.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError):
                self.provider.complete("instructions", "{}")
        opener.open.assert_called_once()


class ShadowRoutingTests(unittest.TestCase):
    setUp = test_shadow.ShadowTests.setUp
    run_shadow = test_shadow.ShadowTests.run_shadow

    def test_override_only_shadow_and_never_creates_orders(self):
        from trader.scheduler import run_paper

        remote = MagicMock()
        remote.name, remote.model = "openteddy", "test:1"
        remote.complete.return_value = self.provider.complete.return_value
        remote.redact.side_effect = lambda s: s
        remote.last_metadata = {"provider": "ollama", "usage": {"input_tokens": 12}}

        def route(*args, **kwargs):
            return run_paper(*args, shadow_provider=remote, **kwargs)

        with patch("tests.integration.test_shadow.run_paper", side_effect=route):
            self.run_shadow()
        self.provider.complete.assert_not_called()
        remote.complete.assert_called_once()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        row = self.connection.execute(
            "SELECT payload_json FROM system_events WHERE event_type='AI_SHADOW_COMPLETED'"
        ).fetchone()
        self.assertEqual(json.loads(row[0])["provider_metadata"]["usage"]["input_tokens"], 12)

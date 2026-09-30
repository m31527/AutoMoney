import json
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from tests.fixtures.paper import average, market
from tests.fixtures.risk import NOW
from tests.integration import test_ai as ai_tests
from trader.config import RiskConfig
from trader.scheduler import run_paper
from trader.shadow import summarize
from trader.strategy.provider import ProviderError


class ShadowTests(unittest.TestCase):
    setUp = ai_tests.AIPipelineTests.setUp

    def run_shadow(self, paired=False):
        from unittest.mock import MagicMock

        self.engine.config = replace(self.engine.config, risk=RiskConfig(symbols=("BTCUSDT",)))
        exchange = MagicMock()
        exchange.get_average_price.return_value = average()
        with (
            patch("trader.scheduler.collect_market", return_value=market()),
            patch(
                "trader.scheduler.evaluate",
                return_value={
                    "skipped": True,
                    "policy_version": "test",
                    "reason": "COST",
                },
            ),
        ):
            run_paper(
                self.engine,
                exchange,
                self.strategy,
                "BTCUSDT",
                ai_prefilter=True,
                ai_shadow_interval=3600 if paired else 1800,
                ai_shadow_paired=paired,
                emit=lambda result: None,
                clock=lambda: NOW,
            )

    def test_shadow_buy_does_not_trade_and_restart_respects_reservation(self):
        self.provider.complete.return_value = ai_tests.proposal(confidence=0.8)
        before = self.engine.status()
        self.run_shadow()
        self.run_shadow()
        self.provider.complete.assert_called_once()
        self.assertEqual(self.engine.status()["positions"], before["positions"])
        self.assertEqual(self.engine.status()["cash"], before["cash"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        events = self.connection.execute(
            "SELECT payload_json FROM system_events WHERE event_type='AI_SHADOW_COMPLETED'"
        ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertFalse(json.loads(events[0][0])["executable"])

    def test_failed_call_is_recorded_and_rate_limited(self):
        self.provider.complete.side_effect = ProviderError()
        self.run_shadow()
        self.run_shadow()
        self.provider.complete.assert_called_once()
        row = self.connection.execute(
            "SELECT payload_json FROM system_events WHERE event_type='AI_SHADOW_COMPLETED'"
        ).fetchone()
        self.assertEqual(json.loads(row[0])["status"], "ERROR")

    def test_forward_anchor_is_after_response(self):
        def quote(minutes, price):
            return {
                "event_type": "AI_PREFILTER_EVALUATED",
                "payload": {
                    "symbol": "BTCUSDT",
                    "quote_timestamp": (NOW + timedelta(minutes=minutes)).isoformat(),
                    "reference_price": str(price),
                    "estimated_round_trip_cost_bps": "40",
                },
            }

        done = {
            "event_type": "AI_SHADOW_COMPLETED",
            "payload": {
                "call_id": "x",
                "symbol": "BTCUSDT",
                "completed_at": (NOW + timedelta(minutes=2)).isoformat(),
                "status": "ERROR",
                "inference_seconds": 120,
            },
        }
        result = summarize([done, quote(0, 50), quote(5, 100), quote(65, 101)])
        forward = result["results"][0]["post_response_forward"]
        self.assertEqual(forward["1h"]["buy_minus_estimated_cost_bps"], "60.00")
        self.assertIsNone(forward["4h"])

    def test_paired_same_market_removes_only_proxy_and_never_trades(self):
        self.provider.complete.side_effect = [
            ai_tests.proposal(action="HOLD", requested_notional_usd=0),
            ai_tests.proposal(),
        ]
        self.run_shadow(paired=True)
        self.run_shadow(paired=True)
        self.assertEqual(self.provider.complete.call_count, 2)
        first, second = self.provider.complete.call_args_list
        control = json.loads(first.args[1])
        treatment = json.loads(second.args[1])
        self.assertIn("deterministic_edge_proxy_bps", control)
        self.assertNotIn("deterministic_edge_proxy_bps", treatment)
        self.assertEqual(control["snapshot"], treatment["snapshot"])
        self.assertEqual(control["trade_budget"], treatment["trade_budget"])
        events = [
            {"event_type": row[0], "payload": json.loads(row[1])}
            for row in self.connection.execute("SELECT event_type,payload_json FROM system_events")
        ]
        stats = summarize(events)
        self.assertEqual(stats["prompt_comparison"]["complete_pairs"], 1)
        self.assertEqual(stats["prompt_comparison"]["disagreeing_pairs"], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

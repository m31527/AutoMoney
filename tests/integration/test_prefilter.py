import io
import json
import unittest
import zipfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures.paper import FixedStrategy, average, market
from tests.fixtures.risk import NOW
from tests.integration import test_ai as ai_tests
from trader.config import RiskConfig
from trader.export_results import write_export
from trader.scheduler import run_paper
from trader.strategy.prefilter import evaluate


class PrefilterTests(unittest.TestCase):
    setUp = ai_tests.AIPipelineTests.setUp

    def run_cycle(self, *, enabled=True, crossover=0):
        markets = {
            "BTCUSDT": market(crossover=crossover),
            "ETHUSDT": market("ETHUSDT", "3000"),
        }
        exchange = MagicMock()
        exchange.get_average_price.return_value = average()
        exchange.get_ticker.side_effect = lambda s: markets[s].ticker
        output = []
        with patch(
            "trader.scheduler.collect_market", side_effect=lambda e, s, *a, **kw: markets[s]
        ):
            run_paper(
                self.engine,
                exchange,
                self.strategy,
                "BTCUSDT",
                ai_prefilter=enabled,
                emit=output.append,
                clock=lambda: NOW,
            )
        return output[0]

    def test_flat_blocked_skips_model_and_exports_separate_counts(self):
        result = self.run_cycle()
        self.provider.complete.assert_not_called()
        self.assertEqual(result["status"], "HOLD")
        self.assertEqual(result["strategy_reason"], "AI_PREFILTER_COST_BLOCKED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
        self.assertEqual(result["portfolio"]["equity"], "1000")
        root = Path(self.connection.execute("PRAGMA database_list").fetchone()[2]).parent
        out = io.BytesIO()
        write_export(root, out)
        with zipfile.ZipFile(out) as z:
            summary = json.loads(z.read("summary.json"))["activity"]["legacy"]
            self.assertEqual(summary["ai_prefilter"]["skipped_model_calls"], 1)
            self.assertEqual(summary["ai_prefilter"]["skip_pct"], 100)
            self.assertEqual(summary["calls"], 0)
            self.assertEqual(summary["call_failures"], 0)
            event = next(
                json.loads(line)
                for line in z.read("legacy/events.jsonl").splitlines()
                if json.loads(line)["event_type"] == "AI_PREFILTER_EVALUATED"
            )
            self.assertEqual(event["payload"]["cycle_id"], result["cycle_id"])

    def test_disabled_preserves_model_call(self):
        self.run_cycle(enabled=False)
        self.provider.complete.assert_called_once()
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM system_events WHERE event_type='AI_PREFILTER_EVALUATED'"
            ).fetchone()[0],
            0,
        )

    def test_eligible_flat_call_still_uses_final_risk(self):
        self.provider.complete.return_value = ai_tests.proposal(confidence=0.62)
        result = self.run_cycle(crossover=10)
        self.provider.complete.assert_called_once()
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("CONFIDENCE", result["reasons"])

    def test_any_holding_keeps_model_evaluation_including_other_symbol_and_dust(self):
        self.connection.execute("INSERT INTO paper_positions VALUES ('ETHUSDT','0.000001','0.003')")
        self.connection.commit()
        self.run_cycle()
        self.provider.complete.assert_called_once()

    def test_existing_target_position_keeps_sell_evaluation(self):
        self.engine.step(
            "BTCUSDT",
            {"BTCUSDT": market()},
            FixedStrategy(),
            cycle_id="buy",
            now=NOW,
            average=average(),
        )
        self.provider.complete.return_value = ai_tests.proposal(
            action="SELL", requested_notional_usd=20
        )
        result = self.run_cycle()
        self.provider.complete.assert_called_once()
        self.assertEqual(result["action"], "SELL")
        self.assertIn("COOLDOWN", result["reasons"])

    def test_exact_cost_threshold_is_not_skipped_but_less_edge_is(self):
        snap = self.engine.prepare_snapshot("BTCUSDT", self.markets, NOW)
        cfg = RiskConfig()
        stats = evaluate(snap, cfg, NOW, account_flat=True)
        threshold = Decimal(stats["signal_proxy_bps"]) - Decimal(
            stats["estimated_round_trip_cost_bps"]
        )
        self.assertGreater(threshold, 0)
        at = replace(cfg, minimum_net_edge_bps=threshold)
        self.assertFalse(evaluate(snap, at, NOW, account_flat=True)["skipped"])
        above = replace(cfg, minimum_net_edge_bps=threshold + Decimal("0.000001"))
        self.assertTrue(evaluate(snap, above, NOW, account_flat=True)["skipped"])
        wide = replace(snap, bid=snap.last_price * Decimal("0.99"))
        self.assertTrue(evaluate(wide, cfg, NOW, account_flat=True)["skipped"])

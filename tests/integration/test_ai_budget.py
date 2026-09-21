import io
import json
import unittest
import zipfile
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures.paper import average, market
from tests.fixtures.risk import NOW
from tests.integration import test_ai as ai_tests
from trader.config import RiskConfig
from trader.exchange.errors import NetworkError
from trader.export_results import write_export
from trader.scheduler import run_paper
from trader.strategy.budget import trade_budget


class BudgetAndRefreshTests(unittest.TestCase):
    setUp = ai_tests.AIPipelineTests.setUp
    prepare = ai_tests.AIPipelineTests.prepare
    execute = ai_tests.AIPipelineTests.execute

    def diagnostics(self):
        return json.loads(
            self.connection.execute(
                "SELECT diagnostics_json FROM ai_call_diagnostics ORDER BY call_id DESC LIMIT 1"
            ).fetchone()[0]
        )

    def test_prompt_budget_and_oversized_suggestion_are_auditable(self):
        self.provider.complete.return_value = ai_tests.proposal(requested_notional_usd=1000)
        result = self.execute(self.prepare())
        context = json.loads(self.provider.complete.call_args.args[1])
        self.assertEqual(Decimal(context["trade_budget"]["max_buy_notional_usd"]), 100)
        self.assertEqual(result["strategy_reason"], "AI_BUDGET_EXCEEDED")
        self.assertEqual(result["status"], "HOLD")
        data = self.diagnostics()
        self.assertEqual(data["requested_notional_usd"], "1000")
        self.assertTrue(data["budget_exceeded"])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM fills").fetchone()[0], 0)

    def test_budget_reserves_fee_and_other_symbol_exposure(self):
        snapshot = self.engine.prepare_snapshot("BTCUSDT", self.markets, NOW)
        b = trade_budget(
            replace(snapshot, available_quote_balance=Decimal("10")), RiskConfig(), Decimal(0)
        )
        self.assertLess(b.max_buy_notional_usd, 10)
        self.assertLessEqual(b.max_buy_notional_usd * Decimal("1.001"), 10)
        b = trade_budget(snapshot, RiskConfig(), Decimal("299"))
        self.assertEqual(b.max_buy_notional_usd, 1)
        b = trade_budget(snapshot, RiskConfig(), Decimal("301"))
        self.assertEqual(b.max_buy_notional_usd, 0)

    def test_allocation_budget_matches_adverse_equity_formula(self):
        snapshot = self.engine.prepare_snapshot("BTCUSDT", self.markets, NOW)
        snapshot = replace(
            snapshot, position=Decimal("0.0039"), available_quote_balance=Decimal("805")
        )
        cfg = RiskConfig()
        b = trade_budget(snapshot, cfg, Decimal("195"))
        self.assertLess(b.max_buy_notional_usd, 5)
        upper = snapshot.ask * (1 + cfg.estimated_slippage_rate)
        amount = b.max_buy_notional_usd
        equity_after = Decimal(1000) - amount * (
            (upper - snapshot.last_price) / upper + cfg.estimated_fee_rate
        )
        self.assertLessEqual(Decimal(195) + amount, equity_after * Decimal("0.2"))

    def run_refresh(self, *, price="50001", seconds=40, fail=False):
        self.engine.config = replace(self.engine.config, risk=RiskConfig(symbols=("BTCUSDT",)))
        current = [NOW]

        def complete(*args):
            current[0] = NOW + timedelta(seconds=seconds)
            return ai_tests.proposal()

        self.provider.complete.side_effect = complete
        exchange = MagicMock()
        exchange.get_average_price.side_effect = lambda s: average(price=price, now=current[0])
        if fail:
            exchange.get_ticker.side_effect = NetworkError()
        else:
            exchange.get_ticker.side_effect = lambda s: market(price=price, now=current[0]).ticker
        output = []
        with patch("trader.scheduler.collect_market", return_value=self.markets["BTCUSDT"]):
            run_paper(
                self.engine,
                exchange,
                self.strategy,
                "BTCUSDT",
                emit=output.append,
                clock=lambda: current[0],
            )
        return output[0]

    def test_fill_uses_refreshed_quote_within_drift_bound(self):
        result = self.run_refresh()
        self.assertEqual(result["status"], "FILLED", result)
        price = Decimal(self.connection.execute("SELECT price FROM fills").fetchone()[0])
        self.assertEqual(price, Decimal("50001.1") * Decimal("1.001"))
        self.assertEqual(self.diagnostics()["quote_recheck"], "ACCEPTED")

    def test_large_move_abandons_signal(self):
        result = self.run_refresh(price="50100")
        self.assertEqual(result["strategy_reason"], "AI_PRICE_MOVED")
        self.assertEqual(result["status"], "HOLD")

    def test_fresh_quote_does_not_extend_original_signal_lifetime(self):
        result = self.run_refresh(seconds=61)
        self.assertEqual(result["strategy_reason"], "AI_SIGNAL_EXPIRED")
        self.assertEqual(self.diagnostics()["quote_recheck"], "AI_SIGNAL_EXPIRED")

    def test_refresh_failure_is_recorded_and_never_fills(self):
        with self.assertRaises(NetworkError):
            self.run_refresh(fail=True)
        self.assertEqual(self.diagnostics()["quote_recheck"], "QUOTE_FETCH_FAILED")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM fills").fetchone()[0], 0)

    def test_export_has_diagnostics_and_supports_pre_migration_database(self):
        self.run_refresh(seconds=61)
        root = Path(self.connection.execute("PRAGMA database_list").fetchone()[2]).parent
        output = io.BytesIO()
        write_export(root, output)
        with zipfile.ZipFile(output) as archive:
            summary = json.loads(archive.read("summary.json"))
            stats = summary["activity"]["legacy"]["ai_diagnostics"]
            self.assertEqual(stats["observations"], 1)
            self.assertEqual(stats["original_signal_expired_pct"], 100)
            self.assertEqual(stats["quote_rechecks"], {"AI_SIGNAL_EXPIRED": 1})
            self.assertGreaterEqual(stats["inference_seconds_p95"], 0)
            diagnostic = json.loads(archive.read("legacy/ai_diagnostics.jsonl"))
            self.assertEqual(len(diagnostic["diagnostics"]["instructions_sha256"]), 64)
        self.connection.execute("DROP TABLE ai_call_diagnostics")
        self.connection.execute("PRAGMA user_version=5")
        self.connection.commit()
        output = io.BytesIO()
        write_export(root, output)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn("legacy/ai_diagnostics.jsonl", archive.namelist())
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 5)

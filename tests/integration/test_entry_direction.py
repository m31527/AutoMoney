import io
import json
import unittest
import zipfile
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures.paper import FixedStrategy, average, market
from tests.fixtures.risk import NOW
from tests.integration import test_ai as ai_tests
from trader.config import RiskConfig
from trader.direction_study import study
from trader.export_results import write_export
from trader.models import Candle
from trader.scheduler import run_paper
from trader.strategy.prefilter import evaluate, hourly_direction


def candles(direction):
    return tuple(
        Candle(
            NOW - timedelta(hours=20 - i),
            Decimal(100),
            Decimal(120),
            Decimal(80),
            Decimal(100 if i < 15 else 100 + direction * 10),
            Decimal(1),
        )
        for i in range(20)
    )


class EntryDirectionTests(unittest.TestCase):
    setUp = ai_tests.AIPipelineTests.setUp

    def snapshot(self, direction):
        m = replace(market(crossover=10), candles_1h=candles(direction))
        self.markets = {"BTCUSDT": m}
        return self.engine.prepare_snapshot("BTCUSDT", self.markets, NOW)

    def test_direction_and_shadow_agree_on_same_input(self):
        for direction, state in ((1, "UP"), (-1, "DOWN"), (0, "FLAT")):
            with self.subTest(state=state):
                snapshot = self.snapshot(direction)
                cost = evaluate(snapshot, RiskConfig(), NOW, account_flat=True)
                new = evaluate(
                    snapshot, RiskConfig(), NOW, account_flat=True, direction_filter=True
                )
                self.assertEqual(new["hourly_direction"]["state"], state)
                self.assertFalse(cost["skipped"])
                self.assertEqual(new["skipped"], direction != 1)
                self.assertEqual(cost["cost_and_direction_would_skip"], new["skipped"])
                self.assertFalse(
                    evaluate(
                        snapshot, RiskConfig(), NOW, account_flat=False, direction_filter=True
                    )["skipped"]
                )

    def test_no_forming_or_future_candle_lookahead(self):
        snapshot = self.snapshot(-1)
        fake = replace(snapshot.candles_1h[-1], timestamp=NOW, close=Decimal("999999"))
        self.assertEqual(
            hourly_direction(replace(snapshot, candles_1h=snapshot.candles_1h + (fake,)), NOW)[
                "state"
            ],
            "DOWN",
        )

    def test_missing_stale_gap_and_nonfinite_candles_are_unavailable(self):
        snapshot = self.snapshot(1)
        for values in (
            snapshot.candles_1h[:-1],
            tuple(
                replace(c, timestamp=c.timestamp - timedelta(hours=3)) for c in snapshot.candles_1h
            ),
            snapshot.candles_1h[:10]
            + (replace(snapshot.candles_1h[10], timestamp=NOW - timedelta(hours=11)),)
            + snapshot.candles_1h[11:],
            snapshot.candles_1h[:-1] + (replace(snapshot.candles_1h[-1], close=Decimal("NaN")),),
        ):
            with self.subTest(values=values[-1]):
                snap = replace(snapshot, candles_1h=values)
                r = evaluate(snap, RiskConfig(), NOW, account_flat=True, direction_filter=True)
                self.assertEqual(r["reason"], "AI_PREFILTER_DIRECTION_UNAVAILABLE")

    def test_flat_downtrend_never_calls_model_and_export_contains_direction(self):
        self.snapshot(-1)
        self.engine.config = replace(self.engine.config, risk=RiskConfig(symbols=("BTCUSDT",)))
        exchange = MagicMock()
        exchange.get_average_price.return_value = average()
        results = []
        with patch("trader.scheduler.collect_market", return_value=self.markets["BTCUSDT"]):
            run_paper(
                self.engine,
                exchange,
                self.strategy,
                "BTCUSDT",
                ai_prefilter=True,
                ai_direction_filter=True,
                emit=results.append,
                clock=lambda: NOW,
            )
        self.provider.complete.assert_not_called()
        self.assertEqual(results[0]["strategy_reason"], "AI_PREFILTER_DIRECTION_BLOCKED")
        root = Path(self.connection.execute("PRAGMA database_list").fetchone()[2]).parent
        out = io.BytesIO()
        write_export(root, out)
        with zipfile.ZipFile(out) as z:
            result = json.loads(z.read("summary.json"))["activity"]["legacy"]["direction_study"]
            self.assertEqual(result["extra_direction_skips"], 1)
            self.assertEqual(result["cohorts"]["DOWN"]["forward_prices"]["1h"]["matched"], 0)

    def test_model_cannot_buy_downtrend_even_with_existing_position(self):
        self.engine.step(
            "BTCUSDT",
            {"BTCUSDT": market()},
            FixedStrategy(),
            cycle_id="seed",
            now=NOW,
            average=average(),
        )
        snapshot = self.snapshot(-1)
        prepared = self.strategy.prepare(
            snapshot,
            NOW,
            budget=self.engine.ai_budget(snapshot, self.markets),
            require_uptrend=True,
        )
        self.assertEqual(prepared.result.proposal.reason, "AI_ENTRY_DIRECTION_BLOCKED")
        context = json.loads(self.provider.complete.call_args.args[1])
        self.assertEqual(context["entry_direction"]["state"], "DOWN")
        self.assertIn("ai-entry-direction-v3", prepared.name)
        self.provider.complete.return_value = ai_tests.proposal(
            action="SELL", requested_notional_usd=20
        )
        prepared = self.strategy.prepare(
            snapshot,
            NOW,
            budget=self.engine.ai_budget(snapshot, self.markets),
            require_uptrend=True,
        )
        self.assertEqual(prepared.result.proposal.action, "SELL")

    def test_uptrend_still_requires_confidence_and_original_risk(self):
        snapshot = self.snapshot(1)
        self.provider.complete.return_value = ai_tests.proposal(confidence=0.62)
        prepared = self.strategy.prepare(
            snapshot,
            NOW,
            budget=self.engine.ai_budget(snapshot, self.markets),
            require_uptrend=True,
        )
        result = self.engine.step(
            "BTCUSDT", self.markets, prepared, cycle_id="entry", now=NOW, average=average()
        )
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("CONFIDENCE", result["reasons"])
        self.provider.complete.return_value = ai_tests.proposal(confidence=0.8)
        prepared = self.strategy.prepare(
            snapshot,
            NOW,
            budget=self.engine.ai_budget(snapshot, self.markets),
            require_uptrend=True,
        )
        approved = self.engine.step(
            "BTCUSDT", self.markets, prepared, cycle_id="valid-entry", now=NOW, average=average()
        )
        self.assertEqual(approved["status"], "FILLED")

    def test_forward_prices_do_not_use_earlier_or_distant_future_quotes(self):
        p = evaluate(self.snapshot(1), RiskConfig(), NOW, account_flat=True, direction_filter=True)
        p["reference_price"] = "100"

        def event(minutes, price):
            return {
                "timestamp": (NOW + timedelta(minutes=minutes)).isoformat(),
                "payload": {
                    **p,
                    "quote_timestamp": (NOW + timedelta(minutes=minutes)).isoformat(),
                    "reference_price": str(price),
                },
            }

        result = study([event(0, 100), event(59, 200), event(65, 110), event(251, 150)])
        horizon = result["cohorts"]["UP"]["forward_prices"]["1h"]
        self.assertEqual(horizon["matched"], 1)
        self.assertEqual(Decimal(horizon["mean_price_return_pct"]), 10)
        self.assertEqual(result["cohorts"]["UP"]["forward_prices"]["4h"]["matched"], 0)
        self.assertEqual(study([{"payload": {}}])["observations_with_direction"], 0)

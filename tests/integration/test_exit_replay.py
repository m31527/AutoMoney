import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.fixtures.paper import FixedStrategy, average, market
from tests.fixtures.risk import NOW
from trader.config import AppConfig, TradingMode
from trader.execution.paper import PaperEngine
from trader.export_results import write_export
from trader.models import Action
from trader.replay_exits import replay_assessment
from trader.storage.db import connect
from trader.strategy.baseline import SMAStrategy


class ExitReplayTests(unittest.TestCase):
    def make_export(self, *, seconds=3600, stale=False, wide=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = connect(root / "paper.db")
            self.addCleanup(db.close)
            engine = PaperEngine(db, AppConfig(mode=TradingMode.PAPER))
            engine.initialize(NOW)
            engine.step(
                "BTCUSDT",
                {"BTCUSDT": market()},
                FixedStrategy(),
                cycle_id="buy",
                now=NOW,
                average=average(),
            )
            now = NOW + timedelta(seconds=seconds)
            m = market(now=NOW if stale else now)
            if wide:
                m = replace(m, ticker=replace(m.ticker, bid=Decimal("48000")))
            decision = FixedStrategy(action=Action.SELL, notional=Decimal("49")).propose(
                engine.prepare_snapshot("BTCUSDT", {"BTCUSDT": m}, now), now
            )
            with patch.object(
                SMAStrategy,
                "propose",
                return_value=replace(decision, expected_edge_bps=Decimal("0.1")),
            ):
                engine.step(
                    "BTCUSDT",
                    {"BTCUSDT": m},
                    SMAStrategy(),
                    cycle_id="sell",
                    now=now,
                    average=average(now=now),
                )
            before = "\n".join(db.iterdump())
            out = io.BytesIO()
            write_export(root, out)
            self.assertEqual("\n".join(db.iterdump()), before)
            with zipfile.ZipFile(out) as z:
                result = json.loads(z.read("exit_policy_replay.json"))
                risk = json.loads(z.read("legacy/risk.jsonl").splitlines()[-1])
            return result["books"]["legacy"], risk

    def test_same_holding_exposes_exit_gate_without_claiming_fills(self):
        result, _ = self.make_export()
        self.assertEqual(result["sell_decisions"], 1)
        self.assertEqual(result["v1_approved"], 0)
        self.assertEqual(result["v2_approved"], 1)
        self.assertNotIn("profit", result)
        self.assertNotIn("fills", result)

    def test_replay_preserves_freshness_cooldown_and_cost_cap(self):
        for args, expected in (
            ({"seconds": 60}, "COOLDOWN"),
            ({"stale": True}, "MARKET_DATA_FRESH_VALID"),
            ({"wide": True}, "FEES_SLIPPAGE_AND_STRATEGY_EDGE"),
        ):
            with self.subTest(args=args):
                result, _ = self.make_export(**args)
                self.assertEqual(result["v2_approved"], 0)
                self.assertIn(expected, result["v2_rejection_reasons"])

    def test_ai_partial_sell_is_not_expanded_and_kill_remains_active(self):
        _, risk = self.make_export()
        for c in risk["result"]["checks"]:
            if c["rule"] == "KILL_SWITCH_CLEAR":
                c["passed"] = False
        replay = replay_assessment(risk, "ai:ollama:test")
        for outcome in replay["policies"].values():
            self.assertEqual(outcome["requested_notional_usd"], Decimal("49"))
            self.assertFalse(outcome["approved"])
            self.assertIn("KILL_SWITCH_CLEAR", outcome["reasons"])

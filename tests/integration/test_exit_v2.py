import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.fixtures.paper import FixedStrategy, average, market
from tests.fixtures.risk import NOW
from trader.config import AppConfig, RiskConfig, TradingMode
from trader.execution.paper import PaperEngine
from trader.models import Action
from trader.storage.db import connect
from trader.strategy.baseline import SMAStrategy


class ExitPolicyTests(unittest.TestCase):
    def run_exit(self, version, *, stale=False, wide=False):
        with tempfile.TemporaryDirectory() as temp:
            db = connect(Path(temp) / "paper.db")
            self.addCleanup(db.close)
            cfg = AppConfig(mode=TradingMode.PAPER, risk=RiskConfig(exit_policy_version=version))
            engine = PaperEngine(db, cfg)
            engine.initialize(NOW)
            first = engine.step(
                "BTCUSDT",
                {"BTCUSDT": market()},
                FixedStrategy(),
                cycle_id="buy",
                now=NOW,
                average=average(),
            )
            self.assertEqual(first["status"], "FILLED")
            now = NOW + timedelta(hours=1)
            m = market(now=NOW if stale else now)
            if wide:
                m = replace(m, ticker=replace(m.ticker, bid=Decimal("48000")))
            decision = FixedStrategy(action=Action.SELL, notional=Decimal("49")).propose(
                engine.prepare_snapshot("BTCUSDT", {"BTCUSDT": market(now=now)}, now), now
            )
            decision = replace(decision, expected_edge_bps=Decimal("0.1"))
            with patch.object(SMAStrategy, "propose", return_value=decision):
                result = engine.step(
                    "BTCUSDT",
                    {"BTCUSDT": m},
                    SMAStrategy(),
                    cycle_id="sell",
                    now=now,
                    average=average(now=now),
                )
            remaining = db.execute("SELECT quantity FROM paper_positions").fetchone()[0]
            return result, Decimal(remaining)

    def test_exit_removes_edge_gate_and_sells_entire_tradable_holding(self):
        old, _ = self.run_exit(1)
        self.assertEqual(old["status"], "REJECTED")
        new, remaining = self.run_exit(2)
        self.assertEqual(new["status"], "FILLED", new)
        self.assertEqual(remaining, 0)

    def test_exit_preserves_market_age_and_cost_cap(self):
        for options in ({"stale": True}, {"wide": True}):
            with self.subTest(options=options):
                result, remaining = self.run_exit(2, **options)
                self.assertEqual(result["status"], "REJECTED")
                self.assertGreater(remaining, 0)

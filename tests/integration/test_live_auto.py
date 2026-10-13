import os
import unittest
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.integration import test_live_control as fixtures
from trader.live_auto import ARM_PHRASE, arm, cycle
from trader.models import Action, TradeProposal
from trader.strategy.baseline import StrategyResult


class AutomaticTests(unittest.TestCase):
    filled = fixtures.LiveTests.filled

    def setUp(self):
        fixtures.LiveTests.setUp(self)
        self.auto_env = patch.dict(
            os.environ, {"ENABLE_AUTO_LIVE": "true", "LIVE_IP_GUARD": "true"}
        )
        self.auto_env.start()
        self.addCleanup(self.auto_env.stop)
        self.ip = patch.object(self.control, "check_ip")
        self.ip.start()
        self.addCleanup(self.ip.stop)

    def test_auto_disabled_and_failed_evidence_cannot_arm(self):
        with patch.dict(os.environ, {"ENABLE_AUTO_LIVE": "false"}):
            with self.assertRaisesRegex(ValueError, "AUTO_LIVE_DISABLED"):
                arm(self.control, ARM_PHRASE)
        with patch("trader.live_control.read_report", return_value={}):
            with self.assertRaisesRegex(ValueError, "TESTNET"):
                arm(self.control, ARM_PHRASE)
        self.adapter.place_order.assert_not_called()

    def test_auto_buy_once_per_slot_and_manual_cannot_race(self):
        arm(self.control, ARM_PHRASE)
        m = self.adapter.get_ticker.return_value
        market = SimpleNamespace(
            ticker=m, candles_1m=(), candles_5m=(), candles_15m=(), candles_1h=()
        )
        decision = StrategyResult(
            TradeProposal("BTCUSDT", Action.BUY, D(".7"), D(15), "test", 60), D(100)
        )

        def reconcile():
            proposal_id = self.control.state["pending"]
            return self.filled(proposal_id)

        self.adapter.reconcile.side_effect = lambda *args: reconcile()
        with (
            patch("trader.live_auto.collect_market", return_value=market),
            patch("trader.live_auto.HourlyTrendStrategy.propose", return_value=decision),
        ):
            cycle(self.control, MagicMock())
            self.adapter.get_account.return_value = replace(
                self.adapter.get_account.return_value,
                balances=self.adapter.get_balances.return_value,
            )
            self.assertEqual(cycle(self.control, MagicMock())["reason"], "CYCLE_ALREADY_EVALUATED")
        self.adapter.place_order.assert_called_once()
        self.assertEqual(self.control.state["orders"], 1)
        with self.assertRaisesRegex(ValueError, "AUTOMATIC_MODE_ACTIVE"):
            self.control.prepare("BUY", "10")

    def test_auto_hold_does_not_submit(self):
        arm(self.control, ARM_PHRASE)
        m = self.adapter.get_ticker.return_value
        market = SimpleNamespace(
            ticker=m, candles_1m=(), candles_5m=(), candles_15m=(), candles_1h=()
        )
        with patch("trader.live_auto.collect_market", return_value=market):
            self.assertEqual(cycle(self.control, MagicMock())["action"], "HOLD")
        self.adapter.place_order.assert_not_called()

    def test_pending_is_read_only_reconciled_and_disarmed(self):
        arm(self.control, ARM_PHRASE)
        self.control.state["pending"] = "old-order"
        self.filled("old-order")
        cycle(self.control, MagicMock())
        self.assertTrue(self.control.switch.active)
        self.assertFalse(self.control.state["auto"]["armed"])
        self.assertIsNone(self.control.state["pending"])
        self.adapter.place_order.assert_not_called()
        with self.assertRaisesRegex(ValueError, "AUTO_ARM_REQUIRED"):
            cycle(self.control, MagicMock())

    def test_order_failure_keeps_intent_and_never_repeats_post(self):
        from trader.exchange.errors import AmbiguousOrder

        arm(self.control, ARM_PHRASE)
        proposal = self.control.prepare("BUY", "10", automatic=True)
        self.adapter.place_order.side_effect = AmbiguousOrder()
        with self.assertRaises(AmbiguousOrder):
            self.control.submit(proposal, automatic=True)
        self.assertTrue(self.control.switch.active)
        self.assertEqual(self.control.state["pending"], proposal["id"])
        with self.assertRaises(ValueError):
            self.control.submit(proposal, automatic=True)
        self.adapter.place_order.assert_called_once()

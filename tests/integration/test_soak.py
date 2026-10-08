import unittest
from decimal import Decimal as D

from tests.integration.test_testnet_acceptance import result
from trader.config import TradingMode
from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.errors import TradingDisabled
from trader.models import Action
from trader.testnet_soak import apply_fill, guard


class SoakTests(unittest.TestCase):
    def state(self):
        return dict(cash="100", btc="0", times=[], last_trade=0, loss_latched=False)

    def check(self, state, action=Action.BUY, amount="50", edge="100"):
        return guard(state, action, D(amount), D("10000"), D("10001"), D(edge), 10000)

    def test_caps_and_entry_gates(self):
        self.assertIsNone(self.check(self.state()))
        self.assertEqual(self.check(self.state(), amount="51"), "ORDER_LIMIT")
        self.assertEqual(self.check(self.state(), edge="10"), "COST_GATE")
        s = self.state()
        s["loss_latched"] = True
        self.assertEqual(self.check(s), "LOSS_LIMIT")
        s = self.state()
        s["times"] = [1, 2, 3, 4]
        self.assertEqual(self.check(s), "DAILY_ORDER_LIMIT")
        s = self.state()
        s["last_trade"] = 9999
        self.assertEqual(self.check(s), "COOLDOWN")
        s = self.state()
        s["btc"] = ".005"
        self.assertEqual(self.check(s), "POSITION_EXISTS")

    def test_invalid_and_oversell(self):
        self.assertEqual(self.check(self.state(), amount="NaN"), "INVALID_MARKET")
        self.assertEqual(self.check(self.state(), action=Action.SELL), "NO_POSITION")
        with self.assertRaises(ValueError):
            apply_fill(self.state(), result("SELL"))

    def test_fees_accounted_before_next_decision(self):
        s = self.state()
        s["pending"] = "buy"
        apply_fill(s, result("BUY"))
        self.assertEqual(D(s["cash"]), D("49.95"))
        self.assertEqual(D(s["btc"]), D(".005"))
        self.assertIsNone(s["pending"])

    def test_live_path_is_rejected_even_with_transport(self):
        from trader.exchange.testnet_transport import TestnetTransport

        with self.assertRaises(TradingDisabled):
            BinanceSpotAdapter(mode=TradingMode.LIVE, transport=TestnetTransport())

    def test_restart_applies_pending_fill_once_and_preserves_deadline(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from trader.storage.db import connect
        from trader.testnet_soak import main

        with tempfile.TemporaryDirectory() as folder:
            db = connect(Path(folder) / "soak.db")
            db.execute("CREATE TABLE soak_state (id INTEGER PRIMARY KEY, payload TEXT)")
            s = self.state()
            s.update(pending="existing-buy", deadline=1, orders=0, status="RUNNING")
            with db:
                db.execute("INSERT INTO soak_state VALUES (1,?)", (json.dumps(s),))
            db.close()
            adapter = MagicMock()
            adapter.journal.expected_request.return_value = {"side": "BUY"}
            adapter.reconcile.return_value = result("BUY")
            with (
                patch("sys.argv", ["soak", "run", "--data", folder]),
                patch.dict(
                    "os.environ",
                    {
                        "BINANCE_TESTNET_API_KEY": "fixture",
                        "BINANCE_TESTNET_API_SECRET": "fixture",
                        "TRADING_MODE": "TESTNET",
                        "ENABLE_LIVE_TRADING": "false",
                    },
                ),
                patch("trader.testnet_soak.BinanceSpotAdapter", return_value=adapter),
            ):
                main()
                main()
            adapter.place_order.assert_not_called()
            self.assertEqual(adapter.reconcile.call_count, 1)
            db = connect(Path(folder) / "soak.db")
            saved = json.loads(db.execute("SELECT payload FROM soak_state").fetchone()[0])
            self.assertEqual(saved["orders"], 1)
            self.assertEqual(D(saved["cash"]), D("49.95"))
            self.assertEqual(saved["deadline"], 1)
            db.close()

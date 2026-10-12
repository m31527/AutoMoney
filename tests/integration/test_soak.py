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

    def check(self, state, action=Action.BUY, amount="15", edge="100"):
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
            s.update(
                pending="existing-buy",
                deadline=1,
                orders=0,
                status="RUNNING",
                policy_version="initial-100-v1",
            )
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

    def test_readiness_cannot_pass_on_time_or_holds_alone(self):
        from trader.live_readiness import assess

        report = assess({"cycles": 600, "policy_version": "initial-100-v1"}, [], False)
        self.assertEqual(report["live_status"], "BLOCKED")
        self.assertEqual(report["testnet_status"], "NOT_PASSED")
        self.assertFalse(report["testnet_checks"]["strategy_buy_and_sell"])

    def test_cumulative_loss_stays_blocked_after_daily_reset(self):
        s = self.state()
        s["cumulative_loss_latched"] = True
        self.assertEqual(self.check(s), "LOSS_LIMIT")

    def test_dashboard_report_missing_does_not_create_database(self):
        import tempfile
        from pathlib import Path

        from trader.live_readiness import read_report

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing"
            self.assertFalse(read_report(path)["available"])
            self.assertFalse(path.exists())

    def test_dashboard_report_only_exposes_observation_allowlist(self):
        import json
        import tempfile
        from pathlib import Path

        from trader.live_readiness import read_report
        from trader.storage.db import connect

        with tempfile.TemporaryDirectory() as folder:
            db = connect(Path(folder) / "soak.db")
            db.execute("CREATE TABLE soak_state(id INTEGER PRIMARY KEY,payload TEXT)")
            db.execute("CREATE TABLE soak_events(id INTEGER PRIMARY KEY,payload TEXT)")
            with db:
                db.execute(
                    "INSERT INTO soak_state VALUES (1,?)",
                    (json.dumps({"base_cash": "private", "cycles": 1}),),
                )
            db.close()
            report = read_report(Path(folder))
            self.assertTrue(report["available"])
            self.assertNotIn("private", json.dumps(report, default=str))
            self.assertEqual(report["live_status"], "BLOCKED")

    def test_recovery_refuses_pending_and_manual_stop(self):
        from unittest.mock import MagicMock, patch

        from trader.testnet_soak import recover_empty_observation

        adapter, db, switch = MagicMock(), MagicMock(), MagicMock()
        s = dict(error="OrderRejected", pending="pending", orders=0, btc="0")
        with patch("trader.testnet_soak.Repository") as repo:
            repo.return_value.safety_state.return_value.reason = "SOAK_REVIEW_REQUIRED"
            with self.assertRaises(ValueError):
                recover_empty_observation(adapter, db, switch, s)
            s["pending"] = None
            repo.return_value.safety_state.return_value.reason = "Operator requested stop"
            with self.assertRaises(ValueError):
                recover_empty_observation(adapter, db, switch, s)
        switch.resume.assert_not_called()
        adapter.get_account.assert_not_called()

    def test_recovery_checks_real_balances_before_clearing(self):
        from unittest.mock import MagicMock, patch

        from trader.exchange.models import Balance
        from trader.testnet_soak import recover_empty_observation

        adapter, db, switch = MagicMock(), MagicMock(), MagicMock()
        db.execute.return_value.fetchone.return_value = (0,)
        adapter.get_open_orders.return_value = ()
        adapter.get_account.return_value.can_trade = True
        adapter.get_account.return_value.balances = (
            Balance("BTC", D("1"), D("0")),
            Balance("USDT", D("1000"), D("0")),
        )
        s = dict(
            error="OrderRejected", pending=None, orders=0, btc="0", base_btc="1", base_cash="1001"
        )
        with patch("trader.testnet_soak.Repository") as repo:
            repo.return_value.safety_state.return_value.reason = "SOAK_REVIEW_REQUIRED"
            with self.assertRaises(ValueError):
                recover_empty_observation(adapter, db, switch, s)
            switch.resume.assert_not_called()
            s["base_cash"] = "1000"
            recover_empty_observation(adapter, db, switch, s)
        switch.resume.assert_called_once()
        adapter.place_order.assert_not_called()


class TransientReadTests(unittest.TestCase):
    def test_retry_budget_and_pending_stop_are_not_bypassed(self):
        from unittest.mock import MagicMock

        from trader.exchange.errors import AuthenticationError, NetworkError, TemporaryError
        from trader.testnet_soak import record_read_retry

        switch = MagicMock()
        switch.active = False
        state = {"pending": None}
        self.assertEqual(record_read_retry(state, switch, NetworkError()), 30)
        self.assertEqual(record_read_retry(state, switch, TemporaryError()), 60)
        with self.assertRaises(TemporaryError):
            record_read_retry(state, switch, TemporaryError())
        for state, active, error in (
            ({"pending": "sent"}, False, NetworkError()),
            ({"pending": None}, True, NetworkError()),
            ({"pending": None}, False, AuthenticationError()),
        ):
            switch.active = active
            with self.assertRaises(type(error)):
                record_read_retry(state, switch, error)

    def test_recovery_preserves_history_and_requires_accounted_submissions(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock

        from trader.exchange.models import Balance
        from trader.safety.kill_switch import KillSwitch
        from trader.storage.db import connect
        from trader.storage.repository import Repository, encode
        from trader.testnet_soak import recover_transient_observation

        with tempfile.TemporaryDirectory() as folder:
            db = connect(Path(folder) / "soak.db")
            self.addCleanup(db.close)
            db.execute("CREATE TABLE soak_state(id INTEGER PRIMARY KEY,payload TEXT)")
            db.execute("CREATE TABLE soak_events(id INTEGER PRIMARY KEY,payload TEXT)")
            state = dict(
                status="BLOCKED",
                error="TemporaryError",
                pending=None,
                orders=1,
                cash="49.95",
                btc=".005",
                base_cash="1000",
                base_btc="0",
                cycles=3,
            )
            db.execute("INSERT INTO soak_state VALUES(1,?)", (encode(state),))
            db.execute("INSERT INTO exchange_submissions VALUES(?,?,?)", ("buy", "{}", "now"))
            db.commit()
            switch = KillSwitch(Repository(db))
            switch.trip("SOAK_REVIEW_REQUIRED")
            adapter = MagicMock()
            adapter.get_account.return_value.can_trade = True
            adapter.get_account.return_value.balances = (
                Balance("USDT", D("949.95"), D(0)),
                Balance("BTC", D(".005"), D(0)),
            )
            adapter.get_open_orders.return_value = ()
            with self.assertRaisesRegex(ValueError, "UNACCOUNTED"):
                recover_transient_observation(adapter, db, switch, state)
            db.execute(
                "INSERT INTO soak_events(payload) VALUES(?)",
                (encode({"event": "FILL", "result": {"order": {"client_order_id": "buy"}}}),),
            )
            db.commit()
            adapter.get_open_orders.return_value = ("open",)
            with self.assertRaisesRegex(ValueError, "ACCOUNT_MISMATCH"):
                recover_transient_observation(adapter, db, switch, state)
            adapter.get_open_orders.return_value = ()
            recover_transient_observation(adapter, db, switch, state)
            self.assertTrue(switch.active)
            self.assertEqual(state["status"], "RECONCILED_STOPPED")
            self.assertEqual(state["orders"], 1)
            self.assertEqual(state["cash"], "49.95")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM soak_events").fetchone()[0], 2)
            self.assertEqual(
                json.loads(db.execute("SELECT payload FROM soak_state").fetchone()[0])["cycles"], 3
            )
            adapter.place_order.assert_not_called()

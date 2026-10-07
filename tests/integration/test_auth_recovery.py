import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from trader.exchange.errors import AuthenticationError
from trader.exchange.models import Credentials
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.exchange_journal import ExchangeJournal
from trader.storage.repository import Repository
from trader.testnet_acceptance import authentication_report, check_and_resume


class AuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = connect(Path(tmp.name) / "test.db")
        self.addCleanup(self.db.close)
        self.switch = KillSwitch(Repository(self.db))
        self.credentials = Credentials("fixture", "fixture-secret")

    def test_auth_recovery_only_reads_and_preserves_journal(self):
        self.switch.trip(AuthenticationError.code)
        journal = ExchangeJournal(self.db)
        journal.claim("existing", {"symbol": "BTCUSDT"})
        adapter = MagicMock()
        adapter.get_account.return_value.can_trade = True
        adapter.reconcile.return_value.fills_complete = True
        adapter.reconcile.return_value.order.status = "FILLED"
        with patch("trader.testnet_acceptance.BinanceSpotAdapter", return_value=adapter):
            result = check_and_resume(self.db, self.switch, self.credentials)
        self.assertFalse(self.switch.active)
        self.assertEqual(result["orders_submitted"], 0)
        adapter.place_order.assert_not_called()
        self.assertIsNotNone(journal.expected_request("existing"))

    def test_failed_auth_keeps_stop(self):
        self.switch.trip(AuthenticationError.code)
        adapter = MagicMock()
        adapter.get_account.side_effect = AuthenticationError(exchange_code=-2015)
        with patch("trader.testnet_acceptance.BinanceSpotAdapter", return_value=adapter):
            with self.assertRaises(AuthenticationError):
                check_and_resume(self.db, self.switch, self.credentials)
        self.assertTrue(self.switch.active)

    def test_operator_stop_cannot_be_cleared_by_auth_recovery(self):
        self.switch.kill()
        with self.assertRaises(ValueError):
            check_and_resume(self.db, self.switch, self.credentials)
        self.assertTrue(self.switch.active)

    def test_diagnostics_are_allowlisted_and_contain_no_credentials(self):
        report = authentication_report(AuthenticationError(http_status=401, exchange_code=-2015))
        self.assertEqual(report["exchange_code"], -2015)
        self.assertEqual(set(report), {"error", "exchange_code", "http_status", "hint"})

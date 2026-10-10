import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from tests.integration.test_testnet_acceptance import result
from trader.exchange.errors import NetworkError, TradingDisabled
from trader.exchange.models import Account, Balance, Credentials, Ticker
from trader.exchange.production import ProductionAdapter, ProductionTransport
from trader.exchange.transport import Response
from trader.live_control import CONFIRMATION, LiveControl
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.repository import Repository, encode
from trader.testnet_soak import recover_pending_observation


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "BINANCE_LIVE_API_KEY": "fixture",
                "BINANCE_LIVE_API_SECRET": "fixture",
                "ENABLE_LIVE_TRADING": "true",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.control = LiveControl(Path(self.temp.name), Path(self.temp.name) / "soak")
        self.addCleanup(self.control.db.close)
        self.adapter = MagicMock()
        self.control.adapter = self.adapter
        self.adapter.check_permissions.return_value = {"verified": True}
        now = datetime.now(UTC)
        self.adapter.get_account.return_value = Account(
            now, now, "SPOT", True, True, (Balance("USDT", D(1000), D(0)),)
        )
        self.adapter.get_open_orders.return_value = ()
        self.adapter.get_ticker.return_value = Ticker(
            "BTCUSDT", now, now, D(10000), D(10000), D(10001), D(100)
        )
        self.evidence = patch(
            "trader.live_control.read_report", return_value={"testnet_status": "REVIEW_REQUIRED"}
        )
        self.evidence.start()
        self.addCleanup(self.evidence.stop)
        self.quantity = patch("trader.live_control.market_quantity", return_value=D(".001"))
        self.quantity.start()
        self.addCleanup(self.quantity.stop)

    def prepare(self):
        return self.control.prepare("BUY", "11")

    def test_missing_evidence_or_key_or_enable_never_sends(self):
        self.assertIsNone(self.control.status()["state"]["cash"])
        with patch(
            "trader.live_control.read_report", return_value={"testnet_status": "NOT_PASSED"}
        ):
            with self.assertRaisesRegex(ValueError, "TESTNET_ACCEPTANCE"):
                self.prepare()
        p = self.prepare()
        with patch.dict(os.environ, {"ENABLE_LIVE_TRADING": "false"}):
            with self.assertRaisesRegex(ValueError, "LIVE_EXECUTION_DISABLED"):
                self.control.approve(p["id"], CONFIRMATION, True)
        self.adapter.place_order.assert_not_called()
        self.control.adapter = None
        with self.assertRaisesRegex(ValueError, "CREDENTIALS_MISSING"):
            self.control.preflight()

    def test_expired_wrong_id_missing_review_and_price_change_never_send(self):
        p = self.prepare()
        for pid, text, reviewed in [
            ("wrong", CONFIRMATION, True),
            (p["id"], "", True),
            (p["id"], CONFIRMATION, False),
        ]:
            with self.assertRaises(ValueError):
                self.control.approve(pid, text, reviewed)
        p["expires"] = time.time() - 1
        with self.assertRaisesRegex(ValueError, "EXPIRED"):
            self.control.approve(p["id"], CONFIRMATION, True)
        p["expires"] = time.time() + 120
        self.adapter.get_ticker.return_value = replace(
            self.adapter.get_ticker.return_value, ask=D(11001), bid=D(11000)
        )
        with self.assertRaisesRegex(ValueError, "PRICE_CHANGED"):
            self.control.approve(p["id"], CONFIRMATION, True)
        self.adapter.place_order.assert_not_called()

    def test_amount_position_loss_and_balance_gates(self):
        for amount in ("NaN", "Infinity", "-1", "16"):
            with self.assertRaises(ValueError):
                self.control.prepare("BUY", amount)
        self.control.state["loss_latched"] = True
        with self.assertRaisesRegex(ValueError, "LOSS_LIMIT"):
            self.control.limits("BUY", D(".001"), D(10000))
        self.control.state["loss_latched"] = False
        self.control.state["btc"] = ".003"
        with self.assertRaisesRegex(ValueError, "BUDGET_LIMIT"):
            self.control.limits("BUY", D(".001"), D(10000))
        with self.assertRaisesRegex(ValueError, "NO_OWNED_POSITION"):
            self.control.limits("SELL", D(".004"), D(1000))
        self.control.state["btc"] = "0"
        self.control.preflight()
        self.adapter.get_account.return_value = replace(
            self.adapter.get_account.return_value, balances=(Balance("USDT", D(999), D(0)),)
        )
        with self.assertRaisesRegex(ValueError, "ACCOUNT_DRIFT"):
            self.control.preflight()
        self.adapter.place_order.assert_not_called()

    def filled(self, cid):
        r = result("BUY")
        r = replace(
            r,
            order=replace(
                r.order,
                client_order_id=cid,
                requested_quantity=D(".001"),
                executed_quantity=D(".001"),
                cumulative_quote_quantity=D(10),
            ),
            fills=(replace(r.fills[0], quantity=D(".001"), quote_quantity=D(10), fee=D(".01")),),
        )
        self.adapter.reconcile.return_value = r
        self.adapter.get_balances.return_value = (
            Balance("BTC", D(".001"), D(0)),
            Balance("USDT", D("989.99"), D(0)),
        )
        return r

    def test_approval_exactly_once_and_balance_updated(self):
        p = self.prepare()
        self.filled(p["id"])
        self.control.approve(p["id"], CONFIRMATION, True)
        self.assertEqual(self.control.state["cash"], "89.99")
        self.assertEqual(self.control.state["orders"], 1)
        self.assertIsNone(self.control.state["pending"])
        with self.assertRaises(ValueError):
            self.control.approve(p["id"], CONFIRMATION, True)
        self.adapter.place_order.assert_called_once()
        self.assertIsNone(self.control.transport.grant)

    def test_lost_response_survives_restart_reconciles_never_reposts(self):
        p = self.prepare()
        self.adapter.place_order.side_effect = NetworkError()
        with self.assertRaises(NetworkError):
            self.control.approve(p["id"], CONFIRMATION, True)
        self.assertTrue(self.control.switch.active)
        self.assertEqual(self.control.state["pending"], p["id"])
        restarted = LiveControl(Path(self.temp.name), Path(self.temp.name) / "soak")
        self.addCleanup(restarted.db.close)
        restarted.adapter = self.adapter
        self.filled(p["id"])
        restarted.reconcile()
        self.assertEqual(restarted.state["orders"], 1)
        self.assertTrue(restarted.switch.active)
        with self.assertRaisesRegex(ValueError, "NO_PENDING"):
            restarted.reconcile()
        self.adapter.place_order.assert_called_once()

    def test_incomplete_or_mismatched_account_cannot_clear_pending(self):
        p = self.prepare()
        self.control.state["pending"] = p["id"]
        r = self.filled(p["id"])
        self.adapter.reconcile.return_value = replace(r, fills_complete=False)
        with self.assertRaises(ValueError):
            self.control.reconcile()
        self.adapter.reconcile.return_value = r
        self.adapter.get_balances.return_value = ()
        with self.assertRaisesRegex(ValueError, "ACCOUNT_MISMATCH"):
            self.control.reconcile()
        self.assertEqual(self.control.state["pending"], p["id"])
        self.assertEqual(self.control.state["cash"], "100")

    def test_key_change_requires_review_and_manual_stop_persists(self):
        self.control.preflight()
        self.control.key_id = "other"
        with self.assertRaisesRegex(ValueError, "KEY_CHANGED"):
            self.control.preflight()
        self.control.dispatch("stop", {})
        with self.assertRaisesRegex(ValueError, "PERSISTENT_STOP"):
            self.prepare()
        self.adapter.place_order.assert_not_called()


class ProductionBoundaryTests(unittest.TestCase):
    def test_production_transport_requires_exact_single_use_grant(self):
        t = ProductionTransport()
        grant = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": ".001",
            "newClientOrderId": "live_test",
            "newOrderRespType": "FULL",
        }
        body = urlencode(
            {**grant, "timestamp": "1", "recvWindow": "5000", "signature": "fixture"}
        ).encode()
        with self.assertRaises(TradingDisabled):
            t.request("POST", "https://api.binance.com/api/v3/order", {}, body, 1)
        with patch(
            "trader.exchange.testnet_transport.TestnetTransport.request",
            return_value=Response(200, {}, b"{}"),
        ) as network:
            t.grant = grant
            t.request("POST", "https://api.binance.com/api/v3/order", {}, body, 1)
            with self.assertRaises(TradingDisabled):
                t.request("POST", "https://api.binance.com/api/v3/order", {}, body, 1)
            network.assert_called_once()
        for url in (
            "http://api.binance.com/api/v3/time",
            "https://api.binance.com.evil/api/v3/time",
            "https://testnet.binance.vision/api/v3/time",
            "https://api.binance.com/sapi/v1/capital/withdraw/apply",
        ):
            with self.assertRaises(TradingDisabled):
                t.request("GET", url, {}, None, 1)
        t.grant = grant
        with self.assertRaises(TradingDisabled):
            t.request(
                "POST", "https://api.binance.com/api/v3/order", {}, body.replace(b"BUY", b"SELL"), 1
            )

    def test_permissions_fail_closed(self):
        adapter = ProductionAdapter(Credentials("fixture", "fixture"))
        valid = {
            k: False
            for k in (
                "enableWithdrawals",
                "enableInternalTransfer",
                "permitsUniversalTransfer",
                "enableMargin",
                "enableFutures",
                "enableVanillaOptions",
                "enablePortfolioMarginTrading",
                "enableFixApiTrade",
            )
        }
        valid.update(ipRestrict=True, enableReading=True, enableSpotAndMarginTrading=True)
        with patch.object(adapter, "_request", return_value=valid):
            self.assertTrue(all(adapter.check_permissions().values()))
        for invalid in (
            {},
            {**valid, "enableWithdrawals": True},
            {**valid, "ipRestrict": False},
            {**valid, "enableReading": 1},
        ):
            with patch.object(adapter, "_request", return_value=invalid):
                with self.assertRaises(ValueError):
                    adapter.check_permissions()


class PendingRecoveryTests(unittest.TestCase):
    def test_fill_recovered_once_with_strict_balance_and_stop_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            db = connect(Path(folder) / "soak.db")
            self.addCleanup(db.close)
            db.execute("CREATE TABLE soak_state (id INTEGER PRIMARY KEY, payload TEXT)")
            db.execute("CREATE TABLE soak_events (id INTEGER PRIMARY KEY, payload TEXT)")
            state = dict(
                cash="100",
                btc="0",
                times=[],
                pending="BUY",
                orders=0,
                base_cash="1000",
                base_btc="0",
                error="AmbiguousOrder",
            )
            with db:
                db.execute("INSERT INTO soak_state VALUES (1,?)", (encode(state),))
            switch = KillSwitch(Repository(db))
            switch.trip("SOAK_REVIEW_REQUIRED")
            adapter = MagicMock()
            adapter.reconcile.return_value = result("BUY")
            adapter.get_open_orders.return_value = ()
            adapter.get_account.return_value.can_trade = True
            adapter.get_account.return_value.balances = ()
            with self.assertRaisesRegex(ValueError, "ACCOUNT_MISMATCH"):
                recover_pending_observation(adapter, db, switch, state)
            self.assertEqual(state["pending"], "BUY")
            adapter.get_account.return_value.balances = (
                Balance("USDT", D("949.95"), D(0)),
                Balance("BTC", D(".005"), D(0)),
            )
            recover_pending_observation(adapter, db, switch, state)
            self.assertTrue(switch.active)
            self.assertIsNone(state["pending"])
            self.assertEqual(state["orders"], 1)
            saved = json.loads(db.execute("SELECT payload FROM soak_state").fetchone()[0])
            self.assertEqual(saved["status"], "RECONCILED_STOPPED")
            with self.assertRaises(ValueError):
                recover_pending_observation(adapter, db, switch, state)
            adapter.place_order.assert_not_called()


class ControlHTTPTests(unittest.TestCase):
    def test_http_auth_and_fixed_routes(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        from trader.live_server import Handler

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_port)
        with (
            patch.dict(os.environ, {"LIVE_CONTROL_TOKEN": "x" * 48}),
            patch("trader.live_server.operate", return_value={"ok": True}) as operation,
        ):
            for headers, path in (
                ({}, "/approve"),
                ({"Authorization": "Bearer wrong"}, "/approve"),
                ({"Authorization": "Bearer " + "x" * 48}, "/withdraw"),
            ):
                request = urllib.request.Request(
                    base + path, data=b"{}", headers={"Content-Type": "application/json", **headers}
                )
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(request)
            operation.assert_not_called()
            request = urllib.request.Request(
                base + "/preflight",
                data=b"{}",
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + "x" * 48},
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
            operation.assert_called_once()

    def test_stop_does_not_wait_for_controller_lock(self):
        import fcntl

        from trader.live_control import operate

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (root / "control.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(operate(root, root, "stop", {})["stop_active"])
            db = connect(root / "live.db")
            try:
                self.assertTrue(KillSwitch(Repository(db)).active)
            finally:
                db.close()


class ReadonlyReadinessTests(unittest.TestCase):
    def test_closed_wal_does_not_create_sidecars_on_original_mount(self):
        from trader.live_readiness import read_report

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "soak.db"
            db = connect(path)
            with db:
                db.execute("CREATE TABLE soak_state (id INTEGER PRIMARY KEY, payload TEXT)")
                db.execute("CREATE TABLE soak_events (id INTEGER PRIMARY KEY, payload TEXT)")
                db.execute("INSERT INTO soak_state VALUES (1,?)", ('{"cycles":0}',))
            db.close()
            before = {f.name for f in Path(folder).iterdir()}
            self.assertTrue(read_report(Path(folder))["available"])
            self.assertEqual(before, {f.name for f in Path(folder).iterdir()})

    def test_snapshot_race_is_rejected_instead_of_ignoring_wal(self):
        import shutil
        import sqlite3

        from trader.live_readiness import readonly_connection

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "probe.db"
            path.write_bytes(b"fixture")
            original = shutil.copyfile

            def racing(source, destination):
                result = original(source, destination)
                Path(str(path) + "-wal").write_bytes(b"new journal")
                return result

            with patch("trader.live_readiness.shutil.copyfile", side_effect=racing):
                with self.assertRaisesRegex(sqlite3.OperationalError, "CHANGED_RETRY"):
                    with readonly_connection(path):
                        self.fail("Must not use a stale main file")

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.fixtures.binance import (
    CLIENT_ID,
    NOW,
    FakeClock,
    FakeTransport,
    candles,
    fill,
    metadata,
    order,
    response,
    ticker,
)
from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.errors import (
    AmbiguousOrder,
    AuthenticationError,
    DuplicateOrderConflict,
    MalformedResponse,
    NetworkError,
    OrderRejected,
    TradingDisabled,
)
from trader.exchange.models import Credentials
from trader.main import main
from trader.market.data import StaleMarketData, collect_market
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import SCHEMA, connect
from trader.storage.exchange_journal import ExchangeJournal
from trader.storage.repository import Repository


class ExchangeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)
        self.repository = Repository(self.connection)
        self.switch = KillSwitch(self.repository)
        self.journal = ExchangeJournal(self.connection)
        self.clock = FakeClock()

    def adapter(self, *responses, connection=None):
        transport = FakeTransport(*responses)
        adapter = BinanceSpotAdapter(
            credentials=Credentials("test-key", "test-secret"),
            transport=transport,
            clock=self.clock.time,
            sleep=self.clock.sleep,
            monotonic=self.clock.time,
            journal=ExchangeJournal(connection) if connection else self.journal,
            kill_switch=KillSwitch(Repository(connection)) if connection else self.switch,
        )
        return adapter, transport

    def time_response(self):
        return response({"serverTime": int(self.clock.time() * 1000)})

    def submit(self, adapter, quantity="0.001"):
        return adapter.place_order("BTCUSDT", "BUY", Decimal(quantity), CLIENT_ID)

    def test_timeout_reconciles_without_second_post(self):
        adapter, transport = self.adapter(
            self.time_response(), NetworkError(), response(order()), response([fill()])
        )
        result = self.submit(adapter)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.average_fill_price, Decimal("50000"))
        self.assertEqual(transport.count("POST"), 1)
        post = next(call for call in transport.calls if call[0] == "POST")
        self.assertNotIn("signature", post[1])  # signed POST parameters live in the body
        self.assertEqual(parse_qs(post[3].decode())["type"], ["MARKET"])
        records = self.connection.execute(
            "SELECT observation_json FROM exchange_observations"
        ).fetchall()
        self.assertEqual(json.loads(records[0][0])["state"], "UNKNOWN")
        self.assertTrue(json.loads(records[-1][0])["fills_complete"])
        stored = json.loads(
            self.connection.execute("SELECT fill_json FROM exchange_fills").fetchone()[0]
        )
        self.assertEqual(stored["fee_asset"], "BTC")
        self.assertEqual(stored["fee"], "0.000001")

    def test_duplicate_id_after_restart_only_queries_and_deduplicates_fills(self):
        adapter, first = self.adapter(
            self.time_response(), response(order()), response(order()), response([fill()])
        )
        self.submit(adapter)
        second_connection = connect(self.path)
        self.addCleanup(second_connection.close)
        restarted, second = self.adapter(
            self.time_response(),
            response(order()),
            response([fill()]),
            connection=second_connection,
        )
        self.submit(restarted, "0.0010")
        self.assertEqual(first.count("POST"), 1)
        self.assertEqual(second.count("POST"), 0)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM exchange_fills").fetchone()[0], 1
        )

    def test_same_id_different_quantity_rejected_before_network(self):
        adapter, _ = self.adapter(
            self.time_response(), response(order()), response(order()), response([fill()])
        )
        self.submit(adapter)
        retry, transport = self.adapter()
        with self.assertRaises(DuplicateOrderConflict):
            self.submit(retry, "0.002")
        self.assertEqual(transport.calls, [])

    def test_unknown_order_not_found_trips_persistent_stop_no_resubmit(self):
        adapter, transport = self.adapter(
            self.time_response(), NetworkError(), response({"code": -2013}, 400)
        )
        with self.assertRaises(AmbiguousOrder):
            self.submit(adapter)
        self.assertTrue(self.switch.active)
        self.assertEqual(transport.count("POST"), 1)
        self.assertEqual(self.repository.safety_state().reason, "RECONCILIATION_REQUIRED")
        # Even explicit resume does not authorize resending the unresolved ID.
        self.switch.resume()
        retry, transport = self.adapter(self.time_response(), response({"code": -2013}, 400))
        with self.assertRaises(AmbiguousOrder):
            self.submit(retry)
        self.assertEqual(transport.count("POST"), 0)

    def test_crash_after_claim_never_sends_again(self):
        request = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.001",
            "newClientOrderId": CLIENT_ID,
            "newOrderRespType": "FULL",
        }
        self.assertTrue(self.journal.claim(CLIENT_ID, request))
        adapter, transport = self.adapter(self.time_response(), response({"code": -2013}, 400))
        with self.assertRaises(AmbiguousOrder):
            self.submit(adapter)
        self.assertEqual(transport.count("POST"), 0)

    def test_partial_fills_preserved_without_claiming_full_execution(self):
        partial = order("PARTIALLY_FILLED")
        adapter, transport = self.adapter(
            self.time_response(),
            response(partial),
            response(partial),
            response([fill("0.0004", "20")]),
        )
        result = self.submit(adapter)
        self.assertEqual(result.status, "PARTIALLY_FILLED")
        self.assertEqual(result.executed_quantity, Decimal("0.0004"))
        self.assertEqual(transport.count("POST"), 1)
        self.assertFalse(self.switch.active)

    def test_missing_fills_trips_stop_and_does_not_invent_fees(self):
        adapter, _ = self.adapter(
            self.time_response(), response(order()), response(order()), response([])
        )
        self.submit(adapter)
        self.assertTrue(self.switch.active)
        payload = self.connection.execute(
            "SELECT observation_json FROM exchange_observations ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertFalse(json.loads(payload)["fills_complete"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM exchange_fills").fetchone()[0], 0
        )

    def test_post_500_and_binance_timeout_never_retried(self):
        for failure in (response({}, 500), response({"code": -1007}, 400)):
            with self.subTest(failure=failure):
                identifier = CLIENT_ID + str(failure.status)
                adapter, transport = self.adapter(
                    self.time_response(),
                    failure,
                    response(order(client_id=identifier)),
                    response([fill()]),
                )
                adapter.place_order("BTCUSDT", "BUY", Decimal("0.001"), identifier)
                self.assertEqual(transport.count("POST"), 1)

    def test_explicit_rejection_recorded_not_retried(self):
        adapter, transport = self.adapter(self.time_response(), response({"code": -1013}, 400))
        with self.assertRaises(OrderRejected):
            self.submit(adapter)
        self.assertEqual(transport.count("POST"), 1)
        payload = self.connection.execute(
            "SELECT observation_json FROM exchange_observations"
        ).fetchone()[0]
        self.assertEqual(json.loads(payload)["error"], OrderRejected.code)

    def test_kill_prevents_any_submission(self):
        self.switch.kill()
        adapter, transport = self.adapter()
        with self.assertRaises(TradingDisabled):
            self.submit(adapter)
        self.assertEqual(transport.calls, [])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM exchange_submissions").fetchone()[0], 0
        )

    def test_default_network_adapter_cannot_place_or_cancel(self):
        adapter = BinanceSpotAdapter(
            credentials=Credentials("x", "y"), journal=self.journal, kill_switch=self.switch
        )
        with self.assertRaises(TradingDisabled):
            self.submit(adapter)
        with self.assertRaises(TradingDisabled):
            adapter.cancel_order("BTCUSDT", CLIENT_ID)

    def test_auth_failure_activates_persistent_safe_mode(self):
        adapter, _ = self.adapter(response({"code": -2015}, 401))
        with self.assertRaises(AuthenticationError):
            adapter.get_ticker("BTCUSDT")
        self.assertTrue(self.repository.safety_state().killed)
        self.assertEqual(self.repository.safety_state().reason, AuthenticationError.code)
        self.assertEqual(self.repository.events()[-1]["severity"], "CRITICAL")

    def test_reconciled_order_must_match_original_request(self):
        wrong = {**order(), "side": "SELL"}
        adapter, transport = self.adapter(self.time_response(), NetworkError(), response(wrong))
        with self.assertRaises(AmbiguousOrder):
            self.submit(adapter)
        self.assertTrue(self.switch.active)
        self.assertEqual(transport.count("POST"), 1)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM exchange_fills").fetchone()[0], 0
        )

    def test_kill_during_clock_sync_prevents_post(self):
        adapter, transport = self.adapter(self.time_response())
        original = transport.request

        def request(*args):
            result = original(*args)
            self.switch.kill()
            return result

        transport.request = request
        with self.assertRaises(TradingDisabled):
            self.submit(adapter)
        self.assertEqual(transport.count("POST"), 0)

    def test_conflicting_fill_is_not_overwritten(self):
        adapter, _ = self.adapter(
            self.time_response(), response(order()), response(order()), response([fill()])
        )
        self.submit(adapter)
        changed = {**fill(), "commission": "0.001"}
        retry, transport = self.adapter(
            self.time_response(), response(order()), response([changed])
        )
        with self.assertRaises(MalformedResponse):
            self.submit(retry)
        self.assertTrue(self.switch.active)
        self.assertEqual(transport.count("POST"), 0)
        data = self.connection.execute("SELECT fill_json FROM exchange_fills").fetchone()[0]
        self.assertEqual(json.loads(data)["fee"], "0.000001")

    def test_repeated_read_failure_activates_safe_mode(self):
        adapter, transport = self.adapter(*[NetworkError() for _ in range(9)])
        for _ in range(3):
            with self.assertRaises(NetworkError):
                adapter.get_ticker("BTCUSDT")
        self.assertTrue(self.repository.safety_state().killed)
        self.assertEqual(len(transport.calls), 9)

    def test_malformed_metadata_activates_safe_mode(self):
        adapter, _ = self.adapter(response({"symbols": []}))
        with self.assertRaises(MalformedResponse):
            adapter.get_exchange_info("BTCUSDT")
        self.assertTrue(self.repository.safety_state().killed)

    def test_cancel_timeout_queries_actual_status(self):
        adapter, transport = self.adapter(
            self.time_response(), NetworkError(), response({"code": -2013}, 400), response(order())
        )
        result = adapter.cancel_order("BTCUSDT", CLIENT_ID)
        self.assertEqual(result.status, "FILLED")  # Cancellation did not win the race.
        self.assertEqual(transport.count("DELETE"), 1)

    def test_successful_cancel_queries_replacement_client_id(self):
        cancel_id = "cx_" + hashlib.sha256(CLIENT_ID.encode()).hexdigest()[:32]
        adapter, transport = self.adapter(
            self.time_response(), response({}), response(order("CANCELED", client_id=cancel_id))
        )
        result = adapter.cancel_order("BTCUSDT", CLIENT_ID)
        self.assertEqual(result.status, "CANCELED")
        self.assertEqual(result.client_order_id, cancel_id)
        cancel_call = next(call for call in transport.calls if call[0] == "DELETE")
        self.assertEqual(parse_qs(cancel_call[3].decode())["newClientOrderId"], [cancel_id])

    def test_open_orders(self):
        adapter, transport = self.adapter(self.time_response(), response([order("NEW")]))
        self.assertEqual(adapter.get_open_orders("BTCUSDT")[0].status, "NEW")
        self.assertEqual(urlsplit(transport.calls[-1][1]).path, "/api/v3/openOrders")

    def test_fill_pagination(self):
        page = [fill(trade_id=i) for i in range(1000)]
        adapter, transport = self.adapter(
            self.time_response(), response(page), response([fill(trade_id=1000)])
        )
        result = adapter.get_fills("BTCUSDT", 42)
        self.assertEqual(len(result), 1001)
        self.assertEqual(parse_qs(urlsplit(transport.calls[-1][1]).query)["fromId"], ["1000"])

    def test_v1_migration_preserves_kill_and_events(self):
        old_path = Path(self.temp.name) / "old.db"
        old = sqlite3.connect(old_path)
        old.executescript(SCHEMA)
        old.row_factory = sqlite3.Row
        Repository(old).set_killed(True, "Old stop")
        old.close()
        migrated = connect(old_path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertTrue(Repository(migrated).safety_state().killed)
        self.assertEqual(len(Repository(migrated).events()), 1)

    def test_concurrent_claims_have_one_winner(self):
        def claim(_):
            connection = connect(self.path)
            try:
                return ExchangeJournal(connection).claim(CLIENT_ID, {"symbol": "BTCUSDT"})
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_market_collection_persists_public_data_without_fake_pnl(self):
        adapter, _ = self.adapter(
            response(metadata()), response(ticker()), *(response(candles()) for _ in range(4))
        )
        observation = collect_market(
            adapter, "BTCUSDT", now=lambda: datetime.fromtimestamp(NOW, UTC)
        )
        self.repository.save_market_snapshot(observation)
        raw = self.connection.execute("SELECT snapshot_json FROM market_snapshots").fetchone()[0]
        self.assertEqual(json.loads(raw)["data_kind"], "PUBLIC_MARKET_OBSERVATION")
        self.assertNotIn("realized_pnl", raw)

    def test_stale_market_is_rejected(self):
        stale = {**ticker(), "closeTime": (NOW - 61) * 1000}
        adapter, _ = self.adapter(
            response(metadata()), response(stale), *(response(candles()) for _ in range(4))
        )
        with self.assertRaises(StaleMarketData):
            collect_market(adapter, "BTCUSDT", now=lambda: datetime.fromtimestamp(NOW, UTC))

    def test_cli_market_uses_no_credentials_and_persists_snapshot(self):
        adapter, _ = self.adapter(
            response(metadata()), response(ticker()), *(response(candles()) for _ in range(4))
        )
        snapshot = collect_market(adapter, "BTCUSDT", now=lambda: datetime.fromtimestamp(NOW, UTC))
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": f"sqlite:///{self.path}",
                    "TRADING_MODE": "TESTNET",
                    "ENABLE_LIVE_TRADING": "false",
                    "BINANCE_API_SECRET": "secret",
                },
            ),
            patch("trader.main.BinanceSpotAdapter") as constructor,
            patch("trader.main.collect_market", return_value=snapshot),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(main(["market", "--symbol", "BTCUSDT", "--limit", "10"]), 0)
            self.assertIsNone(constructor.call_args.args[1])
        self.assertEqual(json.loads(out.getvalue())["symbol"], "BTCUSDT")
        self.assertNotIn("secret", out.getvalue() + err.getvalue())

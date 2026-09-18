import hashlib
import hmac
import json
import unittest
from dataclasses import asdict
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.fixtures.binance import (
    NOW,
    FakeClock,
    FakeTransport,
    account,
    candles,
    metadata,
    response,
    ticker,
)
from trader.config import TradingMode
from trader.exchange.binance import PUBLIC_URL, TESTNET_URL, BinanceSpotAdapter, new_client_order_id
from trader.exchange.errors import (
    AuthenticationError,
    MalformedResponse,
    NetworkError,
    OrderNotFound,
    OrderRejected,
    RateLimited,
    TradingDisabled,
    UnsafeAccount,
)
from trader.exchange.models import Credentials
from trader.exchange.transport import NoRedirect, ReadOnlyHTTPTransport


class AdapterTests(unittest.TestCase):
    def adapter(self, *responses, private=False, **kwargs):
        self.transport = FakeTransport(*responses)
        self.clock = FakeClock()
        return BinanceSpotAdapter(
            credentials=Credentials("fixture-key", "fixture-secret") if private else None,
            transport=self.transport,
            clock=self.clock.time,
            monotonic=self.clock.time,
            sleep=self.clock.sleep,
            **kwargs,
        )

    def test_public_ticker_both_symbols_and_decimal_precision(self):
        for symbol in ("BTCUSDT", "ETHUSDT"):
            adapter = self.adapter(response(ticker(symbol)))
            value = adapter.get_ticker(symbol)
            self.assertEqual(value.last_price, Decimal("50000.123456789"))
            self.assertEqual(value.spread, Decimal("1"))
            call = self.transport.calls[0]
            self.assertTrue(call[1].startswith(TESTNET_URL))
            self.assertNotIn("X-MBX-APIKEY", call[2])
            self.assertNotIn("signature", call[1])

    def test_paper_public_endpoint_and_no_private_credentials(self):
        adapter = self.adapter(response(ticker()), mode=TradingMode.PAPER)
        adapter.get_ticker("BTCUSDT")
        self.assertTrue(self.transport.calls[0][1].startswith(PUBLIC_URL))
        with self.assertRaises(ValueError):
            self.adapter(mode=TradingMode.PAPER, private=True)

    def test_live_is_not_implemented_even_with_configuration_flag(self):
        with self.assertRaises(TradingDisabled):
            BinanceSpotAdapter(mode=TradingMode.LIVE)

    def test_account_hmac_signature_and_clock_sync(self):
        adapter = self.adapter(
            response({"serverTime": NOW * 1000 + 10000}), response(account()), private=True
        )
        result = adapter.get_account()
        self.assertEqual(result.balances[0].locked, Decimal("0.0001"))
        self.assertFalse(result.key_permissions_verified)
        self.assertTrue(result.can_withdraw)  # Account capability is not key permission.
        call = self.transport.calls[1]
        query, signature = urlsplit(call[1]).query.rsplit("&signature=", 1)
        self.assertEqual(
            signature, hmac.new(b"fixture-secret", query.encode(), hashlib.sha256).hexdigest()
        )
        self.assertEqual(parse_qs(query)["timestamp"], [str(NOW * 1000 + 10000)])
        self.assertEqual(parse_qs(query)["recvWindow"], ["5000"])
        self.assertEqual(call[2]["X-MBX-APIKEY"], "fixture-key")
        self.assertNotIn("fixture-secret", repr(adapter))
        self.assertNotIn("fixture-key", repr(adapter._credentials))

    def test_balances(self):
        adapter = self.adapter(
            response({"serverTime": NOW * 1000}), response(account()), private=True
        )
        self.assertEqual(adapter.get_balances()[1].free, Decimal("950.001"))

    def test_missing_credentials_prevents_network(self):
        adapter = self.adapter()
        with self.assertRaises(AuthenticationError):
            adapter.get_account()
        self.assertEqual(self.transport.calls, [])

    def test_filters_preserve_market_flags(self):
        adapter = self.adapter(response(metadata()))
        result = adapter.get_exchange_info("BTCUSDT")
        self.assertEqual(result.lot_size.step_size, Decimal("0.00001"))
        self.assertEqual(result.market_lot_size.step_size, Decimal(0))
        self.assertTrue(result.notional_filters[0].apply_min_to_market)
        self.assertFalse(result.notional_filters[0].apply_max_to_market)
        self.assertEqual(result.notional_filters[0].average_price_minutes, 5)

    def test_min_notional_filter(self):
        data = metadata()
        data["symbols"][0]["filters"][-1] = {
            "filterType": "MIN_NOTIONAL",
            "minNotional": "5",
            "applyToMarket": True,
            "avgPriceMins": 0,
        }
        result = self.adapter(response(data)).get_exchange_info("BTCUSDT")
        self.assertIsNone(result.notional_filters[0].maximum)

    def test_malformed_metadata_rejected(self):
        data = metadata()
        data["symbols"][0]["filters"][0]["stepSize"] = "NaN"
        with self.assertRaises(MalformedResponse):
            self.adapter(response(data)).get_exchange_info("BTCUSDT")

    def test_candles_all_intervals(self):
        for interval in ("1m", "5m", "15m", "1h"):
            adapter = self.adapter(response(candles()))
            result = adapter.get_klines("BTCUSDT", interval, 50)
            self.assertEqual(result[0].volume, Decimal("12.5"))
            self.assertIsNotNone(result[0].timestamp.tzinfo)
            self.assertEqual(
                parse_qs(urlsplit(self.transport.calls[0][1]).query)["interval"], [interval]
            )

    def test_rejects_invalid_input_before_network(self):
        adapter = self.adapter()
        for symbol in ("SOLUSDT", "BTCUSDT&side=BUY"):
            with self.assertRaises(ValueError):
                adapter.get_ticker(symbol)
        for interval, limit in (("1s", 10), ("1m", 0), ("1m", 1001), ("1m", True)):
            with self.assertRaises(ValueError):
                adapter.get_klines("BTCUSDT", interval, limit)
        with self.assertRaises(ValueError):
            adapter.get_order("BTCUSDT", "bad&id")
        self.assertEqual(self.transport.calls, [])

    def test_non_spot_account_rejected(self):
        data = account()
        data["accountType"] = "MARGIN"
        adapter = self.adapter(response({"serverTime": NOW * 1000}), response(data), private=True)
        with self.assertRaises(UnsafeAccount):
            adapter.get_account()

    def test_bad_prices_and_wrong_symbols_rejected(self):
        for change in (
            {"lastPrice": "NaN"},
            {"lastPrice": "0"},
            {"bidPrice": "60000"},
            {"symbol": "ETHUSDT"},
            {"closeTime": True},
        ):
            adapter = self.adapter(response({**ticker(), **change}))
            with self.assertRaises(MalformedResponse):
                adapter.get_ticker("BTCUSDT")

    def test_read_retry_is_bounded(self):
        adapter = self.adapter(NetworkError(), NetworkError(), response(ticker()))
        self.assertEqual(adapter.get_ticker("BTCUSDT").symbol, "BTCUSDT")
        self.assertEqual(self.clock.sleeps, [0.5, 1.0])
        adapter = self.adapter(NetworkError(), NetworkError(), NetworkError())
        with self.assertRaises(NetworkError):
            adapter.get_ticker("BTCUSDT")
        self.assertEqual(len(self.transport.calls), 3)

    def test_rate_limit_honors_retry_after(self):
        adapter = self.adapter(
            response({"code": -1003}, 429, {"Retry-After": "2"}), response(ticker())
        )
        adapter.get_ticker("BTCUSDT")
        self.assertEqual(self.clock.sleeps, [2.0])

    def test_long_rate_limit_does_not_sleep_or_retry_early(self):
        adapter = self.adapter(response({"code": -1003}, 429, {"retry-after": "120"}))
        for _ in range(2):
            with self.assertRaises(RateLimited):
                adapter.get_ticker("BTCUSDT")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_http_418_does_not_retry(self):
        adapter = self.adapter(response({}, 418, {"Retry-After": "300"}))
        with self.assertRaises(RateLimited):
            adapter.get_ticker("BTCUSDT")
        self.assertEqual(len(self.transport.calls), 1)

    def test_auth_failure_is_sanitized_and_not_retried(self):
        adapter = self.adapter(response({"code": -2015, "msg": "fixture-secret"}, 401))
        with self.assertRaises(AuthenticationError) as caught:
            adapter.get_ticker("BTCUSDT")
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertEqual(len(self.transport.calls), 1)

    def test_exchange_not_found_and_rejection_are_distinct(self):
        for code, exception in ((-2013, OrderNotFound), (-1013, OrderRejected)):
            adapter = self.adapter(
                response({"serverTime": NOW * 1000}), response({"code": code}, 400), private=True
            )
            with self.assertRaises(exception):
                adapter.get_order("BTCUSDT", "id")
            self.assertEqual(len(self.transport.calls), 2)

    def test_default_transport_blocks_mutation_before_network(self):
        with patch("urllib.request.build_opener") as opener:
            transport = ReadOnlyHTTPTransport()
            for method in ("POST", "DELETE"):
                with self.assertRaises(TradingDisabled):
                    transport.request(method, TESTNET_URL + "/api/v3/order", {}, b"x", 10)
            opener.assert_not_called()
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other"))

    def test_client_ids_unique_and_valid_length(self):
        ids = {new_client_order_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)
        self.assertTrue(all(len(value) <= 36 for value in ids))

    def test_json_domain_serialization_has_no_credentials(self):
        result = self.adapter(response(ticker())).get_ticker("BTCUSDT")
        self.assertNotIn("secret", json.dumps(asdict(result), default=str))

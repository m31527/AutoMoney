"""Synthetic Binance payloads. No credentials or recorded private account data."""

import json
from urllib.parse import urlsplit

from trader.exchange.transport import Response

NOW = 1_700_000_000
CLIENT_ID = "ct_fixture_order"


def response(data, status=200, headers=None):
    return Response(status, headers or {}, json.dumps(data).encode())


def ticker(symbol="BTCUSDT"):
    return {
        "symbol": symbol,
        "lastPrice": "50000.123456789",
        "bidPrice": "50000",
        "askPrice": "50001",
        "volume": "1234.56",
        "closeTime": NOW * 1000,
    }


def metadata(symbol="BTCUSDT"):
    return {
        "symbols": [
            {
                "symbol": symbol,
                "baseAsset": symbol[:-4],
                "quoteAsset": "USDT",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
                "orderTypes": ["LIMIT", "MARKET"],
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.00001",
                        "maxQty": "100",
                        "stepSize": "0.00001",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": "0",
                        "maxQty": "10",
                        "stepSize": "0",
                    },
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": "5",
                        "maxNotional": "1000000",
                        "applyMinToMarket": True,
                        "applyMaxToMarket": False,
                        "avgPriceMins": 5,
                    },
                ],
            }
        ]
    }


def account():
    return {
        "accountType": "SPOT",
        "canTrade": True,
        "canWithdraw": True,
        "updateTime": (NOW - 86400) * 1000,
        "permissions": ["SPOT"],
        "balances": [
            {"asset": "BTC", "free": "0.001", "locked": "0.0001"},
            {"asset": "USDT", "free": "950.001", "locked": "2"},
        ],
    }


def candles():
    return [
        [
            (NOW - 60) * 1000,
            "50000",
            "50100",
            "49900",
            "50001",
            "12.5",
            NOW * 1000 - 1,
            "625000",
            100,
            "1",
            "50000",
            "0",
        ]
    ]


def order(status="FILLED", quantity="0.001", client_id=CLIENT_ID):
    executed = quantity if status == "FILLED" else "0.0004" if status == "PARTIALLY_FILLED" else "0"
    quote = "50" if status == "FILLED" else "20" if status == "PARTIALLY_FILLED" else "0"
    return {
        "symbol": "BTCUSDT",
        "orderId": 42,
        "clientOrderId": client_id,
        "side": "BUY",
        "type": "MARKET",
        "origQty": quantity,
        "executedQty": executed,
        "cummulativeQuoteQty": quote,
        "status": status,
        "updateTime": NOW * 1000,
    }


def fill(quantity="0.001", quote="50", trade_id=100):
    return {
        "symbol": "BTCUSDT",
        "orderId": 42,
        "id": trade_id,
        "qty": quantity,
        "price": "50000",
        "quoteQty": quote,
        "commission": "0.000001",
        "commissionAsset": "BTC",
        "time": NOW * 1000,
    }


class FakeClock:
    def __init__(self):
        self.value = float(NOW)
        self.sleeps = []

    def time(self):
        return self.value

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.value += duration


class FakeTransport:
    supports_mutations = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        if not self.responses:
            raise AssertionError("Unexpected HTTP call: " + method + " " + urlsplit(url).path)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def count(self, method):
        return sum(call[0] == method for call in self.calls)

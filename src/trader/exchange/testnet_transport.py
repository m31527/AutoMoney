"""Opt-in Testnet transport, separately origin/path/method restricted. Never production."""

import http.client
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

import certifi

from trader.exchange.errors import NetworkError, TradingDisabled
from trader.exchange.transport import NoRedirect, Response


class TestnetTransport:
    supports_mutations = True

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        target = urllib.parse.urlsplit(url)
        allowed = {
            "GET": {
                "/api/v3/time",
                "/api/v3/account",
                "/api/v3/ticker/24hr",
                "/api/v3/avgPrice",
                "/api/v3/exchangeInfo",
                "/api/v3/order",
                "/api/v3/openOrders",
                "/api/v3/myTrades",
            },
            "POST": {"/api/v3/order"},
        }
        if (
            target.scheme != "https"
            or target.netloc != "testnet.binance.vision"
            or target.fragment
            or target.path not in allowed.get(method, set())
        ):
            raise TradingDisabled()
        opener = urllib.request.build_opener(
            NoRedirect(),
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
        )
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(2097153)
                if len(raw) > 2097152:
                    raise NetworkError()
                return Response(response.status, dict(response.headers), raw)
        except urllib.error.HTTPError as error:
            try:
                return Response(error.code, dict(error.headers), error.read(2097152))
            finally:
                error.close()
        except (OSError, http.client.HTTPException, ValueError):
            raise NetworkError() from None

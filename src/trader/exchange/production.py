"""Separate production adapter: read-only until an exact, one-use order is armed."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from trader.config import TradingMode
from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.errors import TradingDisabled
from trader.exchange.models import Credentials
from trader.exchange.testnet_transport import TestnetTransport
from trader.exchange.transport import Response


class ProductionTransport(TestnetTransport):
    origin = "api.binance.com"
    extra_get = {"/sapi/v1/account/apiRestrictions"}

    def __init__(self) -> None:
        self.grant: dict[str, str] | None = None

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        if method != "GET":
            grant, self.grant = self.grant, None
            params = parse_qs((body or b"").decode(), strict_parsing=True)
            if (
                method != "POST"
                or urlsplit(url).path != "/api/v3/order"
                or not grant
                or any(params.get(k) != [v] for k, v in grant.items())
                or set(params) != set(grant) | {"timestamp", "recvWindow", "signature"}
            ):
                raise TradingDisabled()
        return super().request(method, url, headers, body, timeout)


class ProductionAdapter(BinanceSpotAdapter):
    def __init__(self, credentials: Credentials, **kwargs: Any) -> None:
        super().__init__(credentials=credentials, symbols=("BTCUSDT",), **kwargs)
        self.mode = TradingMode.LIVE
        self._private_modes = (TradingMode.LIVE,)
        self._base_url = "https://api.binance.com"

    def check_permissions(self) -> dict[str, bool]:
        raw = self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True)
        expected = {
            "ipRestrict": True,
            "enableReading": True,
            "enableSpotAndMarginTrading": True,
            "enableWithdrawals": False,
            "enableInternalTransfer": False,
            "permitsUniversalTransfer": False,
            "enableMargin": False,
            "enableFutures": False,
            "enableVanillaOptions": False,
            "enablePortfolioMarginTrading": False,
            "enableFixApiTrade": False,
        }
        if not isinstance(raw, dict):
            raise ValueError("INVALID_KEY_PERMISSIONS")
        checks = {k: raw.get(k) is v for k, v in expected.items()}
        if not all(checks.values()):
            raise ValueError("UNSAFE_OR_MISSING_KEY_PERMISSIONS")
        return checks

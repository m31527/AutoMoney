"""Binance Spot Testnet REST adapter. Network mutations remain disabled in Phase B."""

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from typing import Any, TypeVar
from urllib.parse import urlencode
from uuid import uuid4

from trader.config import TradingMode
from trader.exchange import parsing
from trader.exchange.errors import (
    AmbiguousOrder,
    AuthenticationError,
    ExchangeError,
    MalformedResponse,
    NetworkError,
    OrderNotFound,
    OrderRejected,
    RateLimited,
    TemporaryError,
    TradingDisabled,
    UnsafeAccount,
)
from trader.exchange.models import (
    Account,
    AveragePrice,
    Balance,
    Credentials,
    Fill,
    Order,
    Reconciliation,
    SymbolInfo,
    Ticker,
)
from trader.exchange.transport import ReadOnlyHTTPTransport, Transport
from trader.models import Candle
from trader.safety.kill_switch import KillSwitch
from trader.storage.exchange_journal import ExchangeJournal

T = TypeVar("T")
TESTNET_URL = "https://testnet.binance.vision"
PUBLIC_URL = "https://data-api.binance.vision"


def new_client_order_id() -> str:
    return "ct_" + uuid4().hex


class BinanceSpotAdapter:
    def __init__(
        self,
        mode: TradingMode = TradingMode.TESTNET,
        credentials: Credentials | None = None,
        *,
        transport: Transport | None = None,
        journal: ExchangeJournal | None = None,
        kill_switch: KillSwitch | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
    ) -> None:
        if mode not in (TradingMode.PAPER, TradingMode.TESTNET):
            raise TradingDisabled()
        if not symbols or any(
            s not in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT") for s in symbols
        ):
            raise ValueError("Unsupported symbol whitelist")
        if mode == TradingMode.PAPER and credentials is not None:
            raise ValueError("PAPER public market data must not receive credentials")
        self.mode = mode
        self._credentials = credentials
        self.transport = transport or ReadOnlyHTTPTransport()
        self.journal = journal
        self.kill_switch = kill_switch
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.symbols = symbols
        self._offset_ms = 0
        self._synced_at: float | None = None
        self._blocked_until = 0.0
        self._failed_requests = 0

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _symbol(self, symbol: str) -> None:
        if symbol not in self.symbols:
            raise ValueError("Symbol is not whitelisted")

    def _client_id(self, client_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", client_id):
            raise ValueError("Invalid client order ID")

    def _trip(self, code: str) -> None:
        if self.kill_switch is not None:
            self.kill_switch.trip(code)

    def _parse(self, decode: Callable[[], T]) -> T:
        try:
            return decode()
        except UnsafeAccount:
            self._trip(UnsafeAccount.code)
            raise
        except (KeyError, IndexError, TypeError, ValueError, OverflowError, MalformedResponse):
            self._trip(MalformedResponse.code)
            raise MalformedResponse() from None

    def sync_time(self) -> None:
        before = self.clock()
        data = self._request("GET", "/api/v3/time")
        after = self.clock()
        server_ms = self._parse(lambda: parsing.integer(parsing.obj(data)["serverTime"]))
        if after - before > 5 or after < before:
            raise TemporaryError()
        self._offset_ms = server_ms - int((before + after) * 500)
        self._synced_at = self.monotonic()

    def _request(
        self, method: str, path: str, params: dict[str, str] | None = None, *, signed: bool = False
    ) -> Any:
        if self.monotonic() < self._blocked_until:
            raise RateLimited()
        if method != "GET" and not self.transport.supports_mutations:
            raise TradingDisabled()
        if signed:
            if self._credentials is None or self.mode != TradingMode.TESTNET:
                raise AuthenticationError()
            if self._synced_at is None or self.monotonic() - self._synced_at > 60:
                self.sync_time()
        for attempt in range(3 if method == "GET" else 1):
            headers: dict[str, str] = {"Accept": "application/json"}
            parameters = dict(params or {})
            if signed:
                assert self._credentials is not None
                parameters.update(
                    timestamp=str(int(self.clock() * 1000) + self._offset_ms), recvWindow="5000"
                )
            query = urlencode(parameters)
            if signed:
                assert self._credentials is not None
                signature = hmac.new(
                    self._credentials.api_secret.encode(), query.encode(), hashlib.sha256
                ).hexdigest()
                query += "&signature=" + signature
                headers["X-MBX-APIKEY"] = self._credentials.api_key
            url = (TESTNET_URL if self.mode == TradingMode.TESTNET else PUBLIC_URL) + path
            body = None
            if method == "GET":
                if query:
                    url += "?" + query
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body = query.encode()
            delay = 0.5 * 2**attempt
            if method == "POST" and (self.kill_switch is None or self.kill_switch.active):
                raise TradingDisabled()
            try:
                response = self.transport.request(method, url, headers, body, 10.0)
            except (NetworkError, TimeoutError, ConnectionError):
                error: ExchangeError = NetworkError()
            else:
                try:
                    data = json.loads(response.body, parse_float=Decimal)
                except (ValueError, UnicodeError):
                    data = None
                code = data.get("code") if isinstance(data, dict) else None
                if response.status in (401, 403) or code in (-2014, -2015, -1022):
                    self._trip(AuthenticationError.code)
                    raise AuthenticationError()
                if response.status in (418, 429):
                    retry = next(
                        (v for k, v in response.headers.items() if k.lower() == "retry-after"), "1"
                    )
                    try:
                        wait = float(retry)
                        if not 0 <= wait <= 86400 * 7:
                            raise ValueError
                    except ValueError:
                        wait = 60.0
                    delay = max(delay, wait)
                    self._blocked_until = self.monotonic() + delay
                    error = RateLimited()
                    if response.status == 418 or delay > 5:
                        raise error
                elif response.status >= 500 or code in (-1006, -1007):
                    error = TemporaryError()
                elif code == -2013:
                    raise OrderNotFound()
                elif not 200 <= response.status < 300 or (type(code) is int and code < 0):
                    raise OrderRejected()
                elif data is None:
                    self._trip(MalformedResponse.code)
                    if method != "GET":
                        raise AmbiguousOrder()
                    raise MalformedResponse()
                else:
                    self._failed_requests = 0
                    return data
            if method != "GET":
                raise AmbiguousOrder() from None
            if attempt == 2:
                self._failed_requests += 1
                if self._failed_requests >= 3:
                    self._trip("REPEATED_EXCHANGE_FAILURES")
                raise error
            self.sleep(delay)
        raise AssertionError("Unreachable")

    def get_account(self) -> Account:
        data = self._request("GET", "/api/v3/account", signed=True)
        return self._parse(lambda: parsing.account(data, self._now()))

    def get_balances(self) -> tuple[Balance, ...]:
        return self.get_account().balances

    def get_average_price(self, symbol: str) -> AveragePrice:
        self._symbol(symbol)
        data = self._request("GET", "/api/v3/avgPrice", {"symbol": symbol})
        return self._parse(
            lambda: AveragePrice(
                symbol,
                parsing.number(parsing.obj(data)["price"], positive=True),
                parsing.integer(data["mins"]),
                parsing.timestamp(data["closeTime"]),
                self._now(),
            )
        )

    def get_ticker(self, symbol: str) -> Ticker:
        self._symbol(symbol)
        data = self._request("GET", "/api/v3/ticker/24hr", {"symbol": symbol})
        return self._parse(lambda: parsing.ticker(data, symbol, self._now()))

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> tuple[Candle, ...]:
        self._symbol(symbol)
        if (
            interval not in ("1m", "5m", "15m", "1h")
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Invalid candle interval or limit")
        data = self._request(
            "GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": str(limit)}
        )
        return self._parse(lambda: parsing.candles(data))

    def get_exchange_info(self, symbol: str) -> SymbolInfo:
        self._symbol(symbol)
        data = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        return self._parse(lambda: parsing.symbol_info(data, symbol, self._now()))

    def get_order(self, symbol: str, client_order_id: str) -> Order:
        self._symbol(symbol)
        self._client_id(client_order_id)
        data = self._request(
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
            signed=True,
        )
        return self._parse(lambda: parsing.order(data, symbol, client_order_id))

    def get_open_orders(self, symbol: str) -> tuple[Order, ...]:
        self._symbol(symbol)
        data = self._request("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)
        return self._parse(
            lambda: tuple(parsing.order(item, symbol) for item in parsing.array(data))
        )

    def get_fills(self, symbol: str, order_id: int) -> tuple[Fill, ...]:
        self._symbol(symbol)
        if type(order_id) is not int or order_id < 0:
            raise ValueError("Invalid exchange order ID")
        result: dict[int, Fill] = {}
        from_id = 0
        for _ in range(10):
            data = self._request(
                "GET",
                "/api/v3/myTrades",
                {
                    "symbol": symbol,
                    "orderId": str(order_id),
                    "fromId": str(from_id),
                    "limit": "1000",
                },
                signed=True,
            )
            page = self._parse(partial(parsing.fills, data, symbol, order_id))
            if any(fill.trade_id < from_id for fill in page):
                self._trip(MalformedResponse.code)
                raise MalformedResponse()
            result.update((fill.trade_id, fill) for fill in page)
            if len(page) < 1000:
                return tuple(result[key] for key in sorted(result))
            from_id = max(fill.trade_id for fill in page) + 1
        raise TemporaryError()

    def reconcile(self, symbol: str, client_order_id: str) -> Reconciliation:
        expected = self.journal.expected_request(client_order_id) if self.journal else None
        try:
            order = self.get_order(symbol, client_order_id)
            if expected is not None and (
                order.symbol != expected["symbol"]
                or order.side != expected["side"]
                or order.order_type != expected["type"]
                or order.requested_quantity != Decimal(expected["quantity"])
            ):
                raise MalformedResponse()
            fills = (
                self.get_fills(symbol, order.exchange_order_id) if order.executed_quantity else ()
            )
        except ExchangeError:
            self._trip("RECONCILIATION_REQUIRED")
            raise AmbiguousOrder() from None
        total_quantity = sum((fill.quantity for fill in fills), Decimal(0))
        total_quote = sum((fill.quote_quantity for fill in fills), Decimal(0))
        complete = (
            total_quantity == order.executed_quantity
            and total_quote == order.cumulative_quote_quantity
        )
        result = Reconciliation(order, fills, complete)
        if self.journal is not None and expected is not None:
            try:
                self.journal.save_reconciliation(result)
            except MalformedResponse:
                self._trip("CONFLICTING_FILL_RECONCILIATION")
                raise
        if not complete:
            self._trip("INCOMPLETE_FILL_RECONCILIATION")
        return result

    def place_order(self, symbol: str, side: str, quantity: Decimal, client_order_id: str) -> Order:
        self._symbol(symbol)
        self._client_id(client_order_id)
        if (
            side not in ("BUY", "SELL")
            or not isinstance(quantity, Decimal)
            or not quantity.is_finite()
            or quantity <= 0
        ):
            raise ValueError("Only positive-quantity BUY/SELL spot orders are allowed")
        # Only mock transports can exercise this path in Phase B. The built-in transport
        # has an independent mutation block; no environment flag enables network trading.
        if (
            not self.transport.supports_mutations
            or self.mode != TradingMode.TESTNET
            or self.journal is None
            or self.kill_switch is None
        ):
            raise TradingDisabled()
        if self.kill_switch.active:
            raise TradingDisabled()
        if self._credentials is None:
            raise AuthenticationError()
        request = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": format(quantity.normalize(), "f"),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        if not self.journal.claim(client_order_id, request):
            return self.reconcile(symbol, client_order_id).order
        try:
            data = self._request("POST", "/api/v3/order", request, signed=True)
            order = self._parse(lambda: parsing.order(data, symbol, client_order_id))
            if (
                order.side != side
                or order.requested_quantity != quantity
                or order.order_type != "MARKET"
            ):
                raise MalformedResponse()
        except (AmbiguousOrder, MalformedResponse):
            self.journal.observe(client_order_id, {"state": "UNKNOWN"})
            return self.reconcile(symbol, client_order_id).order
        except ExchangeError as error:
            self.journal.observe(client_order_id, {"error": error.code})
            raise
        self.journal.observe(client_order_id, {"order": asdict(order)})
        # Query myTrades: an order response alone does not establish complete fee accounting.
        return self.reconcile(symbol, client_order_id).order

    def cancel_order(self, symbol: str, client_order_id: str) -> Order:
        self._symbol(symbol)
        self._client_id(client_order_id)
        if not self.transport.supports_mutations:
            raise TradingDisabled()
        # Binance replaces clientOrderId on cancellation. Know that ID before
        # submitting so a lost response can be reconciled without retrying DELETE.
        cancel_id = "cx_" + hashlib.sha256(client_order_id.encode()).hexdigest()[:32]
        try:
            self._request(
                "DELETE",
                "/api/v3/order",
                {
                    "symbol": symbol,
                    "origClientOrderId": client_order_id,
                    "newClientOrderId": cancel_id,
                },
                signed=True,
            )
        except AmbiguousOrder:
            pass
        try:
            try:
                return self.get_order(symbol, cancel_id)
            except OrderNotFound:
                # Cancellation may have lost the race to a fill. Report the actual state.
                return self.get_order(symbol, client_order_id)
        except ExchangeError:
            self._trip("CANCEL_RECONCILIATION_REQUIRED")
            raise AmbiguousOrder() from None

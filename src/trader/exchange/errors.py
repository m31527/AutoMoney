"""Sanitized errors: never include URLs, headers, response bodies or credentials."""


class ExchangeError(RuntimeError):
    code = "EXCHANGE_ERROR"

    def __init__(self) -> None:
        super().__init__(self.code)


class NetworkError(ExchangeError):
    code = "EXCHANGE_NETWORK_ERROR"


class TemporaryError(ExchangeError):
    code = "EXCHANGE_TEMPORARILY_UNAVAILABLE"


class RateLimited(ExchangeError):
    code = "EXCHANGE_RATE_LIMITED"


class AuthenticationError(ExchangeError):
    code = "EXCHANGE_AUTHENTICATION_FAILED"

    def __init__(self, *, http_status: int | None = None, exchange_code: int | None = None) -> None:
        super().__init__()
        self.http_status = http_status
        self.exchange_code = exchange_code
        self.hint = {
            -2014: "API_KEY_FORMAT_INVALID",
            -2015: "CHECK_TESTNET_KEY_IP_AND_PERMISSIONS",
            -1022: "CHECK_MATCHING_HMAC_KEY_AND_SECRET",
        }.get(
            exchange_code if exchange_code is not None else 0, "HTTP_ACCESS_DENIED_OR_AUTH_FAILURE"
        )


class MalformedResponse(ExchangeError):
    code = "EXCHANGE_MALFORMED_RESPONSE"


class OrderNotFound(ExchangeError):
    code = "EXCHANGE_ORDER_NOT_FOUND"


class OrderRejected(ExchangeError):
    code = "EXCHANGE_REQUEST_REJECTED"


class AmbiguousOrder(ExchangeError):
    code = "ORDER_STATE_UNKNOWN_RECONCILIATION_REQUIRED"


class TradingDisabled(ExchangeError):
    code = "NETWORK_TRADING_DISABLED_UNTIL_RISK_ENGINE"


class UnsafeAccount(ExchangeError):
    code = "EXCHANGE_ACCOUNT_NOT_SPOT"


class DuplicateOrderConflict(ExchangeError):
    code = "CLIENT_ORDER_ID_REUSED_WITH_DIFFERENT_REQUEST"

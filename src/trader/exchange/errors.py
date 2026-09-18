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

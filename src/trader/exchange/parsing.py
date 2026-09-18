"""Validate external data at the adapter boundary; never fabricate missing balances/prices."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from trader.exchange.errors import MalformedResponse, UnsafeAccount
from trader.exchange.models import (
    Account,
    Balance,
    Fill,
    LotFilter,
    NotionalFilter,
    Order,
    SymbolInfo,
    Ticker,
)
from trader.models import Candle


def obj(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MalformedResponse()
    return value


def array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise MalformedResponse()
    return value


def text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedResponse()
    return value


def number(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MalformedResponse()
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise MalformedResponse() from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise MalformedResponse()
    return result


def integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise MalformedResponse()
    return int(value)


def boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise MalformedResponse()
    return bool(value)


def timestamp(value: Any) -> datetime:
    return datetime.fromtimestamp(integer(value) / 1000, UTC)


def account(raw: Any, now: datetime) -> Account:
    data = obj(raw)
    if data["accountType"] != "SPOT":
        raise UnsafeAccount()
    balances = tuple(
        Balance(text(b["asset"]), number(b["free"]), number(b["locked"]))
        for b in (obj(item) for item in array(data["balances"]))
    )
    if len({b.asset for b in balances}) != len(balances):
        raise MalformedResponse()
    return Account(
        now,
        timestamp(data["updateTime"]),
        "SPOT",
        boolean(data["canTrade"]),
        boolean(data["canWithdraw"]),
        balances,
    )


def ticker(raw: Any, symbol: str, now: datetime) -> Ticker:
    data = obj(raw)
    if data["symbol"] != symbol:
        raise MalformedResponse()
    result = Ticker(
        symbol,
        timestamp(data["closeTime"]),
        now,
        number(data["lastPrice"], positive=True),
        number(data["bidPrice"], positive=True),
        number(data["askPrice"], positive=True),
        number(data["volume"]),
    )
    if result.bid > result.ask:
        raise MalformedResponse()
    return result


def candles(raw: Any) -> tuple[Candle, ...]:
    result: list[Candle] = []
    for item in array(raw):
        row = array(item)
        if len(row) < 7:
            raise MalformedResponse()
        candle = Candle(
            timestamp(row[0]),
            number(row[1], positive=True),
            number(row[2], positive=True),
            number(row[3], positive=True),
            number(row[4], positive=True),
            number(row[5]),
        )
        if not (
            candle.low <= candle.open <= candle.high and candle.low <= candle.close <= candle.high
        ):
            raise MalformedResponse()
        if result and result[-1].timestamp >= candle.timestamp:
            raise MalformedResponse()
        result.append(candle)
    if not result:
        raise MalformedResponse()
    return tuple(result)


def lot(raw: dict[str, Any]) -> LotFilter:
    result = LotFilter(
        number(raw["minQty"]), number(raw["maxQty"], positive=True), number(raw["stepSize"])
    )
    if result.min_quantity > result.max_quantity:
        raise MalformedResponse()
    return result


def symbol_info(raw: Any, symbol: str, now: datetime) -> SymbolInfo:
    rows = array(obj(raw)["symbols"])
    if len(rows) != 1:
        raise MalformedResponse()
    data = obj(rows[0])
    if data["symbol"] != symbol or data["quoteAsset"] != "USDT":
        raise MalformedResponse()
    if data["baseAsset"] != symbol.removesuffix("USDT"):
        raise MalformedResponse()
    filters = {}
    for item in array(data["filters"]):
        item = obj(item)
        name = text(item["filterType"])
        if name in filters:
            raise MalformedResponse()
        filters[name] = item
    notional = []
    if "MIN_NOTIONAL" in filters:
        value = filters["MIN_NOTIONAL"]
        notional.append(
            NotionalFilter(
                number(value["minNotional"]),
                None,
                boolean(value["applyToMarket"]),
                False,
                integer(value["avgPriceMins"]),
            )
        )
    if "NOTIONAL" in filters:
        value = filters["NOTIONAL"]
        minimum, maximum = number(value["minNotional"]), number(value["maxNotional"])
        if maximum < minimum:
            raise MalformedResponse()
        notional.append(
            NotionalFilter(
                minimum,
                maximum,
                boolean(value["applyMinToMarket"]),
                boolean(value["applyMaxToMarket"]),
                integer(value["avgPriceMins"]),
            )
        )
    if not notional:
        raise MalformedResponse()
    return SymbolInfo(
        symbol,
        text(data["baseAsset"]),
        "USDT",
        text(data["status"]),
        boolean(data["isSpotTradingAllowed"]),
        "MARKET" in array(data["orderTypes"]),
        lot(filters["LOT_SIZE"]),
        lot(filters["MARKET_LOT_SIZE"]) if "MARKET_LOT_SIZE" in filters else None,
        tuple(notional),
        now,
    )


def order(raw: Any, symbol: str, client_id: str | None = None) -> Order:
    data = obj(raw)
    if data["symbol"] != symbol or (client_id is not None and data["clientOrderId"] != client_id):
        raise MalformedResponse()
    if data["side"] not in ("BUY", "SELL") or data["status"] not in (
        "NEW",
        "PENDING_NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "PENDING_CANCEL",
        "REJECTED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
    ):
        raise MalformedResponse()
    result = Order(
        symbol,
        integer(data["orderId"]),
        text(data["clientOrderId"]),
        data["side"],
        text(data["type"]),
        data["status"],
        number(data["origQty"], positive=True),
        number(data["executedQty"]),
        number(data["cummulativeQuoteQty"]),
        timestamp(data.get("updateTime", data.get("transactTime"))),
    )
    if result.executed_quantity > result.requested_quantity:
        raise MalformedResponse()
    if result.status == "FILLED" and result.executed_quantity != result.requested_quantity:
        raise MalformedResponse()
    return result


def fills(raw: Any, symbol: str, order_id: int) -> tuple[Fill, ...]:
    result = []
    for item in array(raw):
        data = obj(item)
        if data["symbol"] != symbol or data["orderId"] != order_id:
            raise MalformedResponse()
        result.append(
            Fill(
                symbol,
                order_id,
                integer(data["id"]),
                number(data["qty"], positive=True),
                number(data["price"], positive=True),
                number(data["quoteQty"], positive=True),
                number(data["commission"]),
                text(data["commissionAsset"]),
                timestamp(data["time"]),
            )
        )
    if len({f.trade_id for f in result}) != len(result):
        raise MalformedResponse()
    return tuple(result)

"""Market quantity rounding and the supported Binance notional/lot filters."""

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from math import lcm

from trader.exchange.models import AveragePrice, SymbolInfo, Ticker
from trader.portfolio.valuation import finite
from trader.risk.engine import aware


class FilterRejected(ValueError):
    pass


def market_quantity(
    maximum: Decimal,
    metadata: SymbolInfo,
    ticker: Ticker,
    average: AveragePrice | None,
    *,
    now: datetime,
    max_age: int,
) -> Decimal:
    def fresh(timestamp: datetime) -> bool:
        return aware(timestamp) and 0 <= (now - timestamp).total_seconds() <= max_age

    if (
        metadata.symbol != ticker.symbol
        or metadata.base_asset + metadata.quote_asset != ticker.symbol
        or metadata.quote_asset != "USDT"
        or metadata.status != "TRADING"
        or not metadata.spot_allowed
        or not metadata.market_allowed
        or not fresh(metadata.fetched_at)
        or not finite(maximum)
        or maximum <= 0
    ):
        raise FilterRejected("SYMBOL_FILTER_INVALID")
    lots = [metadata.lot_size] + ([metadata.market_lot_size] if metadata.market_lot_size else [])
    if any(
        not finite(v) for lot in lots for v in (lot.min_quantity, lot.max_quantity, lot.step_size)
    ):
        raise FilterRejected("LOT_FILTER_INVALID")
    steps = [lot.step_size for lot in lots if lot.step_size > 0]
    if not steps:
        raise FilterRejected("QUANTITY_PRECISION_UNKNOWN")
    scale: int = 10 ** max(max(0, -int(step.as_tuple().exponent)) for step in steps)
    step = Decimal(lcm(*(int(s * scale) for s in steps))) / scale
    quantity = (maximum / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity <= 0 or any(not lot.min_quantity <= quantity <= lot.max_quantity for lot in lots):
        raise FilterRejected("LOT_SIZE")
    if not metadata.notional_filters:
        raise FilterRejected("NOTIONAL_FILTER_MISSING")
    for rule in metadata.notional_filters:
        if not rule.apply_min_to_market and not rule.apply_max_to_market:
            continue
        if rule.average_price_minutes == 0:
            price = ticker.last_price
        else:
            if (
                average is None
                or average.symbol != ticker.symbol
                or average.minutes != rule.average_price_minutes
                or not fresh(average.timestamp)
                or not fresh(average.fetched_at)
            ):
                raise FilterRejected("AVERAGE_PRICE_UNAVAILABLE")
            price = average.price
        if not finite(price) or price <= 0 or not finite(rule.minimum):
            raise FilterRejected("NOTIONAL_FILTER_INVALID")
        notional = quantity * price
        if rule.apply_min_to_market and notional < rule.minimum:
            raise FilterRejected("MIN_NOTIONAL")
        if rule.apply_max_to_market and (
            rule.maximum is None or not finite(rule.maximum) or notional > rule.maximum
        ):
            raise FilterRejected("MAX_NOTIONAL")
    return quantity

from decimal import Decimal


def sma(values: tuple[Decimal, ...], period: int) -> Decimal:
    if type(period) is not int or period <= 0 or len(values) < period:
        raise ValueError("Insufficient indicator history")
    if any(not value.is_finite() or value <= 0 for value in values):
        raise ValueError("Invalid indicator price")
    return sum(values[-period:], Decimal(0)) / period

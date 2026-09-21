"""Advisory AI budgets, using the same adverse-price bounds as PAPER risk checks."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from trader.config import RiskConfig
from trader.models import MarketSnapshot


@dataclass(frozen=True)
class TradeBudget:
    max_buy_notional_usd: Decimal
    max_sell_notional_usd: Decimal
    max_order_notional_usd: Decimal
    remaining_total_exposure_usd: Decimal
    max_symbol_allocation_pct: Decimal
    cash_after_fee_reserve_usd: Decimal


def trade_budget(
    snapshot: MarketSnapshot, config: RiskConfig, total_exposure: Decimal
) -> TradeBudget:
    zero = Decimal(0)
    upper = max(snapshot.ask, snapshot.last_price) * (1 + config.estimated_slippage_rate)
    fraction = config.max_symbol_allocation_pct / 100
    equity = snapshot.available_quote_balance + total_exposure
    existing = snapshot.position * snapshot.last_price
    cash = snapshot.available_quote_balance / (1 + config.estimated_fee_rate)
    remaining = max(zero, config.max_total_position_usd - total_exposure)
    # Solve existing + N <= fraction * (equity - adverse mark loss - fee).
    loss_rate = (upper - snapshot.last_price) / upper + config.estimated_fee_rate
    allocation = max(zero, (fraction * equity - existing) / (1 + fraction * loss_rate))
    buy = max(zero, min(config.max_order_notional_usd, cash, remaining, allocation))
    sell = snapshot.position * upper
    if config.exit_policy_version == 1:
        sell /= 1 + config.estimated_fee_rate
    sell = max(zero, min(config.max_order_notional_usd, sell))
    return TradeBudget(
        buy.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN),
        sell.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN),
        config.max_order_notional_usd,
        remaining,
        config.max_symbol_allocation_pct,
        cash,
    )

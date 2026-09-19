"""Pure deterministic preflight. No exchange, order submission or mutable risk settings."""

from datetime import UTC, datetime
from decimal import Decimal

from trader.config import RiskConfig
from trader.models import Action, TradeProposal
from trader.portfolio.valuation import finite, value_portfolio
from trader.risk.models import DayState, RiskContext, RiskEvaluation, RuleCheck, TradeHistory

ZERO = Decimal(0)
BPS = Decimal(10000)


def aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def evaluate(
        self,
        proposal: TradeProposal,
        context: RiskContext,
        day: DayState | None,
        history: TradeHistory,
        *,
        now: datetime,
        killed: bool,
    ) -> RiskEvaluation:
        if not aware(now):
            raise ValueError("Risk evaluation requires timezone-aware time")
        now = now.astimezone(UTC)
        cfg = self.config
        checks: list[RuleCheck] = []

        def check(rule: str, passed: bool) -> None:
            checks.append(RuleCheck(rule, passed))

        valid = (
            isinstance(proposal.action, Action)
            and finite(proposal.confidence)
            and proposal.confidence <= 1
            and finite(proposal.requested_notional_usd)
            and type(proposal.time_horizon_minutes) is int
            and proposal.time_horizon_minutes > 0
            and (
                proposal.requested_notional_usd == 0
                if proposal.action == Action.HOLD
                else proposal.requested_notional_usd > 0
            )
        )
        check("VALID_PROPOSAL", valid)
        # Free-text AI conditions cannot be silently treated as satisfied.
        check("NO_UNINTERPRETED_CONDITIONS", proposal.invalid_if == ())
        check("SYMBOL_WHITELIST", proposal.symbol in cfg.symbols)
        check(
            "SPOT_ONLY",
            proposal.product == "SPOT"
            and finite(proposal.leverage)
            and proposal.leverage == 1
            and proposal.shorting is False
            and proposal.withdrawal is False,
        )
        check("KILL_SWITCH_CLEAR", killed is False)
        check(
            "ACCOUNT_VERIFIED",
            context.connectivity_verified is True
            and context.account_reconciled is True
            and context.account.account_type == "SPOT"
            and context.account.can_trade is True
            and context.account.key_permissions_verified is True,
        )
        check("HISTORY_COMPLETE", context.history_complete is True)
        check("NO_PENDING_ORDERS", context.no_pending_orders is True)

        def fresh(timestamp: datetime) -> bool:
            return (
                aware(timestamp)
                and 0 <= (now - timestamp).total_seconds() <= cfg.max_data_age_seconds
            )

        # Account updateTime describes the last change, not the last successful observation.
        check("ACCOUNT_DATA_FRESH", fresh(context.account.fetched_at))
        market_valid = bool(context.tickers) and all(
            finite(t.last_price)
            and t.last_price > 0
            and finite(t.bid)
            and t.bid > 0
            and finite(t.ask)
            and t.ask >= t.bid
            and finite(t.volume_24h)
            and fresh(t.timestamp)
            and fresh(t.fetched_at)
            for t in context.tickers
        )
        check("MARKET_DATA_FRESH_VALID", market_valid)
        valuation = None
        try:
            valuation = value_portfolio(context.account, context.tickers)
        except ValueError:
            pass
        check("PORTFOLIO_VALUED", valuation is not None)
        day_valid = (
            day is not None
            and day.day == now.date()
            and finite(day.opening_equity)
            and day.opening_equity > 0
        )
        check("DAY_BASELINE_AVAILABLE", day_valid)
        pnl = valuation.equity - day.opening_equity if day_valid and day and valuation else None
        loss_triggered = bool(
            pnl is not None
            and pnl <= -cfg.max_daily_loss_usd
            and market_valid
            and fresh(context.account.fetched_at)
            and context.account_reconciled is True
        )
        stopped = bool(day and day.loss_latched) or loss_triggered
        check("DAILY_BUY_LOSS_STOP", not (proposal.action == Action.BUY and stopped))
        history_valid = (
            type(history.trades_today) is int
            and history.trades_today >= 0
            and (
                history.last_trade_at is None
                or (aware(history.last_trade_at) and history.last_trade_at <= now)
            )
            and (history.trades_today == 0 or history.last_trade_at is not None)
        )
        check("TRADE_HISTORY_VALID", history_valid)
        check("DAILY_TRADE_LIMIT", history_valid and history.trades_today < cfg.max_trades_per_day)
        cooldown_clear = history.last_trade_at is None or (
            aware(history.last_trade_at)
            and (now - history.last_trade_at).total_seconds() >= cfg.min_minutes_between_trades * 60
        )
        check("COOLDOWN", cooldown_clear)
        check("CONFIDENCE", valid and proposal.confidence >= cfg.minimum_confidence)
        check(
            "ORDER_NOTIONAL_LIMIT",
            valid and proposal.requested_notional_usd <= cfg.max_order_notional_usd,
        )

        ticker = next((t for t in context.tickers if t.symbol == proposal.symbol), None)
        check("SYMBOL_PRICE_AVAILABLE", ticker is not None)
        quantity = fee = notional = ZERO
        costs: Decimal | None = None
        calculated = (
            valid
            and proposal.action in (Action.BUY, Action.SELL)
            and valuation is not None
            and ticker is not None
            and market_valid
        )
        funds_ok = total_ok = allocation_ok = costs_ok = False
        if calculated and valuation is not None and ticker is not None:
            # Bound quantity using an adverse upper price; fees are reserved separately.
            upper = max(ticker.ask, ticker.last_price) * (1 + cfg.estimated_slippage_rate)
            lower = min(ticker.bid, ticker.last_price) * (1 - cfg.estimated_slippage_rate)
            notional = proposal.requested_notional_usd
            quantity = notional / upper
            fee = notional * cfg.estimated_fee_rate
            costs = (upper - lower) / ticker.last_price * BPS + 2 * cfg.estimated_fee_rate * BPS
            costs_ok = (
                finite(context.expected_edge_bps)
                and costs <= cfg.max_execution_cost_bps
                and context.expected_edge_bps - costs >= cfg.minimum_net_edge_bps
            )
            if proposal.action == Action.SELL and cfg.exit_policy_version == 2:
                costs_ok = costs <= cfg.max_execution_cost_bps
            position = next((p for p in valuation.positions if p.symbol == proposal.symbol), None)
            existing = position.exposure if position else ZERO
            if proposal.action == Action.BUY:
                funds_ok = notional + fee <= valuation.free_cash
                projected_total = valuation.exposure + notional
                projected_symbol = existing + notional
                projected_equity = valuation.equity - (upper - ticker.last_price) * quantity - fee
            else:
                # Reserve base-asset fee capacity as well; fee asset is not assumed to be USDT.
                funds_ok = (
                    position is not None
                    and quantity * (1 + cfg.estimated_fee_rate) <= position.free_quantity
                )
                if cfg.exit_policy_version == 2:
                    # PAPER fees are charged in quote currency, not sold base quantity.
                    quantity = min(quantity, position.free_quantity) if position else ZERO
                    funds_ok = position is not None and ZERO < quantity <= position.free_quantity
                projected_total = valuation.exposure - quantity * ticker.last_price
                projected_symbol = existing - quantity * ticker.last_price
                projected_equity = valuation.equity - (ticker.last_price - lower) * quantity - fee
            total_ok = ZERO <= projected_total <= cfg.max_total_position_usd
            allocation_ok = (
                ZERO <= projected_symbol <= projected_equity * cfg.max_symbol_allocation_pct / 100
            )
            if proposal.action == Action.SELL and cfg.exit_policy_version == 2:
                total_ok = ZERO <= projected_total <= valuation.exposure
                allocation_ok = ZERO <= projected_symbol <= existing
        check("BALANCE_AND_NO_SHORTING", funds_ok)
        check("TOTAL_EXPOSURE_LIMIT", total_ok)
        check("SYMBOL_ALLOCATION_LIMIT", allocation_ok)
        check("FEES_SLIPPAGE_AND_STRATEGY_EDGE", costs_ok)
        check("ACTIONABLE_TRADE", proposal.action != Action.HOLD)
        reasons = tuple(c.rule for c in checks if not c.passed)
        approved = not reasons
        return RiskEvaluation(
            approved,
            reasons,
            tuple(checks),
            notional if approved else ZERO,
            quantity if approved else ZERO,
            fee,
            costs,
            valuation.equity if valuation else None,
            pnl,
            loss_triggered,
        )

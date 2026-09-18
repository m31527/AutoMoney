from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trader.exchange.models import Account, Ticker


@dataclass(frozen=True)
class RiskContext:
    account: Account
    tickers: tuple[Ticker, ...]
    # Supplied by deterministic strategy/portfolio services, never by the AI JSON.
    expected_edge_bps: Decimal
    connectivity_verified: bool = False
    account_reconciled: bool = False
    history_complete: bool = False
    no_pending_orders: bool = False


@dataclass(frozen=True)
class DayState:
    day: date
    opening_equity: Decimal
    loss_latched: bool = False


@dataclass(frozen=True)
class TradeHistory:
    trades_today: int
    last_trade_at: datetime | None


@dataclass(frozen=True)
class RuleCheck:
    rule: str
    passed: bool


@dataclass(frozen=True)
class RiskEvaluation:
    approved: bool
    reasons: tuple[str, ...]
    checks: tuple[RuleCheck, ...]
    approved_notional: Decimal
    maximum_quantity: Decimal
    estimated_fee: Decimal
    estimated_round_trip_cost_bps: Decimal | None
    current_equity: Decimal | None
    daily_pnl: Decimal | None
    daily_loss_triggered: bool


@dataclass(frozen=True)
class RecordedEvaluation:
    assessment_id: int
    result: RiskEvaluation

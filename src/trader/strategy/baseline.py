"""A transparent test baseline, not a calibrated forecast of returns."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trader.market.indicators import sma
from trader.models import Action, MarketSnapshot, TradeProposal


@dataclass(frozen=True)
class StrategyAudit:
    provider: str
    model: str
    call_id: int
    error: str | None


@dataclass(frozen=True)
class StrategyResult:
    proposal: TradeProposal
    expected_edge_bps: Decimal
    audit: StrategyAudit | None = None


class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    def propose(self, snapshot: MarketSnapshot, now: datetime) -> StrategyResult: ...


@dataclass(frozen=True)
class HoldStrategy:
    name: str = "hold"

    def propose(self, snapshot: MarketSnapshot, now: datetime) -> StrategyResult:
        return StrategyResult(
            TradeProposal(
                snapshot.symbol, Action.HOLD, Decimal(1), Decimal(0), "No-op baseline", 240
            ),
            Decimal(0),
        )


@dataclass(frozen=True)
class SMAStrategy:
    order_notional: Decimal = Decimal("50")
    name: str = "sma-5-20-v1"

    def propose(self, snapshot: MarketSnapshot, now: datetime) -> StrategyResult:
        closed = tuple(c for c in snapshot.candles_5m if c.timestamp + timedelta(minutes=5) <= now)
        if len(closed) < 21 or any(
            b.timestamp - a.timestamp != timedelta(minutes=5)
            for a, b in zip(closed, closed[1:], strict=False)
        ):
            return HoldStrategy().propose(snapshot, now)
        if not 0 <= (now - closed[-1].timestamp).total_seconds() <= 600:
            return HoldStrategy().propose(snapshot, now)
        prices = tuple(c.close for c in closed)
        fast, slow = sma(prices, 5), sma(prices, 20)
        previous_fast, previous_slow = sma(prices[:-1], 5), sma(prices[:-1], 20)
        action = Action.HOLD
        if previous_fast <= previous_slow and fast > slow:
            action = Action.BUY
        elif previous_fast >= previous_slow and fast < slow and snapshot.position > 0:
            action = Action.SELL
        edge = abs(fast / slow - 1) * 10000
        # Sell less than available marked value to leave room for adverse price/fee bounds.
        notional = (
            min(self.order_notional, snapshot.position * snapshot.last_price * Decimal("0.99"))
            if action == Action.SELL
            else self.order_notional
        )
        if action == Action.HOLD:
            notional = Decimal(0)
        return StrategyResult(
            TradeProposal(
                snapshot.symbol,
                action,
                Decimal("0.7"),
                notional,
                "Closed 5m SMA(5/20) crossover; edge is an uncalibrated test proxy",
                240,
            ),
            edge,
        )


@dataclass(frozen=True)
class HourlyTrendStrategy:
    """Hourly regime experiment: one entry while flat, exit in the opposite regime.

    MA separation remains an uncalibrated proxy. No change to cost/risk thresholds.
    """

    name: str = "trend-1h-5-20-v2"

    def propose(self, snapshot: MarketSnapshot, now: datetime) -> StrategyResult:
        closed = tuple(c for c in snapshot.candles_1h if c.timestamp + timedelta(hours=1) <= now)
        hold = HoldStrategy().propose(snapshot, now)
        if (
            len(closed) < 20
            or any(
                b.timestamp - a.timestamp != timedelta(hours=1)
                for a, b in zip(closed, closed[1:], strict=False)
            )
            or not 0 <= (now - closed[-1].timestamp).total_seconds() <= 7200
        ):
            return hold
        prices = tuple(c.close for c in closed)
        fast, slow = sma(prices, 5), sma(prices, 20)
        action = Action.HOLD
        # Quantity rounding and conservative SELL sizing leave dust; do not let it
        # permanently prevent later entries. Exchange filters still have final veto.
        holding_value = snapshot.position * snapshot.last_price
        if fast > slow and holding_value < Decimal("5"):
            action = Action.BUY
        elif fast < slow and holding_value >= Decimal("5"):
            action = Action.SELL
        amount = (
            min(Decimal("50"), snapshot.position * snapshot.last_price * Decimal("0.99"))
            if action == Action.SELL
            else Decimal("50")
            if action == Action.BUY
            else Decimal(0)
        )
        return StrategyResult(
            TradeProposal(
                snapshot.symbol,
                action,
                Decimal("0.7"),
                amount,
                "Hourly SMA(5/20) regime; uncalibrated MA separation proxy",
                1440,
            ),
            abs(fast / slow - 1) * 10000,
        )

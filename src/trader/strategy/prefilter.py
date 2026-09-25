"""Optional PAPER call filter; it never grants permission to trade."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trader.config import RiskConfig
from trader.models import MarketSnapshot
from trader.strategy.baseline import HoldStrategy, SMAStrategy, StrategyResult

VERSION = "flat-cost-prefilter-v1"
DIRECTION_VERSION = "flat-cost-direction-prefilter-v2"


def hourly_direction(snapshot: MarketSnapshot, now: datetime) -> dict[str, Any]:
    closed = tuple(c for c in snapshot.candles_1h if c.timestamp + timedelta(hours=1) <= now)[-20:]
    unavailable = {
        "state": "UNAVAILABLE",
        "fast_sma": None,
        "slow_sma": None,
        "signed_gap_bps": None,
        "last_closed_at": None,
    }
    if (
        len(closed) < 20
        or any(
            b.timestamp - a.timestamp != timedelta(hours=1)
            for a, b in zip(closed, closed[1:], strict=False)
        )
        or not 0 <= (now - closed[-1].timestamp).total_seconds() <= 7200
        or any(not c.close.is_finite() or c.close <= 0 for c in closed)
    ):
        return unavailable
    fast = sum((c.close for c in closed[-5:]), Decimal(0)) / 5
    slow = sum((c.close for c in closed), Decimal(0)) / 20
    return {
        "state": "UP" if fast > slow else "DOWN" if fast < slow else "FLAT",
        "fast_sma": str(fast),
        "slow_sma": str(slow),
        "signed_gap_bps": str((fast / slow - 1) * 10000),
        "last_closed_at": closed[-1].timestamp.isoformat(),
    }


def evaluate(
    snapshot: MarketSnapshot,
    config: RiskConfig,
    now: datetime,
    *,
    account_flat: bool,
    direction_filter: bool = False,
) -> dict[str, Any]:
    edge = SMAStrategy().propose(snapshot, now).expected_edge_bps
    upper = max(snapshot.ask, snapshot.last_price) * (1 + config.estimated_slippage_rate)
    lower = min(snapshot.bid, snapshot.last_price) * (1 - config.estimated_slippage_rate)
    cost = (upper - lower) / snapshot.last_price * 10000 + 2 * config.estimated_fee_rate * 10000
    fresh = 0 <= (now - snapshot.timestamp).total_seconds() <= config.max_data_age_seconds
    blocked = cost > config.max_execution_cost_bps or edge - cost < config.minimum_net_edge_bps
    direction = hourly_direction(snapshot, now)
    cost_skip = account_flat and fresh and blocked
    direction_skip = account_flat and fresh and (blocked or direction["state"] != "UP")
    skip = direction_skip if direction_filter else cost_skip
    return {
        "policy_version": DIRECTION_VERSION if direction_filter else VERSION,
        "reference_price": str(snapshot.last_price),
        "quote_timestamp": snapshot.timestamp.isoformat(),
        "direction_filter": direction_filter,
        "hourly_direction": direction,
        "comparison_eligible": account_flat and fresh,
        "cost_only_would_skip": cost_skip,
        "cost_and_direction_would_skip": direction_skip,
        "symbol": snapshot.symbol,
        "account_flat": account_flat,
        "skipped": skip,
        "reason": "AI_PREFILTER_COST_BLOCKED"
        if cost_skip
        else "AI_PREFILTER_DIRECTION_UNAVAILABLE"
        if skip and direction["state"] == "UNAVAILABLE"
        else "AI_PREFILTER_DIRECTION_BLOCKED"
        if skip
        else "POSITION_PRESENT"
        if not account_flat
        else "STALE_INPUT"
        if not fresh
        else "COST_GATE_PASSED",
        "signal_proxy_bps": str(edge),
        "estimated_round_trip_cost_bps": str(cost),
        "minimum_net_edge_bps": str(config.minimum_net_edge_bps),
        "max_execution_cost_bps": str(config.max_execution_cost_bps),
    }


@dataclass(frozen=True)
class PrefilterHold:
    name: str = VERSION
    reason: str = "AI_PREFILTER_COST_BLOCKED"

    def propose(self, snapshot: MarketSnapshot, now: datetime) -> StrategyResult:
        hold = HoldStrategy().propose(snapshot, now)
        return replace(
            hold,
            proposal=replace(hold.proposal, reason=self.reason),
            expected_edge_bps=SMAStrategy().propose(snapshot, now).expected_edge_bps,
        )

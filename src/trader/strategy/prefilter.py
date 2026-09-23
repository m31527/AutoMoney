"""Optional PAPER call filter; it never grants permission to trade."""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from trader.config import RiskConfig
from trader.models import MarketSnapshot
from trader.strategy.baseline import HoldStrategy, SMAStrategy, StrategyResult

VERSION = "flat-cost-prefilter-v1"


def evaluate(
    snapshot: MarketSnapshot, config: RiskConfig, now: datetime, *, account_flat: bool
) -> dict[str, Any]:
    edge = SMAStrategy().propose(snapshot, now).expected_edge_bps
    upper = max(snapshot.ask, snapshot.last_price) * (1 + config.estimated_slippage_rate)
    lower = min(snapshot.bid, snapshot.last_price) * (1 - config.estimated_slippage_rate)
    cost = (upper - lower) / snapshot.last_price * 10000 + 2 * config.estimated_fee_rate * 10000
    fresh = 0 <= (now - snapshot.timestamp).total_seconds() <= config.max_data_age_seconds
    blocked = cost > config.max_execution_cost_bps or edge - cost < config.minimum_net_edge_bps
    skip = account_flat and fresh and blocked
    return {
        "policy_version": VERSION,
        "symbol": snapshot.symbol,
        "account_flat": account_flat,
        "skipped": skip,
        "reason": "AI_PREFILTER_COST_BLOCKED"
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

    def propose(self, snapshot: MarketSnapshot, now: datetime) -> StrategyResult:
        hold = HoldStrategy().propose(snapshot, now)
        return replace(
            hold,
            proposal=replace(hold.proposal, reason="AI_PREFILTER_COST_BLOCKED"),
            expected_edge_bps=SMAStrategy().propose(snapshot, now).expected_edge_bps,
        )

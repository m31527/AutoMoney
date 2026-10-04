"""Compact, replayable research inputs. Closed candles only; no trading authority."""

import json
from datetime import datetime, timedelta
from typing import Any

from trader.models import MarketSnapshot
from trader.storage.repository import encode
from trader.strategy.budget import TradeBudget

VERSION = "compact-entry-v3"
INSTRUCTIONS = """Return JSON only: symbol, action (BUY/SELL/HOLD), confidence (0..1),
requested_notional_usd, reason, time_horizon_minutes, invalid_if, research_check.
Evaluate entry over exactly 240 minutes using supplied closed OHLCV candles.
Candles columns: open_time, open, high, low, close, volume. Never use future data.
BUY is permitted when permissions.can_buy is true, but is never required.
max_buy_notional_usd is an upper limit, NOT a ban on buying. Zero sell capacity
only prohibits SELL. Fractional coins are allowed. Exchange eligibility is unknown;
never invent minimum order sizes. Explain market evidence, not guessed exchange rules.
HOLD amount must be 0. BUY/SELL amounts must be positive and within their budgets.
The cost proxy is not a return forecast. Account for the estimated round-trip cost.
Copy input_receipt exactly into research_check. This checks receipt, not reasoning accuracy.
No tools, no real orders. Do not manufacture confidence or BUY signals.
"""


def compact_context(
    snapshot: MarketSnapshot, now: datetime, budget: TradeBudget, evaluation: dict[str, Any]
) -> str:
    candles = {}
    for minutes, count in ((1, 4), (5, 8), (15, 8), (60, 20)):
        source = getattr(snapshot, f"candles_{minutes}m" if minutes != 60 else "candles_1h")
        closed = [c for c in source if c.timestamp + timedelta(minutes=minutes) <= now][-count:]
        candles[str(minutes)] = [
            [c.timestamp, c.open, c.high, c.low, c.close, c.volume] for c in closed
        ]
    data = {
        "research_version": VERSION,
        "evaluated_at": now,
        "snapshot": {
            "symbol": snapshot.symbol,
            "timestamp": snapshot.timestamp,
            "last_price": snapshot.last_price,
            "bid": snapshot.bid,
            "ask": snapshot.ask,
            "position": snapshot.position,
            "candles_by_minutes": candles,
        },
        "permissions": {
            "can_buy": budget.max_buy_notional_usd > 0,
            "can_sell": budget.max_sell_notional_usd > 0,
            "exchange_minimum_eligibility": "NOT_ASSESSED",
        },
        "max_buy_notional_usd": budget.max_buy_notional_usd,
        "max_sell_notional_usd": budget.max_sell_notional_usd,
        "estimated_round_trip_cost_bps": evaluation.get("estimated_round_trip_cost_bps"),
        "deterministic_edge_proxy_bps": evaluation.get("signal_proxy_bps"),
    }
    return encode(data)


def research_prompt(context: str, variant: str, marker: str) -> tuple[str, str]:
    data = json.loads(context)
    if variant == "unanchored":
        data.pop("deterministic_edge_proxy_bps", None)
    data["input_receipt"] = {
        "marker": marker,
        "can_buy": data["permissions"]["can_buy"],
        "can_sell": data["permissions"]["can_sell"],
        "max_buy_notional_usd": data["max_buy_notional_usd"],
        "horizon_minutes": 240,
    }
    compact = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    if len((INSTRUCTIONS + compact).encode()) > 10000:
        raise ValueError("COMPACT_RESEARCH_INPUT_TOO_LARGE")
    return INSTRUCTIONS, compact

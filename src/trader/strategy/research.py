"""Compact, replayable research inputs. Closed candles only; no trading authority."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trader.models import MarketSnapshot
from trader.storage.repository import encode
from trader.strategy.budget import TradeBudget

VERSION = "compact-entry-v4"
INSTRUCTIONS = """Return JSON only: symbol, action (BUY/SELL/HOLD), confidence (0..1),
requested_notional_usd, reason, time_horizon_minutes, invalid_if, research_check.
Evaluate entry over exactly 240 minutes using supplied closed OHLCV candles.
Candles columns: open_time, open, high, low, close, volume. Never use future data.
BUY is permitted when permissions.can_buy is true, but is never required.
max_buy_notional_usd is an upper limit, NOT a ban on buying. Zero sell capacity
only prohibits SELL. Fractional coins are allowed. Exchange eligibility is unknown;
never invent minimum order sizes. Explain market evidence, not guessed exchange rules.
HOLD amount must be 0. BUY/SELL amounts must be positive and within their budgets.
Use cost_reference computed by code: 1 bps = 0.01 percent; 40 bps = 0.40 percent.
The reference break-even price is an approximate cost hurdle, NOT a price forecast.
Do not subtract moving-average gaps from costs to invent expected returns.
Zero current sell capacity means no inventory NOW. After a simulated buy, selling
acquired inventory is allowed subject to then-current balance, exchange and risk checks.
It does NOT mean a future exit is forbidden or guaranteed. Assess market evidence
for a 240-minute entry. Explain uncertainty; no trade can guarantee profitability.
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
    data = normalize_research_input(data)
    data["input_receipt"] = {
        "marker": marker,
        "can_buy": data["permissions"]["can_buy"],
        "can_sell": data["permissions"]["can_sell"],
        "max_buy_notional_usd": data["max_buy_notional_usd"],
        "horizon_minutes": 240,
        "round_trip_cost_pct": data["cost_reference"]["round_trip_cost_pct"],
        "future_exit_allowed_subject_to_checks": True,
    }
    compact = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    if len((INSTRUCTIONS + compact).encode()) > 10000:
        raise ValueError("COMPACT_RESEARCH_INPUT_TOO_LARGE")
    return INSTRUCTIONS, compact


def normalize_research_input(data: dict[str, Any]) -> dict[str, Any]:
    """Explicit allowlist for live/replay inputs; never include future labels or proposals."""
    snapshot = data["snapshot"]
    snapshot = {
        k: snapshot[k]
        for k in (
            "symbol",
            "timestamp",
            "last_price",
            "bid",
            "ask",
            "position",
            "candles_by_minutes",
        )
    }
    price = Decimal(str(snapshot["last_price"]))
    cost = Decimal(str(data["estimated_round_trip_cost_bps"]))
    if not price.is_finite() or price <= 0 or not cost.is_finite() or cost < 0:
        raise ValueError("Invalid research cost or price")
    return {
        "research_version": VERSION,
        "evaluated_at": data["evaluated_at"],
        "snapshot": snapshot,
        "permissions": {
            k: data["permissions"][k]
            for k in ("can_buy", "can_sell", "exchange_minimum_eligibility")
        },
        "max_buy_notional_usd": data["max_buy_notional_usd"],
        "max_sell_notional_usd": data["max_sell_notional_usd"],
        "estimated_round_trip_cost_bps": str(cost),
        "cost_reference": {
            "round_trip_cost_pct": str(cost / 100),
            "approximate_break_even_price": str(price * (1 + cost / 10000)),
            "basis": "current last price plus estimated round-trip cost; not an executable quote",
        },
        "exit_policy": {
            "future_exit_allowed_subject_to_checks": True,
            "current_zero_sell_capacity_means": "no current inventory; not a ban on future exits",
            "exit_fill_guaranteed": False,
        },
    }

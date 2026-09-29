"""Rate-limited observations of model advice. This module cannot submit orders."""

import hashlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trader.models import MarketSnapshot
from trader.storage.repository import encode, json_default
from trader.storage.transaction import transaction
from trader.strategy.ai_strategy import INSTRUCTIONS, build_context
from trader.strategy.baseline import SMAStrategy
from trader.strategy.budget import TradeBudget
from trader.strategy.contract import InvalidProposal, parse_proposal
from trader.strategy.prefilter import hourly_direction
from trader.strategy.provider import AIProvider, ProviderError

INSTRUCTIONS_SHADOW = (
    INSTRUCTIONS
    + """
This is a shadow research observation, never an executable order. Evaluate a 240-minute
horizon. Choose BUY or HOLD for an entry opportunity; SELL only if inventory exists.
The SMA cost proxy is recorded for comparison, not a rule forcing your answer to HOLD.
Do not increase confidence to meet any threshold. No suggestion will be sent to execution.
"""
)


def observe(
    connection: sqlite3.Connection,
    provider: AIProvider,
    snapshot: MarketSnapshot,
    budget: TradeBudget,
    evaluation: dict[str, Any],
    *,
    clock: Callable[[], datetime],
    interval_seconds: int = 1800,
) -> bool:
    if interval_seconds < 900:
        raise ValueError("Shadow interval must be at least 900 seconds")
    now = clock()
    if not 0 <= (now - snapshot.timestamp).total_seconds() <= 60:
        return False
    # Reserve before inference; a restart or failed call must not immediately call again.
    with transaction(connection):
        previous = connection.execute(
            "SELECT timestamp FROM system_events WHERE event_type='AI_SHADOW_STARTED' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if (
            previous
            and (now - datetime.fromisoformat(previous[0])).total_seconds() < interval_seconds
        ):
            return False
        call_id = uuid4().hex
        connection.execute(
            "INSERT INTO system_events(timestamp,severity,event_type,payload_json) "
            "VALUES (?,?,?,?)",
            (
                json_default(now),
                "INFO",
                "AI_SHADOW_STARTED",
                encode(
                    {
                        "call_id": call_id,
                        "symbol": snapshot.symbol,
                        "interval_seconds": interval_seconds,
                    }
                ),
            ),
        )
    context = build_context(snapshot, now, budget)
    started = time.monotonic()
    result: dict[str, Any] = {
        "call_id": call_id,
        "policy_version": "shadow-entry-v1",
        "model": provider.model,
        "symbol": snapshot.symbol,
        "started_at": now,
        "reference_price": snapshot.last_price,
        "quote_timestamp": snapshot.timestamp,
        "prefilter": evaluation,
        "direction": hourly_direction(snapshot, now),
        "baseline_action": SMAStrategy().propose(snapshot, now).proposal.action,
        "instructions_sha256": hashlib.sha256(INSTRUCTIONS_SHADOW.encode()).hexdigest(),
        "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
        "budget": asdict(budget),
        "status": "OK",
        "executable": False,
    }
    try:
        raw = provider.complete(INSTRUCTIONS_SHADOW, context)
        proposal = parse_proposal(raw, snapshot.symbol)
        result["proposal"] = asdict(proposal)
        result["proposal"]["reason"] = provider.redact(proposal.reason)
    except (ProviderError, InvalidProposal) as error:
        result.update(status="ERROR", error=str(error))
    result["inference_seconds"] = time.monotonic() - started
    result["completed_at"] = clock()
    with transaction(connection):
        connection.execute(
            "INSERT INTO system_events(timestamp,severity,event_type,payload_json) "
            "VALUES (?,?,?,?)",
            (json_default(clock()), "INFO", "AI_SHADOW_COMPLETED", encode(result)),
        )
    return True


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    results = [e["payload"] for e in events if e["event_type"] == "AI_SHADOW_COMPLETED"]
    starts = [e["payload"] for e in events if e["event_type"] == "AI_SHADOW_STARTED"]
    # Anchor to an observed quote AFTER inference; never pretend to fill before the answer.
    quotes = sorted(
        [
            e["payload"]
            for e in events
            if e["event_type"] == "AI_PREFILTER_EVALUATED" and "reference_price" in e["payload"]
        ],
        key=lambda p: datetime.fromisoformat(p["quote_timestamp"]),
    )
    for result in results:
        completed = datetime.fromisoformat(result["completed_at"])
        candidates = [p for p in quotes if p["symbol"] == result["symbol"]]
        anchor = next(
            (
                p
                for p in candidates
                if completed
                <= datetime.fromisoformat(p["quote_timestamp"])
                <= completed + timedelta(minutes=10)
            ),
            None,
        )
        result["post_response_forward"] = {}
        for hours in (1, 4):
            match = None
            if anchor:
                target = datetime.fromisoformat(anchor["quote_timestamp"]) + timedelta(hours=hours)
                future = next(
                    (
                        p
                        for p in candidates
                        if target
                        <= datetime.fromisoformat(p["quote_timestamp"])
                        <= target + timedelta(minutes=10)
                    ),
                    None,
                )
                if future:
                    change = (
                        Decimal(future["reference_price"]) / Decimal(anchor["reference_price"]) - 1
                    ) * 10000
                    match = {
                        "anchor_timestamp": anchor["quote_timestamp"],
                        "future_timestamp": future["quote_timestamp"],
                        "price_return_bps": str(change),
                        "buy_minus_estimated_cost_bps": str(
                            change - Decimal(anchor["estimated_round_trip_cost_bps"])
                        ),
                        "cash_return_bps": "0",
                    }
            result["post_response_forward"][f"{hours}h"] = match
    finished = {r["call_id"] for r in results}
    return {
        "started": len(starts),
        "completed": len(results),
        "failed": sum(r["status"] == "ERROR" for r in results),
        "started_without_completion_in_window": sum(s["call_id"] not in finished for s in starts),
        "actions": dict(Counter(r["proposal"]["action"] for r in results if "proposal" in r)),
        "mean_inference_seconds": sum(r["inference_seconds"] for r in results) / len(results)
        if results
        else None,
        "note": "Shadow only, no orders or holdings. "
        "Missing completions may cross export boundaries. "
        "Regular ai_calls exclude shadow calls. Suggestions are not executable or realized profit.",
        "results": results,
    }

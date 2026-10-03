"""Rate-limited observations of model advice. This module cannot submit orders."""

import hashlib
import json
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


INSTRUCTIONS_UNANCHORED = (
    INSTRUCTIONS_SHADOW.replace(
        "The deterministic edge proxy is not a calibrated forecast. You cannot set that proxy.\n",
        "",
    ).replace(
        "The SMA cost proxy is recorded for comparison, not a rule forcing your answer to HOLD.\n",
        "",
    )
    + """
The trade budget is only a maximum allowed notional, not evidence for or against entry.
Do not infer exchange minimum-order eligibility or profitability from a $100 budget.
Exchange quantity/minimum filters are checked separately by deterministic code.
Use the provided closed candles to explain evidence for BUY or HOLD over 240 minutes.
HOLD is fully valid; never manufacture a BUY or confidence score to increase trading.
"""
)


def prompt_variant(context: str, variant: str) -> tuple[str, str]:
    if variant == "control":
        return INSTRUCTIONS_SHADOW, context
    data = json.loads(context)
    data.pop("deterministic_edge_proxy_bps", None)
    return INSTRUCTIONS_UNANCHORED, encode(data)


def observe(
    connection: sqlite3.Connection,
    provider: AIProvider,
    snapshot: MarketSnapshot,
    budget: TradeBudget,
    evaluation: dict[str, Any],
    *,
    clock: Callable[[], datetime],
    interval_seconds: int = 1800,
    paired: bool = False,
    paused: Callable[[], bool] = lambda: False,
) -> bool:
    if interval_seconds < 900:
        raise ValueError("Shadow interval must be at least 900 seconds")
    if paired and interval_seconds < 3600:
        raise ValueError("Paired shadow requires at least 3600 seconds")
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
        pair_id = uuid4().hex if paired else None
        count = connection.execute(
            "SELECT COUNT(*) FROM system_events WHERE event_type='AI_SHADOW_STARTED'"
        ).fetchone()[0]
        variants = ["control", "unanchored"] if paired else ["control"]
        if paired and (count // 2) % 2:
            variants.reverse()
        reservations = [(variant, uuid4().hex) for variant in variants]
        for variant, call_id in reservations:
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
                            "pair_id": pair_id,
                            "variant": variant,
                        }
                    ),
                ),
            )
    context = build_context(snapshot, now, budget)
    for variant, call_id in reservations:
        if paused():
            break
        instructions, variant_context = prompt_variant(context, variant)
        started = time.monotonic()
        result: dict[str, Any] = {
            "call_id": call_id,
            "policy_version": "shadow-prompt-pair-v2" if paired else "shadow-entry-v1",
            "pair_id": pair_id,
            "variant": variant,
            "snapshot_context_sha256": hashlib.sha256(context.encode()).hexdigest(),
            "model": provider.model,
            "provider": provider.name,
            "symbol": snapshot.symbol,
            "started_at": now,
            "reference_price": snapshot.last_price,
            "quote_timestamp": snapshot.timestamp,
            "prefilter": evaluation,
            "direction": hourly_direction(snapshot, now),
            "baseline_action": SMAStrategy().propose(snapshot, now).proposal.action,
            "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
            "context_sha256": hashlib.sha256(variant_context.encode()).hexdigest(),
            "budget": asdict(budget),
            "status": "OK",
            "executable": False,
        }
        try:
            raw = provider.complete(instructions, variant_context)
            proposal = parse_proposal(raw, snapshot.symbol)
            result["proposal"] = asdict(proposal)
            result["proposal"]["reason"] = provider.redact(proposal.reason)
        except (ProviderError, InvalidProposal) as error:
            result.update(status="ERROR", error=str(error))
        metadata = getattr(provider, "last_metadata", None)
        if isinstance(metadata, dict):
            result["provider_metadata"] = metadata.copy()
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
    pairs: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        if result.get("pair_id"):
            pairs.setdefault(result["pair_id"], []).append(result)
    for result in results:
        members = pairs.get(result.get("pair_id"), [result])
        completed = max(datetime.fromisoformat(r["completed_at"]) for r in members)
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
        "prompt_comparison": {
            "complete_pairs": sum(
                len(v) == 2 and all(r["status"] == "OK" for r in v) for v in pairs.values()
            ),
            "disagreeing_pairs": sum(
                len(v) == 2
                and all(r["status"] == "OK" for r in v)
                and len({r["proposal"]["action"] for r in v}) > 1
                for v in pairs.values()
            ),
            "by_variant": {
                variant: dict(
                    Counter(
                        r["proposal"]["action"]
                        for r in results
                        if r.get("variant", "control") == variant and "proposal" in r
                    )
                )
                for variant in ("control", "unanchored")
            },
            "note": "Paired same snapshot, alternating execution order. Both use a common "
            "post-pair quote anchor. Incomplete/error pairs are not valid comparisons.",
        },
        "by_provider_model": {
            identity: {
                "completed": len(group),
                "failed": sum(r["status"] == "ERROR" for r in group),
                "actions": dict(Counter(r["proposal"]["action"] for r in group if "proposal" in r)),
            }
            for identity in sorted(
                {f"{r.get('provider', 'legacy')}:{r.get('model', 'unknown')}" for r in results}
            )
            for group in [
                [
                    r
                    for r in results
                    if f"{r.get('provider', 'legacy')}:{r.get('model', 'unknown')}" == identity
                ]
            ]
        },
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

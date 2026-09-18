"""Strict local validation remains necessary even with provider structured output."""

import json
from decimal import Decimal
from typing import Any

from trader.models import Action, TradeProposal

FIELDS = (
    "symbol",
    "action",
    "confidence",
    "requested_notional_usd",
    "reason",
    "time_horizon_minutes",
    "invalid_if",
)
PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(FIELDS),
    "properties": {
        "symbol": {"type": "string", "enum": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]},
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "requested_notional_usd": {"type": "number", "minimum": 0},
        "reason": {"type": "string"},
        "time_horizon_minutes": {"type": "integer", "minimum": 1},
        "invalid_if": {"type": "array", "items": {"type": "string"}},
    },
}


class InvalidProposal(ValueError):
    def __init__(self) -> None:
        super().__init__("AI_INVALID_PROPOSAL")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise InvalidProposal()
    return dict(pairs)


def _constant(value: str) -> None:
    raise InvalidProposal()


def parse_proposal(raw: str, symbol: str) -> TradeProposal:
    try:
        if len(raw.encode()) > 32768:
            raise InvalidProposal()
        data = json.loads(
            raw, parse_float=Decimal, object_pairs_hook=_object, parse_constant=_constant
        )
        if not isinstance(data, dict) or set(data) != set(FIELDS):
            raise InvalidProposal()
        if data["symbol"] != symbol or symbol not in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
            raise InvalidProposal()
        action = Action(data["action"])
        confidence, amount = data["confidence"], data["requested_notional_usd"]
        if any(type(v) not in (int, Decimal) for v in (confidence, amount)):
            raise InvalidProposal()
        confidence, amount = Decimal(confidence), Decimal(amount)
        if (
            not confidence.is_finite()
            or not 0 <= confidence <= 1
            or not amount.is_finite()
            or amount < 0
            or amount > Decimal("1000000000")
        ):
            raise InvalidProposal()
        if (action == Action.HOLD and amount != 0) or (action != Action.HOLD and amount == 0):
            raise InvalidProposal()
        reason, horizon, conditions = (
            data["reason"],
            data["time_horizon_minutes"],
            data["invalid_if"],
        )
        if (
            not isinstance(reason, str)
            or not 1 <= len(reason) <= 2000
            or type(horizon) is not int
            or not 1 <= horizon <= 10080
            or not isinstance(conditions, list)
            or len(conditions) > 10
            or any(not isinstance(v, str) or not 1 <= len(v) <= 300 for v in conditions)
        ):
            raise InvalidProposal()
        reason.encode("utf-8")
        for condition in conditions:
            condition.encode("utf-8")
        return TradeProposal(symbol, action, confidence, amount, reason, horizon, tuple(conditions))
    except (ValueError, TypeError, KeyError, RecursionError, ArithmeticError):
        raise InvalidProposal() from None

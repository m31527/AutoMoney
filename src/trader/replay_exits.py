"""Offline, independent decision replay: identical inputs, different exit policies.

This is a risk-gate counterfactual, not a sequential portfolio backtest or fill simulation.
No network, model, or trading database is used.
"""

import argparse
import json
import zipfile
from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.config import RiskConfig
from trader.exchange.models import Account, Balance, Ticker
from trader.models import Action, TradeProposal
from trader.risk.engine import RiskEngine
from trader.risk.models import DayState, RiskContext, TradeHistory
from trader.storage.repository import encode

BASELINE_EXITS = {"sma-5-20-v1", "trend-1h-5-20-v2"}


def replay_assessment(row: dict[str, Any], strategy: str) -> dict[str, Any]:
    cfg = RiskConfig(**row["rules"])
    p = dict(row["proposal"])
    p["action"] = Action(p["action"])
    for key in ("confidence", "requested_notional_usd", "leverage"):
        p[key] = Decimal(p[key])
    p["invalid_if"] = tuple(p["invalid_if"])
    proposal = TradeProposal(**p)
    data = row["context"]
    obs = dict(data["observations"])
    account = dict(obs["account"])
    for key in ("fetched_at", "updated_at"):
        account[key] = datetime.fromisoformat(account[key])
    account["balances"] = tuple(
        Balance(b["asset"], Decimal(b["free"]), Decimal(b["locked"])) for b in account["balances"]
    )
    obs["account"] = Account(**account)
    tickers = []
    for value in obs["tickers"]:
        ticker_data = dict(value)
        for key in ("timestamp", "fetched_at"):
            ticker_data[key] = datetime.fromisoformat(ticker_data[key])
        for key in ("last_price", "bid", "ask", "volume_24h"):
            ticker_data[key] = Decimal(ticker_data[key])
        tickers.append(Ticker(**ticker_data))
    obs["tickers"] = tuple(tickers)
    obs["expected_edge_bps"] = Decimal(obs["expected_edge_bps"])
    context = RiskContext(**obs)
    d = data["day"]
    day = (
        DayState(date.fromisoformat(d["date"]), Decimal(d["opening_equity"]), d["loss_latched"])
        if d
        else None
    )
    h = data["history"]
    history = TradeHistory(
        h["trades_today"],
        datetime.fromisoformat(h["last_trade_at"]) if h["last_trade_at"] else None,
    )
    checks = {c["rule"]: c["passed"] for c in row["result"]["checks"]}
    killed = not checks["KILL_SWITCH_CLEAR"]
    now = datetime.fromisoformat(row["timestamp"])
    outcomes = {}
    for version in (1, 2):
        rules = replace(cfg, exit_policy_version=version)
        candidate = proposal
        ticker = next((t for t in tickers if t.symbol == proposal.symbol), None)
        if strategy in BASELINE_EXITS and ticker is not None:
            quantity = sum(
                (
                    b.free
                    for b in context.account.balances
                    if b.asset == proposal.symbol.removesuffix("USDT")
                ),
                Decimal(0),
            )
            # Recreate the actual baseline notional rules in both versions.
            amount = min(Decimal("50"), quantity * ticker.last_price * Decimal("0.99"))
            if version == 2:
                upper = max(ticker.ask, ticker.last_price) * (1 + rules.estimated_slippage_rate)
                amount = min(quantity * upper, rules.max_order_notional_usd)
            candidate = replace(proposal, requested_notional_usd=amount)
        result = RiskEngine(rules).evaluate(
            candidate, context, day, history, now=now, killed=killed
        )
        outcomes[str(version)] = {
            "requested_notional_usd": candidate.requested_notional_usd,
            **asdict(result),
        }
    return {
        "assessment_id": row["id"],
        "timestamp": row["timestamp"],
        "symbol": proposal.symbol,
        "strategy": strategy,
        "policies": outcomes,
    }


def replay_book(risks: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    strategies = {d["result"]["assessment_id"]: d["result"]["strategy"] for d in decisions}
    rows = []
    for risk in risks:
        if risk["proposal"]["action"] == "SELL":
            rows.append(replay_assessment(risk, strategies.get(risk["id"], "unknown")))
    return {
        "sell_decisions": len(rows),
        "v1_approved": sum(r["policies"]["1"]["approved"] for r in rows),
        "v2_approved": sum(r["policies"]["2"]["approved"] for r in rows),
        "v1_rejected_v2_approved": sum(
            not r["policies"]["1"]["approved"] and r["policies"]["2"]["approved"] for r in rows
        ),
        "v2_rejection_reasons": dict(
            Counter(reason for r in rows for reason in r["policies"]["2"]["reasons"])
        ),
        "decisions": rows,
    }


LIMITATION = (
    "Independent risk-gate replay, identical recorded account/market/day/history per decision. "
    "Baseline sizing follows each exit version; unknown/AI strategies retain requested amount. "
    "Approvals are NOT fills: exchange lot/minimum filters and execution latency are not replayed. "
    "No sequential portfolio, counterfactual profit, or strategy return is calculated. "
    "Repeated approvals can refer to the same recorded holding; they cannot be added as trades."
)


def replay_zip(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"scope": LIMITATION, "books": {}}
    with zipfile.ZipFile(path) as archive:
        for book in ("sma5m", "trend1h", "ollama", "legacy"):
            risk_name, decision_name = book + "/risk.jsonl", book + "/decisions.jsonl"
            if risk_name not in archive.namelist() or decision_name not in archive.namelist():
                continue
            risks = [json.loads(line) for line in archive.read(risk_name).splitlines() if line]
            decisions = [
                json.loads(line) for line in archive.read(decision_name).splitlines() if line
            ]
            result["books"][book] = replay_book(risks, decisions)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline exit risk replay; no orders or model calls"
    )
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(encode(replay_zip(args.archive)))


if __name__ == "__main__":
    main()

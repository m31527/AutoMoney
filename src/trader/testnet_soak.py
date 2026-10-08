"""Bounded strategy soak on Testnet; public candles, virtual execution, no LIVE path."""

import argparse
import fcntl
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from trader.config import TradingMode
from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.models import Credentials, Reconciliation
from trader.exchange.testnet_transport import TestnetTransport
from trader.execution.filters import market_quantity
from trader.market.data import collect_market
from trader.models import Action, MarketSnapshot
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.exchange_journal import ExchangeJournal
from trader.storage.repository import Repository, encode
from trader.strategy.baseline import HourlyTrendStrategy

CAPITAL = D("100")
ORDER = D("50")


def guard(
    state: dict[str, Any], action: Action, amount: D, bid: D, ask: D, edge: D, now: float
) -> str | None:
    """Fixed Testnet limits; never controlled by strategy output."""
    if any(not v.is_finite() for v in (amount, bid, ask, edge)) or bid <= 0 or ask < bid:
        return "INVALID_MARKET"
    if action == Action.HOLD:
        return "HOLD"
    if amount <= 0 or amount > ORDER:
        return "ORDER_LIMIT"
    if len(state["times"]) >= 4:
        return "DAILY_ORDER_LIMIT"
    if state["last_trade"] and now - state["last_trade"] < 1800:
        return "COOLDOWN"
    if (ask / bid - 1) * 10000 > 20:
        return "SPREAD_LIMIT"
    if action == Action.BUY:
        if state["loss_latched"]:
            return "LOSS_LIMIT"
        if D(state["btc"]) * ask >= 5:
            return "POSITION_EXISTS"
        if D(state["btc"]) * ask + amount > 50 or D(state["cash"]) < amount * D("1.003"):
            return "BUDGET_LIMIT"
        # Preserve conservative 40 bps fee/slippage estimate plus spread + 5 bps edge.
        if edge < 45 + (ask / bid - 1) * 10000:
            return "COST_GATE"
    elif action != Action.SELL or D(state["btc"]) <= 0:
        return "NO_POSITION"
    return None


def apply_fill(state: dict[str, Any], result: Reconciliation) -> None:
    if result.order.status != "FILLED" or not result.fills_complete:
        raise ValueError("UNRESOLVED_ORDER")
    cash, btc = D(state["cash"]), D(state["btc"])
    sign = D(1) if result.order.side == "BUY" else D(-1)
    for f in result.fills:
        if f.fee_asset not in ("BTC", "USDT"):
            raise ValueError("UNSUPPORTED_FEE_ASSET")
        btc += sign * f.quantity - (f.fee if f.fee_asset == "BTC" else 0)
        cash -= sign * f.quote_quantity + (f.fee if f.fee_asset == "USDT" else 0)
    if cash < 0 or btc < 0:
        raise ValueError("NEGATIVE_STRATEGY_BALANCE")
    state.update(
        cash=str(cash), btc=str(btc), pending=None, last_trade=result.order.updated_at.timestamp()
    )
    state["times"].append(result.order.updated_at.timestamp())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "status", "stop"))
    parser.add_argument("--data", type=Path, default=Path("/soak"))
    args = parser.parse_args()
    if (
        os.environ.get("TRADING_MODE", "TESTNET") != "TESTNET"
        or os.environ.get("ENABLE_LIVE_TRADING", "false") != "false"
    ):
        raise SystemExit("TESTNET_ONLY")
    args.data.mkdir(parents=True, exist_ok=True)
    db = connect(args.data / "soak.db")
    db.execute("CREATE TABLE IF NOT EXISTS soak_state (id INTEGER PRIMARY KEY, payload TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS soak_events (id INTEGER PRIMARY KEY, payload TEXT)")
    switch = KillSwitch(Repository(db))
    if args.action == "stop":
        switch.kill()
        print("STOPPED; existing holdings preserved, no automatic liquidation")
        return
    if args.action == "status":
        row = db.execute("SELECT payload FROM soak_state WHERE id=1").fetchone()
        print(row[0] if row else '{"status":"NOT_STARTED"}')
        print(encode({"stop_active": switch.active}))
        return
    with (args.data / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        adapter = BinanceSpotAdapter(
            credentials=Credentials(
                os.environ["BINANCE_TESTNET_API_KEY"], os.environ["BINANCE_TESTNET_API_SECRET"]
            ),
            transport=TestnetTransport(),
            journal=ExchangeJournal(db),
            kill_switch=switch,
        )
        assert adapter.journal is not None
        public = BinanceSpotAdapter(mode=TradingMode.PAPER)
        row = db.execute("SELECT payload FROM soak_state WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else None

        def save(event: dict[str, Any]) -> None:
            with db:
                db.execute("INSERT OR REPLACE INTO soak_state VALUES (1,?)", (encode(state),))
                db.execute("INSERT INTO soak_events(payload) VALUES (?)", (encode(event),))
            print(encode(event), flush=True)

        try:
            if switch.active:
                raise ValueError("PERSISTENT_STOP")
            if state is None:
                account = adapter.get_account()
                if not account.can_trade or adapter.get_open_orders("BTCUSDT"):
                    raise ValueError("ACCOUNT_NOT_READY")
                funds = {b.asset: b.free + b.locked for b in account.balances}
                if funds.get("USDT", D(0)) < CAPITAL:
                    raise ValueError("INSUFFICIENT_TEST_FUNDS")
                state = dict(
                    status="RUNNING",
                    started=time.time(),
                    deadline=time.time() + 48 * 3600,
                    cash=str(CAPITAL),
                    btc="0",
                    base_cash=str(funds["USDT"]),
                    base_btc=str(funds.get("BTC", 0)),
                    pending=None,
                    last_trade=0,
                    times=[],
                    day="",
                    opening_equity="100",
                    loss_latched=False,
                    cycles=0,
                    orders=0,
                    strategy="trend-1h-5-20-v2",
                    market_source="PRODUCTION_PUBLIC",
                    execution="TESTNET",
                )
                save({"event": "STARTED", "at": time.time()})
            while state["pending"] or time.time() < state["deadline"]:
                if switch.active:
                    raise ValueError("PERSISTENT_STOP")
                # Recover pending submission before any new strategy decision.
                if state["pending"]:
                    cid = state["pending"]
                    if adapter.journal.expected_request(cid) is None:
                        raise ValueError("INTENT_NOT_SENT_REQUIRES_REVIEW")
                    result = adapter.reconcile("BTCUSDT", cid)
                    apply_fill(state, result)
                    state["orders"] += 1
                    save({"event": "FILL", "result": asdict(result), "at": time.time()})
                if time.time() >= state["deadline"]:
                    break
                funds = {b.asset: b.free + b.locked for b in adapter.get_balances()}
                if (
                    funds.get("BTC", D(0)) != D(state["base_btc"]) + D(state["btc"])
                    or funds.get("USDT", D(0)) != D(state["base_cash"]) + D(state["cash"]) - CAPITAL
                ):
                    raise ValueError("ACCOUNT_DRIFT")
                if adapter.get_open_orders("BTCUSDT"):
                    raise ValueError("UNEXPECTED_OPEN_ORDER")
                market = collect_market(public, "BTCUSDT", limit=30)
                now = datetime.now(UTC)
                ticker = adapter.get_ticker("BTCUSDT")
                if not 0 <= (datetime.now(UTC) - ticker.timestamp).total_seconds() <= 60:
                    raise ValueError("STALE_TESTNET_PRICE")
                equity = D(state["cash"]) + D(state["btc"]) * ticker.bid
                day = now.date().isoformat()
                if state["day"] != day:
                    state.update(day=day, opening_equity=str(equity), loss_latched=False)
                state["times"] = [
                    t for t in state["times"] if datetime.fromtimestamp(t, UTC).date() == now.date()
                ]
                if equity <= D(state["opening_equity"]) - 2 or equity <= CAPITAL - 3:
                    state["loss_latched"] = True
                m = market.ticker
                snap = MarketSnapshot(
                    "BTCUSDT",
                    now,
                    m.last_price,
                    m.bid,
                    m.ask,
                    m.spread,
                    market.candles_1m,
                    market.candles_5m,
                    market.candles_15m,
                    market.candles_1h,
                    m.volume_24h,
                    D(state["btc"]),
                    D(state["cash"]),
                    D(0),
                    D(0),
                    D(0),
                    len(state["times"]),
                )
                strategy_result = HourlyTrendStrategy().propose(snap, now)
                proposal = strategy_result.proposal
                reason = guard(
                    state,
                    proposal.action,
                    proposal.requested_notional_usd,
                    ticker.bid,
                    ticker.ask,
                    strategy_result.expected_edge_bps,
                    now.timestamp(),
                )
                if abs(ticker.last_price / m.last_price - 1) > D(".01"):
                    reason = "TESTNET_PRICE_DIVERGENCE"
                previous_cycle = state.get("last_cycle")
                if previous_cycle:
                    gap = (now - datetime.fromisoformat(previous_cycle)).total_seconds()
                    state["max_gap_seconds"] = max(state.get("max_gap_seconds", 0), gap)
                state["cycles"] += 1
                state["equity"] = str(equity)
                state["last_cycle"] = now.isoformat()
                save(
                    {
                        "event": "DECISION",
                        "at": now.isoformat(),
                        "proposal": asdict(proposal),
                        "edge_bps": strategy_result.expected_edge_bps,
                        "reason": reason,
                        "equity": equity,
                    }
                )
                if reason is None:
                    metadata = adapter.get_exchange_info("BTCUSDT")
                    average = adapter.get_average_price("BTCUSDT")
                    ticker = adapter.get_ticker("BTCUSDT")
                    if not 0 <= (datetime.now(UTC) - ticker.timestamp).total_seconds() <= 60:
                        raise ValueError("STALE_TESTNET_PRICE")
                    repeated_guard = guard(
                        state,
                        proposal.action,
                        proposal.requested_notional_usd,
                        ticker.bid,
                        ticker.ask,
                        strategy_result.expected_edge_bps,
                        time.time(),
                    )
                    if repeated_guard:
                        raise ValueError("PREFLIGHT_CHANGED")
                    maximum = proposal.requested_notional_usd / (ticker.ask * D("1.002"))
                    if proposal.action == Action.SELL:
                        maximum = min(maximum, D(state["btc"]))
                    quantity = market_quantity(
                        maximum, metadata, ticker, average, now=datetime.now(UTC), max_age=60
                    )
                    cid = (
                        "soak_"
                        + hashlib.sha256(
                            f"{state['started']}:{state['cycles']}".encode()
                        ).hexdigest()[:28]
                    )
                    state["pending"] = cid
                    save(
                        {
                            "event": "INTENT",
                            "client_id": cid,
                            "side": proposal.action,
                            "quantity": quantity,
                        }
                    )
                    adapter.place_order("BTCUSDT", proposal.action.value, quantity, cid)
                    continue
                time.sleep(300)
            state["status"] = "OBSERVATION_COMPLETE_REVIEW_REQUIRED"
            save({"event": "WINDOW_ENDED", "holdings_btc": state["btc"]})
        except Exception as error:
            switch.trip("SOAK_REVIEW_REQUIRED")
            if state is not None:
                state.update(status="BLOCKED", error=type(error).__name__)
                save({"event": "BLOCKED", "error": type(error).__name__})
            raise SystemExit(
                "SOAK stopped; inspect persistent records; no automatic retry"
            ) from None


if __name__ == "__main__":
    main()

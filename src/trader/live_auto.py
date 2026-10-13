"""Opt-in automatic production runner. Never enabled by installation or deployment."""

import argparse
import fcntl
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from trader.config import TradingMode
from trader.exchange.binance import BinanceSpotAdapter
from trader.launch_policy import LIVE_POLICY
from trader.live_control import LiveControl
from trader.market.data import collect_market
from trader.models import Action, MarketSnapshot
from trader.storage.repository import encode
from trader.strategy.baseline import HourlyTrendStrategy

STRATEGY = "trend-1h-5-20-v2"
ARM_PHRASE = "我確認啟用80USDT自動實盤"


def enabled() -> None:
    if os.getenv("ENABLE_LIVE_TRADING") != "true" or os.getenv("ENABLE_AUTO_LIVE") != "true":
        raise ValueError("AUTO_LIVE_DISABLED")
    if os.getenv("LIVE_IP_GUARD") != "true":
        raise ValueError("IP_GUARD_REQUIRED")


def require_armed(control: LiveControl) -> None:
    enabled()
    arm = control.state.get("auto", {})
    if (
        not arm.get("armed")
        or arm.get("key_id") != control.key_id
        or arm.get("policy_version") != LIVE_POLICY.version
        or arm.get("strategy") != STRATEGY
    ):
        raise ValueError("AUTO_ARM_REQUIRED")


def arm(control: LiveControl, confirmation: str) -> None:
    enabled()
    if confirmation != ARM_PHRASE:
        raise ValueError("AUTO_CONFIRMATION_REQUIRED")
    control.check_release()
    control.preflight()
    if control.state.get("proposal"):
        raise ValueError("MANUAL_PROPOSAL_EXISTS")
    previous = control.state.get("auto", {})
    control.state["auto"] = {
        "armed": True,
        "key_id": control.key_id,
        "policy_version": LIVE_POLICY.version,
        "strategy": STRATEGY,
        "armed_at": time.time(),
        "last_slot": previous.get("last_slot"),
    }
    control.save("AUTO_ARMED", policy=asdict(LIVE_POLICY), strategy=STRATEGY)


def cycle(control: LiveControl, public: BinanceSpotAdapter) -> dict[str, Any]:
    require_armed(control)
    # Unknown prior execution is queried only; it never re-enters the send path.
    if control.state.get("pending"):
        control.switch.trip("LIVE_ORDER_REVIEW_REQUIRED")
        control.state["auto"]["armed"] = False
        control.save("AUTO_PENDING_REVIEW")
        return control.reconcile()
    control.check_release()
    control.preflight()
    slot = int(time.time() // 300)
    auto = control.state["auto"]
    if auto.get("last_slot") == slot:
        return {"action": "SKIP", "reason": "CYCLE_ALREADY_EVALUATED"}
    # Claim before evaluation: a process crash can skip a decision, never replay it.
    auto.update(last_slot=slot, last_cycle=time.time())
    control.save("AUTO_CYCLE_STARTED", slot=slot)
    market = collect_market(public, "BTCUSDT", limit=30)
    now = datetime.now(UTC)
    m = market.ticker
    if not 0 <= (now - m.timestamp).total_seconds() <= 60:
        raise ValueError("STALE_PRICE")
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
        D(control.state["btc"]),
        D(control.state["cash"]),
        D(0),
        D(0),
        D(0),
        len(control.state["times"]),
    )
    decision = HourlyTrendStrategy().propose(snap, now)
    side = decision.proposal.action
    reason = "HOLD" if side == Action.HOLD else None
    if side == Action.BUY and decision.expected_edge_bps < 45 + (m.ask / m.bid - 1) * 10000:
        reason = "COST_GATE"
    if side == Action.BUY and (
        control.state["loss_latched"] or control.state.get("cumulative_loss_latched")
    ):
        reason = "LOSS_LIMIT"
    amount = min(LIVE_POLICY.order_limit, decision.proposal.requested_notional_usd)
    control.save(
        "AUTO_DECISION",
        strategy=STRATEGY,
        proposal=asdict(decision.proposal),
        edge_bps=decision.expected_edge_bps,
        reason=reason,
        slot=slot,
    )
    if reason:
        auto.update(last_action="HOLD", last_reason=reason)
        control.save("AUTO_WAIT", reason=reason)
        return {"action": "HOLD", "reason": reason}
    try:
        proposal = control.prepare(side.value, str(amount), automatic=True)
    except ValueError as error:
        if str(error) not in {
            "RATE_OR_COOLDOWN_LIMIT",
            "BUDGET_LIMIT",
            "LOSS_LIMIT",
            "NO_OWNED_POSITION",
        }:
            raise
        auto.update(last_action="REJECTED", last_reason=str(error))
        control.save("AUTO_REJECTED", reason=str(error))
        return {"action": "REJECTED", "reason": str(error)}
    # Fresh execution quote must still support the original cost gate and direction price.
    ask = D(proposal["ask"])
    if abs(ask / m.ask - 1) > D(".003") or time.time() - now.timestamp() > 60:
        raise ValueError("AUTO_SIGNAL_EXPIRED_OR_MOVED")
    if side == Action.BUY:
        assert control.adapter is not None
        fresh = control.adapter.get_ticker("BTCUSDT")
        if not 0 <= time.time() - fresh.timestamp.timestamp() <= 60:
            raise ValueError("STALE_PRICE")
        if decision.expected_edge_bps < 45 + (fresh.ask / fresh.bid - 1) * 10000:
            control.state["proposal"] = None
            control.save("AUTO_REJECTED", reason="COST_GATE_REFRESHED")
            return {"action": "REJECTED", "reason": "COST_GATE_REFRESHED"}
    result = control.submit(proposal, automatic=True)
    control.state["auto"].update(last_action=side.value, last_reason="FILLED")
    control.save("AUTO_CYCLE_COMPLETED")
    return result


def step(data: Path, soak: Path, action: str, confirmation: str = "") -> dict[str, Any]:
    data.mkdir(parents=True, exist_ok=True)
    with (data / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        control = LiveControl(data, soak)
        try:
            if action == "arm":
                arm(control, confirmation)
            elif action == "disarm":
                control.switch.kill()
                control.state.setdefault("auto", {})["armed"] = False
                control.state["proposal"] = None
                control.save("AUTO_DISARMED")
            elif action == "run":
                try:
                    return cycle(control, BinanceSpotAdapter(mode=TradingMode.PAPER))
                except Exception:
                    control.switch.trip("AUTO_LIVE_REVIEW_REQUIRED")
                    control.state.setdefault("auto", {})["armed"] = False
                    control.state.update(proposal=None, status="BLOCKED")
                    control.save("AUTO_BLOCKED")
                    raise
            return control.status()
        finally:
            control.db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "arm", "disarm", "run"))
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--data", type=Path, default=Path("/live"))
    parser.add_argument("--soak", type=Path, default=Path("/soak"))
    args = parser.parse_args()
    while True:
        try:
            print(encode(step(args.data, args.soak, args.action, args.confirmation)), flush=True)
        except BlockingIOError:
            if args.action != "run":
                raise SystemExit("CONTROL_BUSY") from None
        except Exception:
            raise SystemExit("AUTO_STOPPED_CHECK_STATUS_AND_JOURNAL") from None
        if args.action != "run":
            return
        time.sleep(300)


if __name__ == "__main__":
    main()

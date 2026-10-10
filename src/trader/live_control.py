"""Authenticated, one-order-at-a-time production control. No autonomous LIVE worker."""

import copy
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
from uuid import uuid4

from trader.exchange.models import Credentials
from trader.exchange.production import ProductionAdapter, ProductionTransport
from trader.execution.filters import market_quantity
from trader.launch_policy import INITIAL_POLICY
from trader.live_readiness import read_report
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.exchange_journal import ExchangeJournal
from trader.storage.repository import Repository, encode
from trader.testnet_soak import apply_fill

CONFIRMATION = "我確認使用真實 USDT 下這一筆訂單"


class LiveControl:
    def __init__(self, data: Path, soak: Path) -> None:
        self.data, self.soak = data, soak
        data.mkdir(parents=True, exist_ok=True)
        self.db = connect(data / "live.db")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS live_state (id INTEGER PRIMARY KEY, payload TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS live_events (id INTEGER PRIMARY KEY, payload TEXT)"
        )
        self.switch = KillSwitch(Repository(self.db))
        self.transport = ProductionTransport()
        self.adapter: ProductionAdapter | None = None
        key, secret = (
            os.getenv("BINANCE_LIVE_API_KEY", ""),
            os.getenv("BINANCE_LIVE_API_SECRET", ""),
        )
        self.key_id = hashlib.sha256(key.encode()).hexdigest()
        if key and secret:
            self.adapter = ProductionAdapter(
                Credentials(key, secret),
                transport=self.transport,
                journal=ExchangeJournal(self.db),
                kill_switch=self.switch,
            )
        row = self.db.execute("SELECT payload FROM live_state WHERE id=1").fetchone()
        self.state: dict[str, Any] = (
            json.loads(row[0])
            if row
            else {
                "cash": "100",
                "btc": "0",
                "times": [],
                "last_trade": 0,
                "pending": None,
                "orders": 0,
                "status": "NOT_INITIALIZED",
                "policy": asdict(INITIAL_POLICY),
                "loss_latched": False,
            }
        )

    def save(self, event: str, **details: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO live_state VALUES (1,?)", (encode(self.state),))
            self.db.execute(
                "INSERT INTO live_events(payload) VALUES (?)",
                (
                    encode(
                        {
                            "event": event,
                            "at": time.time(),
                            **details,
                        }
                    ),
                ),
            )

    def status(self) -> dict[str, Any]:
        evidence = read_report(self.soak)
        return {
            "environment": "PRODUCTION",
            "execution": "MANUAL_PER_ORDER",
            "credentials_configured": self.adapter is not None,
            "execution_enabled": os.getenv("ENABLE_LIVE_TRADING") == "true",
            "stop_active": self.switch.active,
            "testnet_passed": evidence.get("testnet_status") == "REVIEW_REQUIRED",
            "policy": asdict(INITIAL_POLICY),
            "state": {
                k: (
                    None
                    if k in ("cash", "btc", "equity") and "base_cash" not in self.state
                    else self.state.get(k)
                )
                for k in (
                    "status",
                    "cash",
                    "btc",
                    "equity",
                    "orders",
                    "pending",
                    "proposal",
                    "last_check",
                )
            },
        }

    def preflight(self) -> dict[str, Any]:
        if self.adapter is None:
            raise ValueError("PRODUCTION_CREDENTIALS_MISSING")
        permissions = self.adapter.check_permissions()
        account = self.adapter.get_account()
        if not account.can_trade or self.adapter.get_open_orders("BTCUSDT"):
            raise ValueError("ACCOUNT_NOT_READY")
        if any(b.locked for b in account.balances if b.asset in ("BTC", "USDT")):
            raise ValueError("ACCOUNT_FUNDS_LOCKED")
        funds = {b.asset: b.free + b.locked for b in account.balances}
        if "base_cash" not in self.state:
            if funds.get("USDT", D(0)) < INITIAL_POLICY.capital:
                raise ValueError("INSUFFICIENT_LIVE_FUNDS")
            self.state.update(
                base_cash=str(funds["USDT"]),
                base_btc=str(funds.get("BTC", 0)),
                key_id=self.key_id,
                status="READY_FOR_REVIEW",
            )
        if self.state["key_id"] != self.key_id:
            raise ValueError("KEY_CHANGED_REQUIRES_REVIEW")
        if (
            funds.get("BTC", D(0)) != D(self.state["base_btc"]) + D(self.state["btc"])
            or funds.get("USDT", D(0))
            != D(self.state["base_cash"]) + D(self.state["cash"]) - INITIAL_POLICY.capital
        ):
            raise ValueError("ACCOUNT_DRIFT_RECONCILE_REQUIRED")
        ticker = self.adapter.get_ticker("BTCUSDT")
        if not 0 <= time.time() - ticker.timestamp.timestamp() <= 60:
            raise ValueError("STALE_PRICE")
        if (ticker.ask / ticker.bid - 1) * 10000 > 20:
            raise ValueError("SPREAD_LIMIT")
        equity = D(self.state["cash"]) + D(self.state["btc"]) * ticker.bid
        day = datetime.now(UTC).date().isoformat()
        if self.state.get("day") != day:
            self.state.update(day=day, opening_equity=str(equity), loss_latched=False)
        if equity <= D(self.state["opening_equity"]) - INITIAL_POLICY.daily_loss:
            self.state["loss_latched"] = True
        if equity <= INITIAL_POLICY.capital - INITIAL_POLICY.cumulative_loss:
            self.state["cumulative_loss_latched"] = True
        self.state["equity"] = str(equity)
        self.state["last_check"] = time.time()
        self.save("PREFLIGHT_PASSED", permissions=permissions)
        return {"bid": ticker.bid, "ask": ticker.ask, "ticker": ticker}

    def check_release(self) -> None:
        if self.switch.active:
            raise ValueError("PERSISTENT_STOP")
        if self.state.get("pending"):
            raise ValueError("PENDING_ORDER_RECONCILE_REQUIRED")
        if self.state["policy"] != json.loads(encode(asdict(INITIAL_POLICY))):
            # In-memory initial policy contains Decimals; serialize both sides.
            if encode(self.state["policy"]) != encode(asdict(INITIAL_POLICY)):
                raise ValueError("POLICY_CHANGED")
        if read_report(self.soak).get("testnet_status") != "REVIEW_REQUIRED":
            raise ValueError("TESTNET_ACCEPTANCE_NOT_PASSED")

    def limits(self, side: str, quantity: D, ask: D) -> None:
        cost = quantity * ask
        if (
            side not in ("BUY", "SELL")
            or not quantity.is_finite()
            or quantity <= 0
            or cost > INITIAL_POLICY.order_limit
        ):
            raise ValueError("ORDER_LIMIT")
        if side == "BUY":
            if self.state["loss_latched"] or self.state.get("cumulative_loss_latched"):
                raise ValueError("LOSS_LIMIT")
            if (
                cost * D("1.003") > D(self.state["cash"])
                or (D(self.state["btc"]) + quantity) * ask > INITIAL_POLICY.exposure_limit
            ):
                raise ValueError("BUDGET_LIMIT")
        elif quantity > D(self.state["btc"]):
            raise ValueError("NO_OWNED_POSITION")
        today = datetime.now(UTC).date()
        times = [t for t in self.state["times"] if datetime.fromtimestamp(t, UTC).date() == today]
        if len(times) >= 4 or time.time() - self.state["last_trade"] < 1800:
            raise ValueError("RATE_OR_COOLDOWN_LIMIT")

    def prepare(self, side: str, amount: str) -> dict[str, Any]:
        self.check_release()
        value = D(amount)
        if not value.is_finite() or not 0 < value <= INITIAL_POLICY.order_limit:
            raise ValueError("ORDER_LIMIT")
        market = self.preflight()
        assert self.adapter is not None
        maximum = value / (market["ask"] * D("1.003"))
        if side == "SELL":
            maximum = min(maximum, D(self.state["btc"]))
        quantity = market_quantity(
            maximum,
            self.adapter.get_exchange_info("BTCUSDT"),
            market["ticker"],
            self.adapter.get_average_price("BTCUSDT"),
            now=datetime.now(UTC),
            max_age=60,
        )
        self.limits(side, quantity, market["ask"])
        proposal = {
            "id": "live_" + uuid4().hex[:28],
            "symbol": "BTCUSDT",
            "side": side,
            "quantity": str(quantity),
            "estimated_usdt": str(quantity * market["ask"]),
            "ask": str(market["ask"]),
            "expires": time.time() + 120,
            "key_id": self.key_id,
            "policy_version": INITIAL_POLICY.version,
        }
        self.state["proposal"] = proposal
        self.save("ORDER_PREPARED", proposal=proposal)
        return proposal

    def approve(self, proposal_id: str, confirmation: str, reviewed: bool) -> dict[str, Any]:
        proposal = self.state.get("proposal")
        if (
            not proposal
            or proposal["id"] != proposal_id
            or confirmation != CONFIRMATION
            or reviewed is not True
        ):
            raise ValueError("EXPLICIT_ORDER_CONFIRMATION_REQUIRED")
        self.check_release()
        if os.getenv("ENABLE_LIVE_TRADING") != "true":
            raise ValueError("LIVE_EXECUTION_DISABLED")
        if proposal["expires"] < time.time() or proposal["key_id"] != self.key_id:
            raise ValueError("PROPOSAL_EXPIRED_OR_KEY_CHANGED")
        market = self.preflight()
        if abs(market["ask"] / D(proposal["ask"]) - 1) > D("0.003"):
            raise ValueError("PRICE_CHANGED_PREPARE_AGAIN")
        quantity = D(proposal["quantity"])
        self.limits(proposal["side"], quantity, market["ask"])
        assert self.adapter is not None
        checked = market_quantity(
            quantity,
            self.adapter.get_exchange_info("BTCUSDT"),
            market["ticker"],
            self.adapter.get_average_price("BTCUSDT"),
            now=datetime.now(UTC),
            max_age=60,
        )
        if checked != quantity or time.time() > proposal["expires"]:
            raise ValueError("FILTERS_CHANGED_PREPARE_AGAIN")
        if time.time() - market["ticker"].timestamp.timestamp() > 60:
            raise ValueError("STALE_PRICE")
        if self.switch.active:
            raise ValueError("PERSISTENT_STOP")
        self.state.update(pending=proposal_id, proposal=None, status="PENDING")
        self.save("HUMAN_APPROVED", proposal=proposal, performance_reviewed=True)
        self.transport.grant = {
            "symbol": "BTCUSDT",
            "side": proposal["side"],
            "type": "MARKET",
            "quantity": format(quantity.normalize(), "f"),
            "newClientOrderId": proposal_id,
            "newOrderRespType": "FULL",
        }
        try:
            self.adapter.place_order("BTCUSDT", proposal["side"], quantity, proposal_id)
            return self.reconcile()
        except Exception:
            self.switch.trip("LIVE_ORDER_REVIEW_REQUIRED")
            self.state["status"] = "BLOCKED"
            self.save("ORDER_UNRESOLVED", client_id=proposal_id)
            raise
        finally:
            self.transport.grant = None

    def reconcile(self) -> dict[str, Any]:
        cid = self.state.get("pending")
        if not cid or self.adapter is None or self.adapter.journal is None:
            raise ValueError("NO_PENDING_ORDER")
        if self.adapter.journal.expected_request(cid) is None:
            raise ValueError("INTENT_NOT_SENT_REQUIRES_REVIEW")
        result = self.adapter.reconcile("BTCUSDT", cid)
        candidate = copy.deepcopy(self.state)
        apply_fill(candidate, result)
        funds = {b.asset: b.free + b.locked for b in self.adapter.get_balances()}
        if (
            funds.get("BTC", D(0)) != D(candidate["base_btc"]) + D(candidate["btc"])
            or funds.get("USDT", D(0))
            != D(candidate["base_cash"]) + D(candidate["cash"]) - INITIAL_POLICY.capital
            or self.adapter.get_open_orders("BTCUSDT")
        ):
            raise ValueError("POST_FILL_ACCOUNT_MISMATCH")
        candidate["orders"] += 1
        candidate["status"] = "RECONCILED_STOPPED" if self.switch.active else "READY_FOR_REVIEW"
        self.state = candidate
        self.save("FILL", result=asdict(result))
        return self.status()

    def dispatch(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if action == "status":
            return self.status()
        if action == "preflight":
            self.preflight()
            return self.status()
        if action == "prepare":
            return self.prepare(str(body.get("side", "")), str(body.get("amount", "")))
        if action == "approve":
            return self.approve(
                str(body.get("id", "")),
                str(body.get("confirmation", "")),
                body.get("reviewed") is True,
            )
        if action == "reconcile":
            return self.reconcile()
        if action == "stop":
            self.switch.kill()
            self.state.update(proposal=None, status="STOPPED")
            self.save("MANUAL_STOP")
            return self.status()
        if action == "resume":
            if body.get("confirmation") != "我確認已核對帳本並解除停機":
                raise ValueError("RESUME_CONFIRMATION_REQUIRED")
            if self.state.get("pending"):
                raise ValueError("PENDING_ORDER_RECONCILE_REQUIRED")
            self.preflight()
            self.switch.resume()
            self.state.update(proposal=None, status="READY_FOR_REVIEW")
            self.save("MANUAL_RESUME")
            return self.status()
        raise ValueError("UNKNOWN_ACTION")


def operate(data: Path, soak: Path, action: str, body: dict[str, Any]) -> dict[str, Any]:
    data.mkdir(parents=True, exist_ok=True)
    if action == "stop":
        # Emergency stop must not wait behind a slow exchange request holding the worker lock.
        db = connect(data / "live.db")
        try:
            KillSwitch(Repository(db)).kill()
            return {"stop_active": True, "message": "IN_FLIGHT_ORDERS_REQUIRE_RECONCILIATION"}
        finally:
            db.close()
    with (data / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        control = LiveControl(data, soak)
        try:
            return control.dispatch(action, body)
        finally:
            control.db.close()

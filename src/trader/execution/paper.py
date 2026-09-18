"""Local-only atomic PAPER execution. This module has no order transport."""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trader.config import AppConfig, TradingMode
from trader.exchange.models import Account, AveragePrice, Balance, Ticker
from trader.execution.filters import FilterRejected, market_quantity
from trader.market.data import MarketObservation
from trader.models import Action, MarketSnapshot, Position
from trader.portfolio.valuation import finite
from trader.risk.engine import aware
from trader.risk.models import RiskContext
from trader.risk.service import RiskService
from trader.safety.kill_switch import KillSwitch
from trader.storage.repository import encode, json_default
from trader.storage.transaction import transaction
from trader.strategy.baseline import Strategy

ZERO = Decimal(0)


class PaperEngine:
    def __init__(
        self,
        connection: sqlite3.Connection,
        config: AppConfig,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        if config.mode != TradingMode.PAPER:
            raise ValueError("Paper commands require TRADING_MODE=PAPER")
        self.connection, self.config = connection, config
        self.risk = RiskService(connection, config.risk, kill_switch)

    def mark(self, markets: dict[str, MarketObservation], now: datetime) -> dict[str, Any]:
        """Record a fresh equity observation even when no new strategy candle closed."""
        with transaction(self.connection):
            needed = {s for s, (q, _) in self._positions().items() if q}
            if not needed <= markets.keys():
                raise ValueError("Missing held asset valuation")
            return self._mark(tuple(m.ticker for m in markets.values()), now)

    def initialize(self, now: datetime) -> None:
        if not aware(now):
            raise ValueError("Timezone-aware timestamp required")
        with transaction(self.connection):
            if self.connection.execute("SELECT 1 FROM paper_account").fetchone():
                return  # Never reset balances or daily stops on restart.
            for table in ("orders", "risk_assessments", "risk_days", "exchange_submissions"):
                if self.connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    raise ValueError("PAPER requires a separate, unused trading database")
            capital = str(self.config.risk.starting_capital_usd)
            self.connection.execute(
                "INSERT INTO paper_account VALUES (1,?,?,?, ?,?)",
                (capital, capital, "0", "0", json_default(now)),
            )
            self.risk.initialize_day(
                now.astimezone(UTC).date(),
                Decimal(capital),
                source="PAPER inception: cash only, no earlier trades",
            )

    def _state(self) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Run paper-init with a dedicated PAPER database first")
        if not isinstance(row, sqlite3.Row):
            raise ValueError("Invalid database row factory")
        if not finite(Decimal(row["cash"])):
            raise ValueError("Invalid paper cash")
        return row

    def _positions(self) -> dict[str, tuple[Decimal, Decimal]]:
        result = {
            row["symbol"]: (Decimal(row["quantity"]), Decimal(row["cost_basis"]))
            for row in self.connection.execute("SELECT * FROM paper_positions")
        }
        if any(not finite(q) or not finite(cost) for q, cost in result.values()):
            raise ValueError("Invalid paper position")
        return result

    def status(self) -> dict[str, Any]:
        with transaction(self.connection):
            state = self._state()
            latest = self.connection.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "mode": "PAPER",
                "simulated": True,
                "cash": state["cash"],
                "starting_capital": state["starting_capital"],
                "realized_pnl": state["realized_pnl"],
                "fees": state["fees"],
                "positions": self._positions(),
                "last_valuation": dict(latest) if latest else None,
                "trades": self.connection.execute(
                    "SELECT count(*) FROM risk_executions"
                ).fetchone()[0],
            }

    def _baseline(self, markets: dict[str, MarketObservation], now: datetime) -> None:
        if self.risk.day_state(now.date()) is not None:
            return
        # No PAPER trades can occur on a new day until its opening value is established.
        equity = Decimal(self._state()["cash"])
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for symbol, (quantity, _) in self._positions().items():
            if quantity == 0:
                continue
            candle = next((c for c in markets[symbol].candles_1h if c.timestamp == midnight), None)
            if candle is None or not finite(candle.open) or candle.open <= 0:
                return  # Risk engine records a missing baseline rejection, never invents one.
            equity += quantity * candle.open
        self.risk.initialize_day(
            now.date(), equity, source="PAPER ledger at UTC midnight + 1h candle opens"
        )

    def _snapshot(
        self, market: MarketObservation, tickers: tuple[Ticker, ...], now: datetime
    ) -> MarketSnapshot:
        state, positions = self._state(), self._positions()
        prices = {t.symbol: t.last_price for t in tickers}
        unrealized = sum((q * prices[s] - cost for s, (q, cost) in positions.items() if q), ZERO)
        q, cost = positions.get(market.symbol, (ZERO, ZERO))
        daily_realized = sum(
            (
                Decimal(json.loads(row[0])["realized_delta"])
                for row in self.connection.execute(
                    "SELECT result_json FROM paper_cycles WHERE substr(timestamp,1,10)=?",
                    (now.date().isoformat(),),
                )
            ),
            ZERO,
        )
        return MarketSnapshot(
            market.symbol,
            market.ticker.timestamp,
            market.ticker.last_price,
            market.ticker.bid,
            market.ticker.ask,
            market.ticker.spread,
            market.candles_1m,
            market.candles_5m,
            market.candles_15m,
            market.candles_1h,
            market.ticker.volume_24h,
            q,
            Decimal(state["cash"]),
            cost / q if q else ZERO,
            daily_realized,
            unrealized,
            self.risk.history(now).trades_today,
        )

    def prepare_snapshot(
        self, symbol: str, markets: dict[str, MarketObservation], now: datetime
    ) -> MarketSnapshot:
        if not aware(now) or symbol not in self.config.risk.symbols:
            raise ValueError("Whitelisted symbol and timezone-aware time required")
        with transaction(self.connection):
            needed = {symbol} | {s for s, (q, _) in self._positions().items() if q}
            if not needed <= markets.keys() or any(
                s != m.symbol or s != m.ticker.symbol for s, m in markets.items()
            ):
                raise ValueError("Incomplete or mismatched market observations")
            return self._snapshot(markets[symbol], tuple(m.ticker for m in markets.values()), now)

    def step(
        self,
        symbol: str,
        markets: dict[str, MarketObservation],
        strategy: Strategy,
        *,
        cycle_id: str,
        now: datetime,
        average: AveragePrice | None = None,
    ) -> dict[str, Any]:
        if not cycle_id or len(cycle_id) > 128 or not aware(now):
            raise ValueError("Cycle ID and timezone-aware time required")
        now = now.astimezone(UTC)
        if symbol not in self.config.risk.symbols:
            raise ValueError("Symbol is not whitelisted")
        request_key = encode(
            {"symbol": symbol, "strategy": strategy.name, "risk": asdict(self.config.risk)}
        )
        with transaction(self.connection):
            self._state()
            existing = self.connection.execute(
                "SELECT * FROM paper_cycles WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
            if existing:
                if existing["request_key"] != request_key:
                    raise ValueError("Cycle ID reused with a different request")
                result: dict[str, Any] = json.loads(existing["result_json"])
                return result
            previous = self.connection.execute(
                "SELECT MAX(timestamp) FROM paper_cycles"
            ).fetchone()[0]
            if previous and now < datetime.fromisoformat(previous):
                raise ValueError("PAPER clock cannot move backward")
            needed = {symbol} | {s for s, (q, _) in self._positions().items() if q}
            if not needed <= markets.keys() or any(
                s != m.symbol or s != m.ticker.symbol for s, m in markets.items()
            ):
                raise ValueError("Incomplete or mismatched market observations")
            self._baseline(markets, now)
            tickers = tuple(m.ticker for m in markets.values())
            snapshot = self._snapshot(markets[symbol], tickers, now)
            decision = strategy.propose(snapshot, now)
            if decision.proposal.symbol != symbol:
                raise ValueError("Strategy changed the selected symbol")
            state, positions = self._state(), self._positions()
            balances = (Balance("USDT", Decimal(state["cash"]), ZERO),) + tuple(
                Balance(s.removesuffix("USDT"), q, ZERO) for s, (q, _) in positions.items()
            )
            # These verified flags apply to the atomic local simulator, not Binance key permissions.
            account = Account(now, now, "SPOT", True, False, balances, True)
            context = RiskContext(
                account, tickers, decision.expected_edge_bps, True, True, True, True
            )
            assessed = self.risk.assess(decision.proposal, context, now=now)
            reasons = list(assessed.result.reasons)
            quantity = ZERO
            if assessed.result.approved:
                try:
                    quantity = market_quantity(
                        assessed.result.maximum_quantity,
                        markets[symbol].metadata,
                        markets[symbol].ticker,
                        average,
                        now=now,
                        max_age=self.config.risk.max_data_age_seconds,
                    )
                except FilterRejected as error:
                    reasons.append(str(error))
            accepted = assessed.result.approved and not reasons
            timestamp = json_default(now)
            cursor = self.connection.execute(
                "INSERT INTO market_snapshots(timestamp,symbol,snapshot_json) VALUES (?,?,?)",
                (
                    timestamp,
                    symbol,
                    encode(
                        {
                            "strategy_snapshot": asdict(snapshot),
                            "markets": {s: asdict(m) for s, m in markets.items()},
                            "average": asdict(average) if average else None,
                        }
                    ),
                ),
            )
            market_id = cursor.lastrowid
            p = decision.proposal
            cursor = self.connection.execute(
                "INSERT INTO ai_decisions(market_snapshot_id,timestamp,symbol,action,confidence,"
                "requested_notional,reason,raw_response) VALUES (?,?,?,?,?,?,?,?)",
                (
                    market_id,
                    timestamp,
                    symbol,
                    p.action,
                    str(p.confidence),
                    str(p.requested_notional_usd),
                    p.reason,
                    encode(
                        {
                            "provider": decision.audit.provider
                            if decision.audit
                            else "deterministic_baseline",
                            "strategy": strategy.name,
                            "decision": asdict(decision),
                        }
                    ),
                ),
            )
            cursor = self.connection.execute(
                "INSERT INTO risk_decisions(ai_decision_id,approved,rejection_reason,"
                "approved_notional,"
                "rules_snapshot_json) VALUES (?,?,?,?,?)",
                (
                    cursor.lastrowid,
                    int(accepted),
                    ",".join(reasons) or None,
                    str(assessed.result.approved_notional if accepted else ZERO),
                    encode(
                        {
                            "assessment_id": assessed.assessment_id,
                            "rules": asdict(self.config.risk),
                            "checks": asdict(assessed.result),
                            "execution_rejections": reasons,
                        }
                    ),
                ),
            )
            risk_id = cursor.lastrowid
            realized_delta = ZERO
            order_id = None
            if accepted:
                assert risk_id is not None
                order_id, realized_delta = self._fill(
                    p.action,
                    symbol,
                    quantity,
                    markets[symbol].ticker,
                    risk_id,
                    assessed.assessment_id,
                    cycle_id,
                    now,
                )
            portfolio = self._mark(tickers, now)
            result = {
                "cycle_id": cycle_id,
                "symbol": symbol,
                "simulated": True,
                "action": p.action,
                "strategy": strategy.name,
                "assessment_id": assessed.assessment_id,
                "status": "FILLED"
                if accepted
                else "HOLD"
                if p.action == Action.HOLD
                else "REJECTED",
                "order_id": order_id,
                "reasons": reasons,
                "strategy_reason": p.reason,
                "signal_proxy_bps": str(decision.expected_edge_bps),
                "estimated_round_trip_cost_bps": (
                    str(assessed.result.estimated_round_trip_cost_bps)
                    if assessed.result.estimated_round_trip_cost_bps is not None
                    else None
                ),
                "realized_delta": str(realized_delta),
                "portfolio": portfolio,
            }
            self.connection.execute(
                "INSERT INTO paper_cycles VALUES (?,?,?,?)",
                (cycle_id, request_key, timestamp, encode(result)),
            )
            return result

    def _fill(
        self,
        action: Action,
        symbol: str,
        quantity: Decimal,
        ticker: Ticker,
        risk_id: int,
        assessment_id: int,
        cycle_id: str,
        now: datetime,
    ) -> tuple[int, Decimal]:
        if self.risk.kill_switch.active:
            raise ValueError("Kill switch active")
        state = self._state()
        q, cost = self._positions().get(symbol, (ZERO, ZERO))
        slip = self.config.risk.estimated_slippage_rate
        price = (
            max(ticker.ask, ticker.last_price) * (1 + slip)
            if action == Action.BUY
            else min(ticker.bid, ticker.last_price) * (1 - slip)
        )
        notional = quantity * price
        fee = notional * self.config.risk.estimated_fee_rate
        cash, realized = Decimal(state["cash"]), ZERO
        if action == Action.BUY:
            cash -= notional + fee
            q, cost = q + quantity, cost + notional + fee
        elif action == Action.SELL and q >= quantity:
            removed_cost = cost * quantity / q
            realized = notional - fee - removed_cost
            cash += notional - fee
            q, cost = q - quantity, cost - removed_cost
        else:
            raise ValueError("Invalid simulated side or inventory")
        if cash < 0 or q < 0:
            raise ValueError("Simulator balance invariant failed")
        timestamp = json_default(now)
        client_id = "paper_" + cycle_id
        cursor = self.connection.execute(
            "INSERT INTO orders(risk_decision_id,client_order_id,symbol,side,type,requested_qty,"
            "executed_qty,average_fill_price,status,fee,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                risk_id,
                client_id,
                symbol,
                action,
                "MARKET",
                str(quantity),
                str(quantity),
                str(price),
                "FILLED",
                str(fee),
                timestamp,
                timestamp,
            ),
        )
        assert cursor.lastrowid is not None
        order_id = cursor.lastrowid
        self.connection.execute(
            "INSERT INTO fills(order_id,exchange_trade_id,quantity,price,fee,fee_asset,"
            "fee_usdt,timestamp) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                order_id,
                "paper-fill-" + str(order_id),
                str(quantity),
                str(price),
                str(fee),
                "USDT",
                str(fee),
                timestamp,
            ),
        )
        self.connection.execute(
            "UPDATE paper_account SET cash=?,realized_pnl=?,fees=? WHERE id=1",
            (
                str(cash),
                str(Decimal(state["realized_pnl"]) + realized),
                str(Decimal(state["fees"]) + fee),
            ),
        )
        self.connection.execute(
            "INSERT INTO paper_positions VALUES (?,?,?) ON CONFLICT(symbol) "
            "DO UPDATE SET quantity=excluded.quantity,cost_basis=excluded.cost_basis",
            (symbol, str(q), str(cost)),
        )
        self.risk.record_execution(client_id, assessment_id, now)
        self.connection.execute(
            "INSERT INTO system_events(timestamp,severity,event_type,payload_json) "
            "VALUES (?,?,?,?)",
            (
                timestamp,
                "INFO",
                "PAPER_FILL",
                encode(
                    {
                        "order_id": order_id,
                        "price": price,
                        "fee_usdt": fee,
                        "reference_price": max(ticker.ask, ticker.last_price)
                        if action == Action.BUY
                        else min(ticker.bid, ticker.last_price),
                        "slippage_usdt": abs(
                            price
                            - (
                                max(ticker.ask, ticker.last_price)
                                if action == Action.BUY
                                else min(ticker.bid, ticker.last_price)
                            )
                        )
                        * quantity,
                    }
                ),
            ),
        )
        return order_id, realized

    def _mark(self, tickers: tuple[Ticker, ...], now: datetime) -> dict[str, str]:
        state, positions = self._state(), self._positions()
        if any(
            not finite(t.last_price)
            or t.last_price <= 0
            or not aware(t.timestamp)
            or not 0 <= (now - t.timestamp).total_seconds() <= self.config.risk.max_data_age_seconds
            for t in tickers
        ):
            return {
                "valuation_status": "UNAVAILABLE",
                "cash": state["cash"],
                "fees": state["fees"],
                "realized_pnl": state["realized_pnl"],
            }
        prices = {t.symbol: t.last_price for t in tickers}
        marked = tuple(
            Position(s, q, cost / q if q else ZERO, prices[s])
            for s, (q, cost) in positions.items()
            if q
        )
        equity = Decimal(state["cash"]) + sum((p.quantity * p.market_price for p in marked), ZERO)
        unrealized = sum((q * prices[s] - cost for s, (q, cost) in positions.items() if q), ZERO)
        self.connection.execute(
            "INSERT INTO portfolio_snapshots(timestamp,cash,positions_json,equity,"
            "realized_pnl,unrealized_pnl) "
            "VALUES (?,?,?,?,?,?)",
            (
                json_default(now),
                state["cash"],
                encode([asdict(p) for p in marked]),
                str(equity),
                state["realized_pnl"],
                str(unrealized),
            ),
        )
        return {
            "cash": state["cash"],
            "equity": str(equity),
            "realized_pnl": state["realized_pnl"],
            "unrealized_pnl": str(unrealized),
            "fees": state["fees"],
            "net_pnl": str(equity - Decimal(state["starting_capital"])),
            "valued_at": json_default(now),
        }

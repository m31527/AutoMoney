"""Atomic risk audit and durable UTC daily stop. This service cannot submit orders."""

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal

from trader.config import RiskConfig
from trader.models import TradeProposal
from trader.portfolio.valuation import finite
from trader.risk.engine import RiskEngine, aware
from trader.risk.models import DayState, RecordedEvaluation, RiskContext, TradeHistory
from trader.safety.kill_switch import KillSwitch
from trader.storage.repository import Repository, encode, json_default
from trader.storage.transaction import transaction


class RiskService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        config: RiskConfig,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.connection = connection
        self.config = config
        self.engine = RiskEngine(config)
        self.kill_switch = kill_switch or KillSwitch(Repository(connection))

    def initialize_day(self, day: date, opening_equity: Decimal, *, source: str) -> None:
        """Accept a verified UTC opening baseline from the portfolio ledger/operator.

        Never infer today's opening equity from a later observation. Transfers or missing
        pre-start history require external reconciliation; the strategy cannot call this API.
        """
        if not finite(opening_equity) or opening_equity == 0 or not source.strip():
            raise ValueError("Verified positive baseline and source are required")
        with transaction(self.connection):
            old = self.connection.execute(
                "SELECT opening_equity FROM risk_days WHERE day=?", (day.isoformat(),)
            ).fetchone()
            if old:
                if Decimal(old[0]) != opening_equity:
                    raise ValueError("Daily baseline is immutable")
                return
            self.connection.execute(
                "INSERT INTO risk_days(day,opening_equity,baseline_source) VALUES (?,?,?)",
                (day.isoformat(), str(opening_equity), source),
            )

    def day_state(self, day: date) -> DayState | None:
        row = self.connection.execute(
            "SELECT opening_equity,loss_latched FROM risk_days WHERE day=?", (day.isoformat(),)
        ).fetchone()
        return DayState(day, Decimal(row[0]), bool(row[1])) if row else None

    def history(self, now: datetime) -> TradeHistory:
        times = [
            (datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1]))
            for row in self.connection.execute(
                "SELECT first_fill_at,last_fill_at FROM risk_executions"
            )
        ]
        if any(not aware(first) or not aware(last) or first > last for first, last in times):
            raise ValueError("Corrupt execution timestamp")
        return TradeHistory(
            sum(first.astimezone(UTC).date() == now.astimezone(UTC).date() for first, _ in times),
            max(last for _, last in times) if times else None,
        )

    def record_execution(
        self,
        client_order_id: str,
        assessment_id: int,
        first_fill_at: datetime,
        *,
        last_fill_at: datetime | None = None,
    ) -> None:
        """Called by the future executor after confirmed nonzero fills.

        One order counts once regardless of how many fills it later receives. This is an
        accounting boundary, not an alternative to the executor's final pre-order checks.
        """
        last_fill_at = last_fill_at or first_fill_at
        if (
            not client_order_id
            or not aware(first_fill_at)
            or not aware(last_fill_at)
            or last_fill_at < first_fill_at
        ):
            raise ValueError("Execution identity and aware fill timestamp required")
        timestamp = json_default(first_fill_at)
        latest = json_default(last_fill_at)
        with transaction(self.connection):
            assessment = self.connection.execute(
                "SELECT timestamp,result_json FROM risk_assessments WHERE id=?", (assessment_id,)
            ).fetchone()
            if (
                not assessment
                or json.loads(assessment[1])["approved"] is not True
                or first_fill_at < datetime.fromisoformat(assessment[0])
            ):
                raise ValueError("Execution requires an earlier approved assessment")
            existing = self.connection.execute(
                "SELECT assessment_id,first_fill_at,last_fill_at FROM risk_executions "
                "WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if existing:
                if tuple(existing[:2]) != (assessment_id, timestamp):
                    raise ValueError("Conflicting execution record")
                if last_fill_at <= datetime.fromisoformat(existing[2]):
                    return
                self.connection.execute(
                    "UPDATE risk_executions SET last_fill_at=? WHERE client_order_id=?",
                    (latest, client_order_id),
                )
            else:
                self.connection.execute(
                    "INSERT INTO risk_executions VALUES (?,?,?,?)",
                    (client_order_id, assessment_id, timestamp, latest),
                )
            self.connection.execute(
                "INSERT INTO system_events(timestamp,severity,event_type,payload_json) "
                "VALUES (?,?,?,?)",
                (
                    latest,
                    "INFO",
                    "RISK_EXECUTION_CONFIRMED",
                    encode(
                        {
                            "client_order_id": client_order_id,
                            "assessment_id": assessment_id,
                            "first_fill_at": timestamp,
                            "last_fill_at": latest,
                        }
                    ),
                ),
            )

    def _unresolved_submissions(self) -> bool:
        rows = self.connection.execute(
            "SELECT (SELECT observation_json FROM exchange_observations o "
            "WHERE o.client_order_id=s.client_order_id ORDER BY id DESC LIMIT 1) "
            "FROM exchange_submissions s"
        ).fetchall()
        for row in rows:
            if row[0] is None:
                return True
            observation = json.loads(row[0])
            if observation.get("error") == "EXCHANGE_REQUEST_REJECTED":
                continue
            if observation.get("fills_complete") is not True or observation.get("order", {}).get(
                "status"
            ) not in ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"):
                return True
        return False

    def assess(
        self, proposal: TradeProposal, context: RiskContext, *, now: datetime
    ) -> RecordedEvaluation:
        if not aware(now):
            raise ValueError("Timezone-aware evaluation time required")
        now = now.astimezone(UTC)
        with transaction(self.connection):
            day = self.day_state(now.date())
            history = self.history(now)
            effective = replace(
                context,
                no_pending_orders=(
                    context.no_pending_orders is True and not self._unresolved_submissions()
                ),
            )
            result = self.engine.evaluate(
                proposal, effective, day, history, now=now, killed=self.kill_switch.active
            )
            if result.daily_loss_triggered and day and not day.loss_latched:
                self.connection.execute(
                    "UPDATE risk_days SET loss_latched=1 WHERE day=?", (now.date().isoformat(),)
                )
                self.connection.execute(
                    "INSERT INTO system_events(timestamp,severity,event_type,payload_json) "
                    "VALUES (?,?,?,?)",
                    (
                        json_default(now),
                        "CRITICAL",
                        "DAILY_LOSS_LIMIT_TRIGGERED",
                        encode({"day": now.date().isoformat(), "pnl": result.daily_pnl}),
                    ),
                )
            inputs = {
                "observations": asdict(effective),
                "history": asdict(history),
                "day": {
                    "date": day.day.isoformat(),
                    "opening_equity": day.opening_equity,
                    "loss_latched": day.loss_latched,
                }
                if day
                else None,
            }
            cursor = self.connection.execute(
                "INSERT INTO risk_assessments(timestamp,proposal_json,context_json,"
                "rules_json,result_json) "
                "VALUES (?,?,?,?,?)",
                (
                    json_default(now),
                    encode(asdict(proposal)),
                    encode(inputs),
                    encode(asdict(self.config)),
                    encode(asdict(result)),
                ),
            )
            assert cursor.lastrowid is not None
            return RecordedEvaluation(cursor.lastrowid, result)

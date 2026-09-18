import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trader.market.data import MarketObservation
from trader.models import MarketSnapshot, PortfolioSnapshot


def json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"Unsupported JSON type: {type(value).__name__}")


def encode(value: object) -> str:
    return json.dumps(value, default=json_default, allow_nan=False, sort_keys=True)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SafetyState:
    killed: bool
    reason: str
    updated_at: str


class Repository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def safety_state(self) -> SafetyState:
        row = self.connection.execute("SELECT * FROM system_state WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("Missing safety state; refusing operation")
        return SafetyState(bool(row["killed"]), row["reason"], row["updated_at"])

    def set_killed(self, killed: bool, reason: str, *, critical: bool = False) -> SafetyState:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.safety_state()  # A damaged database must not silently recreate safety state.
            timestamp = utc_now()
            self.connection.execute(
                "UPDATE system_state SET killed=?, reason=?, updated_at=? WHERE id=1",
                (int(killed), reason, timestamp),
            )
            self.connection.execute(
                "INSERT INTO system_events(timestamp,severity,event_type,payload_json) "
                "VALUES (?,?,?,?)",
                (
                    timestamp,
                    ("CRITICAL" if critical else "WARNING") if killed else "INFO",
                    "KILL_SWITCH_ACTIVATED" if killed else "KILL_SWITCH_RESUMED",
                    encode({"reason": reason}),
                ),
            )
            state = self.safety_state()
        return state

    def events(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection.execute("SELECT * FROM system_events ORDER BY id")
        ]

    def save_market_snapshot(self, snapshot: MarketSnapshot | MarketObservation) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO market_snapshots(timestamp,symbol,snapshot_json) VALUES (?,?,?)",
                (json_default(snapshot.timestamp), snapshot.symbol, encode(asdict(snapshot))),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def save_portfolio_snapshot(self, snapshot: PortfolioSnapshot) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO portfolio_snapshots(timestamp,cash,positions_json,equity,"
                "realized_pnl,unrealized_pnl) VALUES (?,?,?,?,?,?)",
                (
                    json_default(snapshot.timestamp),
                    str(snapshot.cash),
                    encode([asdict(p) for p in snapshot.positions]),
                    str(snapshot.equity),
                    str(snapshot.realized_pnl),
                    str(snapshot.unrealized_pnl),
                ),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

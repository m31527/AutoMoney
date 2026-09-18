"""Persist before submitting; an existing ID is NEVER submitted a second time."""

import json
import sqlite3
from dataclasses import asdict

from trader.exchange.errors import DuplicateOrderConflict, MalformedResponse
from trader.exchange.models import Reconciliation
from trader.storage.repository import encode, utc_now


class ExchangeJournal:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def expected_request(self, client_id: str) -> dict[str, str] | None:
        row = self.connection.execute(
            "SELECT request_json FROM exchange_submissions WHERE client_order_id=?", (client_id,)
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise MalformedResponse()
        return value

    def claim(self, client_id: str, request: dict[str, str]) -> bool:
        payload = encode(request)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT request_json FROM exchange_submissions WHERE client_order_id=?",
                (client_id,),
            ).fetchone()
            if row:
                if row[0] != payload:
                    raise DuplicateOrderConflict()
                return False
            self.connection.execute(
                "INSERT INTO exchange_submissions VALUES (?,?,?)", (client_id, payload, utc_now())
            )
        return True

    def observe(self, client_id: str, value: object) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO exchange_observations(client_order_id,timestamp,observation_json) "
                "VALUES (?,?,?)",
                (client_id, utc_now(), encode(value)),
            )

    def save_reconciliation(self, result: Reconciliation) -> None:
        client_id = result.order.client_order_id
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            for fill in result.fills:
                payload = encode(asdict(fill))
                existing = self.connection.execute(
                    "SELECT fill_json FROM exchange_fills WHERE client_order_id=? AND trade_id=?",
                    (client_id, fill.trade_id),
                ).fetchone()
                if existing and existing[0] != payload:
                    raise MalformedResponse()
                self.connection.execute(
                    "INSERT OR IGNORE INTO exchange_fills VALUES (?,?,?)",
                    (client_id, fill.trade_id, payload),
                )
            self.connection.execute(
                "INSERT INTO exchange_observations(client_order_id,timestamp,observation_json) "
                "VALUES (?,?,?)",
                (client_id, utc_now(), encode(asdict(result))),
            )

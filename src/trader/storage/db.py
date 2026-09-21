"""SQLite bootstrap. Future migrations must increment user_version explicitly."""

import sqlite3
from pathlib import Path

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    killed INTEGER NOT NULL CHECK (killed IN (0, 1)),
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO system_state VALUES (1, 0, 'Initial state', strftime('%Y-%m-%dT%H:%M:%fZ'));
CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
    severity TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY, market_snapshot_id INTEGER NOT NULL REFERENCES market_snapshots(id),
    timestamp TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
    confidence TEXT NOT NULL, requested_notional TEXT NOT NULL,
    reason TEXT NOT NULL, raw_response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY, ai_decision_id INTEGER NOT NULL REFERENCES ai_decisions(id),
    approved INTEGER NOT NULL CHECK (approved IN (0, 1)), rejection_reason TEXT,
    approved_notional TEXT NOT NULL, rules_snapshot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, risk_decision_id INTEGER NOT NULL REFERENCES risk_decisions(id),
    exchange_order_id TEXT, client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    type TEXT NOT NULL, requested_qty TEXT NOT NULL, executed_qty TEXT NOT NULL,
    average_fill_price TEXT, status TEXT NOT NULL, fee TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
    exchange_trade_id TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
    fee TEXT NOT NULL, fee_asset TEXT NOT NULL, fee_usdt TEXT,
    timestamp TEXT NOT NULL, UNIQUE(order_id, exchange_trade_id)
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, cash TEXT NOT NULL,
    positions_json TEXT NOT NULL, equity TEXT NOT NULL,
    realized_pnl TEXT NOT NULL, unrealized_pnl TEXT NOT NULL
);
PRAGMA user_version = 1;
COMMIT;
"""

# Adapter-level submission journal, separate from risk-approved execution orders.
# Append-only observations keep the complete exchange history for Phase C integration.
MIGRATION_2 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS exchange_submissions (
    client_order_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_observations (
    id INTEGER PRIMARY KEY,
    client_order_id TEXT NOT NULL REFERENCES exchange_submissions(client_order_id),
    timestamp TEXT NOT NULL,
    observation_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_fills (
    client_order_id TEXT NOT NULL REFERENCES exchange_submissions(client_order_id),
    trade_id INTEGER NOT NULL,
    fill_json TEXT NOT NULL,
    PRIMARY KEY(client_order_id, trade_id)
);
PRAGMA user_version = 2;
COMMIT;
"""

MIGRATION_3 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS risk_days (
    day TEXT PRIMARY KEY,
    opening_equity TEXT NOT NULL,
    baseline_source TEXT NOT NULL,
    loss_latched INTEGER NOT NULL DEFAULT 0 CHECK (loss_latched IN (0,1))
);
CREATE TABLE IF NOT EXISTS risk_assessments (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_executions (
    client_order_id TEXT PRIMARY KEY,
    assessment_id INTEGER NOT NULL UNIQUE REFERENCES risk_assessments(id),
    first_fill_at TEXT NOT NULL,
    last_fill_at TEXT NOT NULL
);
PRAGMA user_version = 3;
COMMIT;
"""


MIGRATION_4 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK(id=1),
    starting_capital TEXT NOT NULL, cash TEXT NOT NULL,
    realized_pnl TEXT NOT NULL, fees TEXT NOT NULL, initialized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL, cost_basis TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_cycles (
    cycle_id TEXT PRIMARY KEY, request_key TEXT NOT NULL,
    timestamp TEXT NOT NULL, result_json TEXT NOT NULL
);
PRAGMA user_version = 4;
COMMIT;
"""


MIGRATION_5 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS ai_calls (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    prompt TEXT NOT NULL, response TEXT NOT NULL, error TEXT
);
PRAGMA user_version = 5;
COMMIT;
"""


MIGRATION_6 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS ai_call_diagnostics (
    call_id INTEGER PRIMARY KEY REFERENCES ai_calls(id),
    timestamp TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL
);
PRAGMA user_version = 6;
COMMIT;
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6):
            raise RuntimeError("Unsupported database schema version")
        if version == 0:
            connection.executescript(SCHEMA)
        if version < 2:
            connection.executescript(MIGRATION_2)
        if version < 3:
            connection.executescript(MIGRATION_3)
        if version < 4:
            connection.executescript(MIGRATION_4)
        if version < 5:
            connection.executescript(MIGRATION_5)
        if version < 6:
            connection.executescript(MIGRATION_6)
        return connection
    except Exception:
        connection.close()
        raise

"""Read-only evidence report. Never authorizes or submits a production order."""

import argparse
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from trader.launch_policy import INITIAL_POLICY
from trader.storage.repository import encode


def assess(state: dict[str, Any], events: list[dict[str, Any]], stopped: bool) -> dict[str, Any]:
    from dataclasses import asdict
    from datetime import datetime

    # Prior faults remain stored; evaluate only the explicitly restarted observation window.
    recovery = max(
        (i for i, e in enumerate(events) if e.get("event") == "RECOVERY_VERIFIED"), default=-1
    )
    events = events[recovery + 1 :]
    started = state.get("policy_started", 0)
    last = datetime.fromisoformat(state["last_cycle"]).timestamp() if state.get("last_cycle") else 0
    sides = {e["result"]["order"]["side"] for e in events if e.get("event") == "FILL"}
    checks = {
        "current_policy": state.get("policy_version") == INITIAL_POLICY.version,
        "48h_observed": started > 0 and last - started >= 48 * 3600 - 600,
        "500_cycles": state.get("cycles", 0) >= 500,
        "max_gap_15m": 0 < state.get("max_gap_seconds", 0) <= 900,
        "strategy_buy_and_sell": {"BUY", "SELL"} <= sides,
        "no_pending_order": state.get("pending") is None,
        "no_persistent_stop": not stopped,
        "observation_completed": state.get("status") == "OBSERVATION_COMPLETE_REVIEW_REQUIRED",
        "no_faults": not any(e.get("event") == "BLOCKED" for e in events),
    }
    return {
        "testnet_checks": checks,
        "testnet_status": "REVIEW_REQUIRED" if all(checks.values()) else "NOT_PASSED",
        "live_status": "BLOCKED",
        "live_blockers": [
            "PRODUCTION_KEY_PERMISSIONS_NOT_VERIFIED",
            "AUTHENTICATED_PER_ORDER_APPROVAL_REQUIRED",
            "STRATEGY_PERFORMANCE_REVIEW_REQUIRED",
        ],
        "policy": asdict(INITIAL_POLICY),
        "software_capabilities": {
            "production_preflight": True,
            "production_order_entry": True,
            "authenticated_manual_release": True,
        },
        "orders_submitted": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/soak"))
    args = parser.parse_args()
    print(encode(read_report(args.data)))


@contextmanager
def readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Read WAL safely on read-only Docker mounts, including after the writer exits.

    A closed WAL database can require new shm files even for mode=ro. Only copy
    a checkpointed main file when no WAL exists and its identity stays stable.
    Active WAL databases are always read by SQLite itself, never copied piecemeal.
    """
    wal = Path(str(path) + "-wal")
    with tempfile.TemporaryDirectory(prefix="readiness-") as folder:
        source = path
        if not wal.exists():
            before = path.stat()
            source = Path(folder) / "snapshot.db"
            shutil.copyfile(path, source)
            after = path.stat()
            if wal.exists() or (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise sqlite3.OperationalError("READINESS_CHANGED_RETRY")
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            yield connection
        finally:
            connection.close()


def read_report(data: Path) -> dict[str, Any]:
    path = (data / "soak.db").resolve()
    if not path.is_file():
        return {"available": False, "live_status": "BLOCKED"}
    with readonly_connection(path) as db:
        row = db.execute("SELECT payload FROM soak_state WHERE id=1").fetchone()
        if not row:
            return {"available": False, "live_status": "BLOCKED"}
        state = json.loads(row[0])
        events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM soak_events")]
        # Read the existing safety repository without migrations or writes.
        from trader.storage.repository import Repository

        db.row_factory = sqlite3.Row
        stopped = Repository(db).safety_state().killed
        report = assess(state, events, stopped)
        report.update(
            available=True,
            observation={
                key: state.get(key)
                for key in (
                    "status",
                    "cycles",
                    "orders",
                    "policy_started",
                    "deadline",
                    "last_cycle",
                    "equity",
                    "btc",
                )
            },
        )
        return report


if __name__ == "__main__":
    main()

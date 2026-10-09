"""Read-only evidence report. Never authorizes or submits a production order."""

import argparse
import json
import sqlite3
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
            "PRODUCTION_EXECUTION_NOT_IMPLEMENTED",
            "MANUAL_RELEASE_NOT_IMPLEMENTED",
            "STRATEGY_PERFORMANCE_REVIEW_REQUIRED",
        ],
        "policy": asdict(INITIAL_POLICY),
        "orders_submitted": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/soak"))
    args = parser.parse_args()
    print(encode(read_report(args.data)))


def read_report(data: Path) -> dict[str, Any]:
    path = (data / "soak.db").resolve()
    if not path.is_file():
        return {"available": False, "live_status": "BLOCKED"}
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
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

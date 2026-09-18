import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from tests.fixtures.risk import NOW, context, proposal
from trader.config import RiskConfig
from trader.models import Action
from trader.risk.service import RiskService
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import MIGRATION_2, SCHEMA, connect
from trader.storage.exchange_journal import ExchangeJournal
from trader.storage.repository import Repository

D = Decimal


class RiskServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "risk.db"
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)
        self.service = RiskService(self.connection, RiskConfig())
        self.service.initialize_day(
            NOW.date(), D("1000"), source="Verified synthetic opening ledger"
        )

    def assess(self, *, c=None, p=None, now=NOW, service=None):
        return (service or self.service).assess(p or proposal(), c or context(now=now), now=now)

    def test_audit_captures_observations_rules_calculations_and_rejections(self):
        assessment = self.assess(p=proposal(requested_notional_usd=D("101")))
        row = self.connection.execute(
            "SELECT * FROM risk_assessments WHERE id=?", (assessment.assessment_id,)
        ).fetchone()
        self.assertEqual(json.loads(row["proposal_json"])["requested_notional_usd"], "101")
        self.assertEqual(json.loads(row["rules_json"])["max_order_notional_usd"], "100")
        self.assertIn("ORDER_NOTIONAL_LIMIT", json.loads(row["result_json"])["reasons"])
        self.assertIn("balances", json.loads(row["context_json"])["observations"]["account"])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM orders").fetchone()[0], 0)

    def test_daily_loss_persists_after_restart_and_price_recovery(self):
        self.assertFalse(self.assess(c=context("980")).result.approved)
        other = connect(self.path)
        self.addCleanup(other.close)
        restarted = RiskService(other, RiskConfig())
        result = self.assess(c=context("1100"), service=restarted).result
        self.assertIn("DAILY_BUY_LOSS_STOP", result.reasons)
        self.assertTrue(restarted.day_state(NOW.date()).loss_latched)

    def test_daily_stop_event_occurs_once_and_resume_does_not_clear_it(self):
        for _ in range(3):
            self.assess(c=context("975"))
        KillSwitch(Repository(self.connection)).resume()
        self.assertIn("DAILY_BUY_LOSS_STOP", self.assess().result.reasons)
        events = [
            e
            for e in Repository(self.connection).events()
            if e["event_type"] == "DAILY_LOSS_LIMIT_TRIGGERED"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["severity"], "CRITICAL")

    def test_new_utc_day_requires_new_baseline(self):
        self.assess(c=context("980"))
        tomorrow = NOW + timedelta(days=1)
        result = self.assess(now=tomorrow).result
        self.assertIn("DAY_BASELINE_AVAILABLE", result.reasons)
        self.service.initialize_day(tomorrow.date(), D("1000"), source="Next verified opening")
        self.assertTrue(self.assess(now=tomorrow).result.approved)

    def test_baseline_cannot_be_replaced_to_erase_loss(self):
        self.assess(c=context("980"))
        with self.assertRaises(ValueError):
            self.service.initialize_day(NOW.date(), D("980"), source="Attempted reset")
        self.service.initialize_day(NOW.date(), D("1000"), source="Retry original")
        self.assertTrue(self.service.day_state(NOW.date()).loss_latched)

    def test_hold_can_latch_loss_but_never_counts_or_orders(self):
        value = self.assess(
            p=proposal(action=Action.HOLD, requested_notional_usd=D(0)), c=context("980")
        )
        self.assertFalse(value.result.approved)
        self.assertTrue(self.service.day_state(NOW.date()).loss_latched)
        self.assertEqual(self.service.history(NOW).trades_today, 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.service.record_execution("hold", value.assessment_id, NOW)

    def test_six_confirmed_orders_block_seventh_across_restart(self):
        for i in range(6):
            time = NOW - timedelta(hours=6 - i)
            assessment = self.assess(now=time)
            self.assertTrue(assessment.result.approved, assessment.result.reasons)
            self.service.record_execution(f"order-{i}", assessment.assessment_id, time)
        other = connect(self.path)
        self.addCleanup(other.close)
        restarted = RiskService(other, RiskConfig())
        self.assertIn("DAILY_TRADE_LIMIT", self.assess(service=restarted).result.reasons)

    def test_approval_alone_does_not_count_trade(self):
        for _ in range(8):
            self.assertTrue(self.assess().result.approved)
        self.assertEqual(self.service.history(NOW).trades_today, 0)

    def test_execution_is_idempotent_and_cooldown_uses_latest_fill(self):
        earlier = NOW - timedelta(hours=1)
        assessment = self.assess(now=earlier)
        for _ in range(2):
            self.service.record_execution("same", assessment.assessment_id, earlier)
        self.service.record_execution(
            "same", assessment.assessment_id, earlier, last_fill_at=NOW - timedelta(minutes=1)
        )
        self.assertEqual(self.service.history(NOW).trades_today, 1)
        self.assertIn("COOLDOWN", self.assess().result.reasons)
        with self.assertRaises(ValueError):
            self.service.record_execution("same", assessment.assessment_id, NOW)

    def test_unresolved_journal_overrides_claim_of_no_pending_orders(self):
        ExchangeJournal(self.connection).claim("unknown", {"symbol": "BTCUSDT"})
        self.assertIn("NO_PENDING_ORDERS", self.assess().result.reasons)

    def test_rejected_submission_does_not_block_forever(self):
        journal = ExchangeJournal(self.connection)
        journal.claim("rejected", {"symbol": "BTCUSDT"})
        journal.observe("rejected", {"error": "EXCHANGE_REQUEST_REJECTED"})
        self.assertTrue(self.assess().result.approved)

    def test_runtime_and_persistent_kill_both_block(self):
        switch = KillSwitch(Repository(self.connection))
        service = RiskService(self.connection, RiskConfig(), switch)
        switch.kill()
        KillSwitch(Repository(self.connection)).resume()
        self.assertIn("KILL_SWITCH_CLEAR", self.assess(service=service).result.reasons)
        switch.resume()
        switch.kill()
        self.assertIn("KILL_SWITCH_CLEAR", self.assess().result.reasons)

    def test_daily_latch_and_audit_roll_back_together(self):
        self.connection.execute(
            "CREATE TRIGGER no_audit BEFORE INSERT ON risk_assessments "
            "BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.assess(c=context("980"))
        self.assertFalse(self.service.day_state(NOW.date()).loss_latched)
        self.assertEqual(Repository(self.connection).events(), [])

    def test_stale_or_unreconciled_data_cannot_latch_daily_loss(self):
        c = context("970", now=NOW - timedelta(minutes=5))
        self.assess(c=c)
        self.assertFalse(self.service.day_state(NOW.date()).loss_latched)
        self.assess(c=replace(context("970"), account_reconciled=False))
        self.assertFalse(self.service.day_state(NOW.date()).loss_latched)

    def test_v2_migration_keeps_safety_and_exchange_journal(self):
        path = Path(self.temp.name) / "old.db"
        old = sqlite3.connect(path)
        old.row_factory = sqlite3.Row
        old.executescript(SCHEMA)
        old.executescript(MIGRATION_2)
        Repository(old).set_killed(True, "Preserve stop")
        ExchangeJournal(old).claim("old-order", {"symbol": "BTCUSDT"})
        old.close()
        migrated = connect(path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertTrue(Repository(migrated).safety_state().killed)
        self.assertIsNotNone(ExchangeJournal(migrated).expected_request("old-order"))

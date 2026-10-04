import json
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

from tests.fixtures.risk import NOW
from tests.integration.test_research import snapshot_fixture
from trader.config import RiskConfig
from trader.replay import prepare
from trader.strategy.budget import trade_budget
from trader.strategy.research import compact_context


class ReplayTests(unittest.TestCase):
    def test_all_cases_without_future_labels_and_deduplicated(self):
        snapshot = snapshot_fixture()
        budget = trade_budget(snapshot, RiskConfig(), Decimal(0))
        data = json.loads(
            compact_context(
                snapshot,
                NOW,
                budget,
                {"estimated_round_trip_cost_bps": "40", "signal_proxy_bps": "5"},
            )
        )
        data["future_return"] = "secret-future-label"
        records = []
        for action in ("BUY", "HOLD"):
            records.append(
                {
                    "research_input": data,
                    "proposal": {"action": action},
                    "post_response_forward": "secret-future-label",
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "summary.json",
                    json.dumps({"activity": {"ollama": {"shadow_ai": {"results": records}}}}),
                )
            cases = prepare(path)
        self.assertEqual(len(cases), 1)
        self.assertNotIn("secret-future-label", json.dumps(cases))
        self.assertNotIn("deterministic_edge_proxy_bps", cases[0]["context"])
        parsed = json.loads(cases[0]["context"])
        self.assertEqual(parsed["cost_reference"]["round_trip_cost_pct"], "0.4")
        self.assertTrue(parsed["exit_policy"]["future_exit_allowed_subject_to_checks"])

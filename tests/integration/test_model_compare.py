import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trader.model_compare import compare, main
from trader.strategy.provider import ProviderError


def row(arm, action="HOLD", status="OK"):
    return dict(
        case_id="a",
        arm=arm,
        input_sha256="same",
        status=status,
        proposal={"action": action},
        metadata={"usage": {"input_tokens": 10}},
        seconds=1,
    )


class ModelCompareTests(unittest.TestCase):
    def test_only_completed_identical_inputs_form_pairs(self):
        result = compare([row("local"), row("cloud", "BUY")], ["a", "b"])
        self.assertEqual(result["complete_pairs"], 1)
        self.assertEqual(result["incomplete_pairs"], 1)
        self.assertEqual(result["action_disagreements"], 1)
        self.assertIsNone(result["cost_usd"])

    def test_failure_is_not_hold(self):
        result = compare([row("local"), row("cloud", status="ERROR")], ["a"])
        self.assertEqual(result["complete_pairs"], 0)
        self.assertEqual(result["arms"]["cloud"]["actions"], {})

    def test_mismatched_inputs_rejected(self):
        cloud = row("cloud")
        cloud["input_sha256"] = "different"
        with self.assertRaises(ValueError):
            compare([row("local"), cloud], ["a"])


class OrchestrationTests(unittest.TestCase):
    def test_same_inputs_alternating_order_and_stop_on_failure(self):
        calls = []

        class Provider:
            def __init__(self, *, backend, model, replay):
                self.backend, self.model = backend, model
                self.last_metadata = {}
                assert replay

            def complete(self, instructions, context):
                calls.append((self.backend, instructions, context))
                if len(calls) == 3:
                    raise ProviderError()
                return json.dumps(
                    dict(
                        symbol="BTCUSDT",
                        action="HOLD",
                        confidence=0.5,
                        requested_notional_usd=0,
                        reason="test",
                        time_horizon_minutes=240,
                        invalid_if=[],
                    )
                )

        cases = [
            dict(
                case_id=str(i),
                instructions="fixed",
                context=json.dumps({"snapshot": {"symbol": "BTCUSDT"}, "case": i}),
            )
            for i in range(3)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            with (
                patch("trader.model_compare.prepare", return_value=cases),
                patch("trader.model_compare.OpenTeddyProvider", Provider),
                patch(
                    "sys.argv",
                    [
                        "compare",
                        "input.zip",
                        "--output",
                        str(out),
                        "--run-cloud",
                        "--cloud-model",
                        "test",
                    ],
                ),
            ):
                main()
            report = json.loads((out / "comparison.json").read_text())
        self.assertEqual([c[0] for c in calls], ["ollama", "openai", "openai"])
        self.assertEqual(calls[0][1:], calls[1][1:])
        self.assertEqual(report["complete_pairs"], 1)
        self.assertEqual(report["incomplete_pairs"], 2)

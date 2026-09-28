import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.entry_research import research, strength


class EntryResearchTests(unittest.TestCase):
    def event(self, minute, price, symbol="BTCUSDT", quote_minute=None):
        now = datetime(2026, 9, 28, tzinfo=UTC)
        return {
            "timestamp": (now + timedelta(minutes=minute)).isoformat(),
            "payload": {
                "comparison_eligible": True, "symbol": symbol,
                "quote_timestamp": (now + timedelta(
                    minutes=minute if quote_minute is None else quote_minute
                )).isoformat(),
                "reference_price": str(price), "signal_proxy_bps": "10",
                "hourly_direction": {"state": "UP", "signed_gap_bps": "50"},
                "cost_only_would_skip": True, "estimated_round_trip_cost_bps": "40",
            },
        }

    def test_blocked_entries_are_included_and_cost_subtracted_once(self):
        result = research([self.event(0, 100), self.event(65, 101)])
        self.assertEqual(result["observations"], 2)
        first = result["samples"][0]["forward"]["1h"]
        self.assertEqual(Decimal(first["price_return_bps"]), 100)
        self.assertEqual(Decimal(first["after_estimated_cost_bps"]), 60)
        group = next(g for g in result["groups"] if g["dimension"] == "all")
        self.assertEqual(group["forward"]["1h"]["matched"], 1)
        self.assertEqual(group["forward"]["1h"]["unmatched"], 1)
        self.assertIsNone(group["forward"]["4h"]["mean_price_return_bps"])

    def test_no_early_quote_other_symbol_or_late_match(self):
        result = research([
            self.event(0, 100), self.event(60, 200, quote_minute=59),
            self.event(65, 300, "ETHUSDT"), self.event(71, 400),
        ])
        self.assertIsNone(result["samples"][0]["forward"]["1h"])

    def test_exclusions_and_fixed_boundaries(self):
        occupied = self.event(0, 100)
        occupied["payload"]["comparison_eligible"] = False
        result = research([{"payload": {}}, occupied])
        self.assertEqual(result["observations"], 0)
        self.assertEqual(result["excluded_missing_direction"], 1)
        self.assertEqual(result["excluded_not_flat_or_fresh"], 1)
        self.assertEqual([strength(v) for v in [0, -15, 30, 45, None]],
                         ["0-15", "15-30", "30-45", "45+", "UNAVAILABLE"])

import json
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from tests.fixtures.paper import market
from tests.fixtures.risk import NOW
from trader.config import RiskConfig
from trader.models import MarketSnapshot
from trader.strategy.budget import trade_budget
from trader.strategy.research import compact_context, research_prompt


def snapshot_fixture():
    m = market()
    return MarketSnapshot(
        m.symbol,
        NOW,
        m.ticker.last_price,
        m.ticker.bid,
        m.ticker.ask,
        m.ticker.spread,
        m.candles_1m,
        m.candles_5m,
        m.candles_15m,
        m.candles_1h,
        m.ticker.volume_24h,
        Decimal(0),
        Decimal(1000),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        0,
    )


class ResearchTests(unittest.TestCase):
    def test_closed_candles_budget_and_size(self):
        snapshot = snapshot_fixture()
        future = replace(snapshot.candles_1h[-1], timestamp=NOW + timedelta(hours=1))
        snapshot = replace(snapshot, candles_1h=snapshot.candles_1h + (future,))
        budget = trade_budget(snapshot, RiskConfig(), Decimal(0))
        base = compact_context(snapshot, NOW, budget, {})
        instructions, raw = research_prompt(base, "control", "unique-call")
        data = json.loads(raw)
        self.assertNotIn(future.timestamp.isoformat(), raw)
        self.assertEqual(data["input_receipt"]["can_buy"], budget.max_buy_notional_usd > 0)
        self.assertEqual(data["input_receipt"]["horizon_minutes"], 240)
        self.assertLessEqual(len((instructions + raw).encode()), 10000)
        self.assertEqual(data["snapshot"]["symbol"], snapshot.symbol)

    def test_variants_keep_identical_market(self):
        snapshot = snapshot_fixture()
        budget = trade_budget(snapshot, RiskConfig(), Decimal(0))
        base = compact_context(snapshot, NOW, budget, {"signal_proxy_bps": "10"})
        a = json.loads(research_prompt(base, "control", "same")[1])
        b = json.loads(research_prompt(base, "unanchored", "same")[1])
        self.assertEqual(a.pop("deterministic_edge_proxy_bps"), "10")
        self.assertEqual(a, b)

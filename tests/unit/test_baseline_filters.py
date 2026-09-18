import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from tests.fixtures.binance import FakeTransport, response
from tests.fixtures.paper import average, market
from tests.fixtures.risk import NOW
from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.models import LotFilter
from trader.execution.filters import FilterRejected, market_quantity
from trader.market.indicators import sma
from trader.models import Action, MarketSnapshot
from trader.strategy.baseline import HoldStrategy, SMAStrategy

D = Decimal


class FilterTests(unittest.TestCase):
    def quantity(self, maximum="0.001234", m=None, avg=None):
        m = m or market()
        return market_quantity(
            D(maximum), m.metadata, m.ticker, avg or average(), now=NOW, max_age=60
        )

    def test_quantity_rounds_down_not_up(self):
        self.assertEqual(self.quantity(), D("0.00123"))

    def test_intersection_of_non_power_of_ten_lot_steps(self):
        m = market()
        info = replace(
            m.metadata,
            lot_size=LotFilter(D("0.0025"), D("100"), D("0.0025")),
            market_lot_size=LotFilter(D("0.003"), D("100"), D("0.003")),
        )
        self.assertEqual(self.quantity("0.04", replace(m, metadata=info)), D("0.03"))

    def test_below_minimum_quantity_rejected(self):
        with self.assertRaises(FilterRejected):
            self.quantity("0.000001")

    def test_above_market_maximum_rejected(self):
        with self.assertRaises(FilterRejected):
            self.quantity("11")

    def test_average_window_and_freshness_must_match(self):
        for avg in (
            replace(average(), minutes=1),
            replace(average(), timestamp=NOW - timedelta(seconds=61)),
            replace(average(), symbol="ETHUSDT"),
        ):
            with self.assertRaises(FilterRejected):
                self.quantity(avg=avg)

    def test_missing_average_fails_closed(self):
        m = market()
        with self.assertRaises(FilterRejected):
            market_quantity(D("0.001"), m.metadata, m.ticker, None, now=NOW, max_age=60)

    def test_zero_window_uses_last_trade_price(self):
        m = market()
        info = replace(
            m.metadata,
            notional_filters=(replace(m.metadata.notional_filters[0], average_price_minutes=0),),
        )
        self.assertEqual(
            market_quantity(D("0.001"), info, m.ticker, None, now=NOW, max_age=60), D("0.001")
        )

    def test_minimum_notional_uses_reference_not_last_price(self):
        with self.assertRaisesRegex(FilterRejected, "MIN_NOTIONAL"):
            self.quantity("0.001", avg=average("1000"))

    def test_max_notional_market_flag(self):
        m = market()
        rule = replace(m.metadata.notional_filters[0], maximum=D("10"), apply_max_to_market=True)
        m = replace(m, metadata=replace(m.metadata, notional_filters=(rule,)))
        with self.assertRaisesRegex(FilterRejected, "MAX_NOTIONAL"):
            self.quantity(m=m)

    def test_halted_or_stale_metadata_rejected(self):
        m = market()
        for change in (
            {"status": "HALT"},
            {"market_allowed": False},
            {"fetched_at": NOW - timedelta(seconds=61)},
        ):
            with self.assertRaises(FilterRejected):
                self.quantity(m=replace(m, metadata=replace(m.metadata, **change)))

    def test_public_average_price_endpoint(self):
        stamp = int(NOW.timestamp() * 1000)
        transport = FakeTransport(
            response({"mins": 5, "price": "50000.000123", "closeTime": stamp})
        )
        result = BinanceSpotAdapter(transport=transport).get_average_price("BTCUSDT")
        self.assertEqual(result.price, D("50000.000123"))
        self.assertEqual(result.minutes, 5)
        self.assertNotIn("X-MBX-APIKEY", transport.calls[0][2])


class BaselineTests(unittest.TestCase):
    def snapshot(self, crossover=0, position="0"):
        m = market(crossover=crossover)
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
            D("100"),
            D(position),
            D("1000"),
            D("50000"),
            D(0),
            D(0),
            0,
        )

    def test_sma_exact_arithmetic(self):
        self.assertEqual(sma((D(1), D(2), D(3)), 2), D("2.5"))
        with self.assertRaises(ValueError):
            sma((D(1),), 2)

    def test_hold_strategy(self):
        result = HoldStrategy().propose(self.snapshot(10), NOW)
        self.assertEqual(result.proposal.action, Action.HOLD)
        self.assertEqual(result.proposal.requested_notional_usd, 0)

    def test_bullish_and_bearish_cross(self):
        self.assertEqual(SMAStrategy().propose(self.snapshot(10), NOW).proposal.action, Action.BUY)
        self.assertEqual(
            SMAStrategy().propose(self.snapshot(-10, "0.002"), NOW).proposal.action, Action.SELL
        )
        self.assertEqual(
            SMAStrategy().propose(self.snapshot(-10), NOW).proposal.action, Action.HOLD
        )

    def test_unclosed_candle_never_creates_signal(self):
        s = self.snapshot()
        new = replace(s.candles_5m[-1], timestamp=NOW, close=D("90000"))
        self.assertEqual(
            SMAStrategy()
            .propose(replace(s, candles_5m=s.candles_5m + (new,)), NOW)
            .proposal.action,
            Action.HOLD,
        )

    def test_missing_history_or_gap_holds(self):
        s = self.snapshot(10)
        for candles in (s.candles_5m[-5:], s.candles_5m[:10] + s.candles_5m[11:]):
            self.assertEqual(
                SMAStrategy().propose(replace(s, candles_5m=candles), NOW).proposal.action,
                Action.HOLD,
            )

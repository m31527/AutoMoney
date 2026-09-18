import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures.paper import average, market
from tests.fixtures.risk import NOW
from trader.config import AppConfig, TradingMode
from trader.exchange.errors import NetworkError
from trader.experiment import Experiment
from trader.market.data import StaleMarketData
from trader.models import Candle
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.repository import Repository

D = Decimal


def observations(now=NOW, price="50000", trend=10):
    output = {}
    for symbol, base in (("BTCUSDT", price), ("ETHUSDT", "2500")):
        m = market(symbol, base, now=now, crossover=trend)
        hourly_end = now.replace(minute=0, second=0, microsecond=0)
        hourly = tuple(
            Candle(
                hourly_end - timedelta(hours=24 - i),
                D(base),
                D(base),
                D(base),
                D(base) * (1 + D(trend) / 100) if i >= 19 else D(base),
                D(10),
            )
            for i in range(24)
        )
        output[symbol] = replace(m, candles_1h=hourly)
    return output


def averages(now=NOW, price="50000"):
    return {
        "BTCUSDT": average(price, now=now),
        "ETHUSDT": replace(average("2500", now=now), symbol="ETHUSDT"),
    }


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "paper.db"
        self.control = connect(self.path)
        self.addCleanup(self.control.close)
        self.config = AppConfig(mode=TradingMode.PAPER, database_url="sqlite:///" + str(self.path))
        self.switch = KillSwitch(Repository(self.control))
        self.exp = Experiment(self.config, self.switch)
        self.addCleanup(lambda: self.exp.close())

    def test_two_arms_share_limits_across_symbols_but_not_each_other(self):
        result = self.exp.sample(observations(), averages(), NOW)
        for name in ("sma5m", "trend1h"):
            portfolio = result["portfolios"][name]
            self.assertEqual(portfolio["stats"]["fills"], 1)
            self.assertEqual(portfolio["stats"]["rejected"], 1)
            self.assertEqual(portfolio["stats"]["reasons"]["COOLDOWN"], 1)
            self.assertGreater(D(portfolio["cash"]), D(949))
        self.assertEqual(self.control.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_baseline_costs_and_fixed_quantities(self):
        first = self.exp.sample(observations(), averages(), NOW)
        hold = first["portfolios"]["hold"]
        self.assertLess(D(hold["equity"]), D(1000))
        self.assertGreater(D(hold["fees"]), D(0))
        baseline = json.loads(
            self.exp.db.execute(
                "SELECT value FROM experiment_meta WHERE key='baseline'"
            ).fetchone()[0]
        )
        later = NOW + timedelta(minutes=5)
        result = self.exp.sample(observations(later, "55000"), averages(later, "55000"), later)
        change = D(result["portfolios"]["hold"]["equity"]) - D(hold["equity"])
        self.assertAlmostEqual(change, D(baseline["quantities"]["BTCUSDT"]) * 5000, places=20)
        self.assertEqual(result["portfolios"]["cash"]["equity"], "1000")

    def test_repeated_bucket_and_restart_do_not_repeat_fills_or_reprice_baseline(self):
        first = self.exp.sample(observations(), averages(), NOW)
        self.exp.close()
        self.exp = Experiment(self.config, self.switch)
        again = self.exp.sample(observations(), averages(), NOW)
        self.assertEqual(first, again)
        self.assertEqual(
            self.exp.db.execute("SELECT COUNT(*) FROM experiment_samples").fetchone()[0], 1
        )
        for engine in self.exp.engines.values():
            self.assertEqual(
                engine.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1
            )

    def test_hourly_signal_only_once_per_closed_candle(self):
        self.exp.sample(observations(), averages(), NOW)
        later = NOW + timedelta(minutes=5)
        result = self.exp.sample(observations(later), averages(later), later)
        self.assertEqual({r["arm"] for r in result["outcomes"]}, {"sma5m"})
        self.assertEqual(result["portfolios"]["trend1h"]["stats"]["fills"], 1)

    def test_kill_applies_to_both_arms_and_both_symbols(self):
        self.switch.kill()
        result = self.exp.sample(observations(), averages(), NOW)
        self.assertTrue(all(r["status"] == "REJECTED" for r in result["outcomes"]))
        self.assertEqual(len(result["outcomes"]), 4)
        for engine in self.exp.engines.values():
            self.assertEqual(
                engine.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0
            )

    def test_eth_can_trade_and_hourly_position_can_exit(self):
        markets = observations()
        markets["BTCUSDT"] = observations(trend=0)["BTCUSDT"]
        first = self.exp.sample(markets, averages(), NOW)
        fills = [r for r in first["outcomes"] if r["status"] == "FILLED"]
        self.assertEqual(len(fills), 2)
        self.assertEqual({r["symbol"] for r in fills}, {"ETHUSDT"})
        later = NOW + timedelta(hours=1)
        second = self.exp.sample(observations(later, trend=-10), averages(later), later)
        hourly = [
            r for r in second["outcomes"] if r["arm"] == "trend1h" and r["symbol"] == "ETHUSDT"
        ][0]
        self.assertEqual(hourly["action"], "SELL")
        self.assertEqual(hourly["status"], "FILLED", hourly)
        next_hour = later + timedelta(hours=1)
        third = self.exp.sample(observations(next_hour), averages(next_hour), next_hour)
        hourly_eth = [
            r for r in third["outcomes"] if r["arm"] == "trend1h" and r["symbol"] == "ETHUSDT"
        ][0]
        self.assertEqual(hourly_eth["action"], "BUY")

    def test_reject_stale_and_missing_market_before_inception(self):
        with self.assertRaises(StaleMarketData):
            self.exp.sample(observations(), averages(), NOW + timedelta(minutes=2))
        with self.assertRaises(ValueError):
            self.exp.sample({"BTCUSDT": observations()["BTCUSDT"]}, averages(), NOW)
        self.assertIsNone(
            self.exp.db.execute("SELECT value FROM experiment_meta WHERE key='baseline'").fetchone()
        )

    def test_config_changes_cannot_rewrite_experiment(self):
        changed = replace(
            self.config, risk=replace(self.config.risk, estimated_fee_rate=D("0.002"))
        )
        with self.assertRaises(ValueError):
            Experiment(changed, self.switch)

    def test_duplicate_worker_is_rejected(self):
        another = Experiment(self.config, self.switch)
        try:
            with self.exp.worker(), self.assertRaises(ValueError), another.worker():
                self.fail("second worker acquired lock")
        finally:
            another.close()

    def test_partial_round_preserves_inception_and_replay_does_not_duplicate(self):
        target = self.exp.engines["trend1h"]
        with patch.object(target, "step", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.exp.sample(observations(), averages(), NOW)
        before = self.exp.db.execute(
            "SELECT value FROM experiment_meta WHERE key='baseline'"
        ).fetchone()[0]
        self.assertEqual(
            self.exp.db.execute("SELECT COUNT(*) FROM experiment_samples").fetchone()[0], 0
        )
        self.exp.sample(observations(), averages(), NOW)
        self.assertEqual(
            before,
            self.exp.db.execute(
                "SELECT value FROM experiment_meta WHERE key='baseline'"
            ).fetchone()[0],
        )
        self.assertEqual(
            self.exp.engines["sma5m"]
            .connection.execute("SELECT COUNT(*) FROM fills")
            .fetchone()[0],
            1,
        )

    def test_report_includes_drawdown_costs_and_staleness_but_not_hold_rejections(self):
        self.exp.sample(observations(trend=0), averages(), NOW)
        later = NOW + timedelta(minutes=5)
        self.exp.sample(observations(later, "40000", trend=0), averages(later, "40000"), later)
        report = self.exp.report(later + timedelta(minutes=16))
        self.assertIn("超過 15 分鐘", report)
        self.assertIn("最大回落", report)
        self.assertIn("等待 4 次；拒絕 0 次", report)
        self.assertNotIn("可用資金或持幣不足", report)
        self.assertIn("-3.05%", report)  # BTC -20% on ~15% sleeve plus initial costs

    def test_network_failure_is_logged_then_next_round_recovers(self):
        exchange = MagicMock()
        exchange.get_average_price.side_effect = lambda s: averages()[s]
        calls = []
        with patch(
            "trader.experiment.collect_market",
            side_effect=[NetworkError(), *observations().values()],
        ):
            self.exp.run(
                exchange, cycles=2, emit=calls.append, clock=lambda: NOW, sleep=lambda _: None
            )
        self.assertIn("行情取得失敗", calls[0])
        self.assertEqual(
            self.exp.db.execute("SELECT COUNT(*) FROM experiment_errors").fetchone()[0], 1
        )
        self.assertEqual(
            self.exp.db.execute("SELECT COUNT(*) FROM experiment_samples").fetchone()[0], 1
        )

    def test_hourly_unclosed_candle_cannot_trigger_entry(self):
        markets = observations(trend=0)
        candle = Candle(NOW, D(50000), D(100000), D(50000), D(100000), D(1))
        markets["BTCUSDT"] = replace(
            markets["BTCUSDT"], candles_1h=markets["BTCUSDT"].candles_1h + (candle,)
        )
        result = self.exp.sample(markets, averages(), NOW)
        hourly = [r for r in result["outcomes"] if r["arm"] == "trend1h"]
        self.assertTrue(all(r["action"] == "HOLD" for r in hourly))

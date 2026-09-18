import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from tests.fixtures.risk import NOW
from tests.integration.test_ai import proposal
from tests.integration.test_experiment import averages, observations
from trader.config import AppConfig, RiskConfig, TradingMode
from trader.dashboard import summary
from trader.experiment import Experiment
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.repository import Repository
from trader.strategy.contract import InvalidProposal, parse_proposal


class AltcoinTests(unittest.TestCase):
    def test_two_coin_experiment_values_new_assets_and_freezes_universe(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "paper.db"
            db = connect(path)
            self.addCleanup(db.close)
            config = AppConfig(
                mode=TradingMode.PAPER,
                database_url="sqlite:///" + str(path),
                risk=RiskConfig(symbols=("SOLUSDT", "XRPUSDT")),
            )
            switch = KillSwitch(Repository(db))
            exp = Experiment(config, switch)
            source, avg = observations(), averages()
            markets, prices = {}, {}
            for old, new in [("BTCUSDT", "SOLUSDT"), ("ETHUSDT", "XRPUSDT")]:
                m = source[old]
                markets[new] = replace(
                    m,
                    symbol=new,
                    ticker=replace(m.ticker, symbol=new),
                    metadata=replace(m.metadata, symbol=new, base_asset=new[:-4]),
                )
                prices[new] = replace(avg[old], symbol=new)
            result = exp.sample(markets, prices, NOW)
            self.assertEqual(result["portfolios"]["trend1h"]["stats"]["fills"], 1)
            info = summary(path.parent)
            self.assertEqual(set(info["markets"]), {"SOLUSDT", "XRPUSDT"})
            self.assertTrue(any(p["positions"] for p in info["holdings"]))
            baseline = json.loads(
                exp.db.execute("SELECT value FROM experiment_meta WHERE key='baseline'").fetchone()[
                    0
                ]
            )
            self.assertEqual(Decimal(baseline["cash"]), Decimal("700"))
            exp.close()
            with self.assertRaises(ValueError):
                Experiment(replace(config, risk=RiskConfig()), switch)

    def test_contract_accepts_new_symbol_but_not_cross_symbol_response(self):
        raw = proposal(symbol="SOLUSDT")
        self.assertEqual(parse_proposal(raw, "SOLUSDT").symbol, "SOLUSDT")
        with self.assertRaises(InvalidProposal):
            parse_proposal(raw, "XRPUSDT")

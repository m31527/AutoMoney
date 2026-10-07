import unittest
from decimal import Decimal as D
from unittest.mock import MagicMock, patch

from tests.fixtures.risk import NOW
from trader.exchange.errors import NetworkError, TradingDisabled
from trader.exchange.models import Balance, Fill, Order, Reconciliation
from trader.exchange.testnet_transport import TestnetTransport
from trader.testnet_acceptance import run


def result(side):
    order = Order(
        "BTCUSDT",
        1 if side == "BUY" else 2,
        side,
        side,
        "MARKET",
        "FILLED",
        D(".005"),
        D(".005"),
        D("50"),
        NOW,
    )
    fill = Fill(
        "BTCUSDT",
        order.exchange_order_id,
        order.exchange_order_id,
        D(".005"),
        D("10000"),
        D("50"),
        D(".05"),
        "USDT",
        NOW,
    )
    return Reconciliation(order, (fill,), True)


class AcceptanceTests(unittest.TestCase):
    def test_production_and_redirect_targets_blocked_before_network(self):
        for url in (
            "https://api.binance.com/api/v3/order",
            "https://testnet.binance.vision.evil/api/v3/order",
            "http://testnet.binance.vision/api/v3/order",
            "https://testnet.binance.vision/sapi/v1/capital/withdraw/apply",
        ):
            with self.assertRaises(TradingDisabled):
                TestnetTransport().request("POST", url, {}, b"", 1)

    def test_lost_buy_response_restart_reconciles_without_reposting(self):
        adapter = MagicMock()
        journal = {}
        adapter.journal.expected_request.side_effect = lambda cid: journal.get(cid)
        adapter.get_open_orders.return_value = ()
        adapter.get_ticker.return_value.ask = D("10000")
        state = {"run_id": "fixture"}
        adapter.get_balances.return_value = (Balance("USDT", D("1000"), D(0)),)

        def lost(symbol, side, quantity, cid):
            journal[cid] = side
            raise NetworkError()

        adapter.place_order.side_effect = lost
        with patch("trader.testnet_acceptance.market_quantity", return_value=D(".005")):
            with self.assertRaises(NetworkError):
                run(adapter, state, lambda s: None)
        adapter.place_order.reset_mock()

        def placed(symbol, side, quantity, cid):
            journal[cid] = side

        adapter.place_order.side_effect = placed
        adapter.reconcile.side_effect = lambda symbol, cid: result(journal[cid])
        adapter.get_balances.side_effect = [
            (Balance("BTC", D(".005"), D(0)), Balance("USDT", D("949.95"), D(0))),
            (Balance("BTC", D(0), D(0)), Balance("USDT", D("999.90"), D(0))),
        ]
        with patch("trader.testnet_acceptance.market_quantity", return_value=D(".005")):
            report = run(adapter, state, lambda s: None)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(adapter.place_order.call_count, 1)
        self.assertEqual(adapter.place_order.call_args.args[1], "SELL")

    def test_incomplete_fill_stops_before_sell(self):
        adapter = MagicMock()
        adapter.journal.expected_request.return_value = {"exists": "yes"}
        partial = result("BUY")
        adapter.reconcile.return_value = Reconciliation(partial.order, (), False)
        with self.assertRaises(ValueError):
            run(adapter, {"run_id": "fixture", "before": {}}, lambda s: None)
        adapter.place_order.assert_not_called()

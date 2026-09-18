from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from trader.exchange.models import Account, Balance, Ticker
from trader.models import Action, TradeProposal
from trader.risk.models import RiskContext

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
D = Decimal


def context(cash="1000", btc="0", eth="0", *, now=NOW):
    account = Account(
        now,
        now,
        "SPOT",
        True,
        False,
        (
            Balance("USDT", D(cash), D(0)),
            Balance("BTC", D(btc), D(0)),
            Balance("ETH", D(eth), D(0)),
        ),
        True,
    )
    tickers = (
        Ticker("BTCUSDT", now, now, D("50000"), D("49999"), D("50001"), D("10")),
        Ticker("ETHUSDT", now, now, D("2500"), D("2499.9"), D("2500.1"), D("100")),
    )
    return RiskContext(account, tickers, D("100"), True, True, True, True)


def proposal(**changes):
    value = TradeProposal("BTCUSDT", Action.BUY, D("0.8"), D("100"), "Synthetic fixture", 240)
    return replace(value, **changes)

"""Offline, synthetic risk examples. No exchange imports, credentials or order submission."""

import tempfile
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trader.config import RiskConfig
from trader.exchange.models import Account, Balance, Ticker
from trader.models import Action, TradeProposal
from trader.risk.models import RiskContext
from trader.risk.service import RiskService
from trader.storage.db import connect
from trader.storage.repository import encode


def main() -> None:
    now = datetime.now(UTC)
    account = Account(
        now, now, "SPOT", True, False, (Balance("USDT", Decimal("1000"), Decimal(0)),), True
    )
    ticker = Ticker(
        "BTCUSDT", now, now, Decimal("50000"), Decimal("49999"), Decimal("50001"), Decimal("100")
    )
    # All verification flags below refer ONLY to these synthetic observations.
    context = RiskContext(account, (ticker,), Decimal("100"), True, True, True, True)
    proposal = TradeProposal(
        "BTCUSDT", Action.BUY, Decimal("0.8"), Decimal("100"), "Synthetic offline example", 240
    )
    with tempfile.TemporaryDirectory() as directory:
        connection = connect(Path(directory) / "demo.db")
        try:
            service = RiskService(connection, RiskConfig())
            service.initialize_day(now.date(), Decimal("1000"), source="Synthetic example baseline")
            for label, p, c in (
                ("BUY_100", proposal, context),
                ("BUY_101", replace(proposal, requested_notional_usd=Decimal("101")), context),
                (
                    "DAILY_LOSS_20",
                    proposal,
                    replace(
                        context,
                        account=replace(
                            account, balances=(Balance("USDT", Decimal("980"), Decimal(0)),)
                        ),
                    ),
                ),
                ("RECOVERY_STILL_BLOCKED", proposal, context),
            ):
                result = service.assess(p, c, now=now)
                print(encode({"synthetic": True, "scenario": label, **asdict(result)}))
        finally:
            connection.close()


if __name__ == "__main__":
    main()

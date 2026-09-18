import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from tests.fixtures.risk import NOW, context, proposal
from trader.config import ConfigError, RiskConfig
from trader.exchange.models import Balance
from trader.models import Action
from trader.portfolio.valuation import value_portfolio
from trader.risk.engine import RiskEngine
from trader.risk.models import DayState, TradeHistory

D = Decimal


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine(RiskConfig())
        self.day = DayState(NOW.date(), D("1000"))

    def assess(self, p=None, c=None, history=None, day=None, killed=False):
        return self.engine.evaluate(
            p or proposal(),
            c or context(),
            day or self.day,
            history or TradeHistory(0, None),
            now=NOW,
            killed=killed,
        )

    def rejected(self, rule, **kwargs):
        result = self.assess(**kwargs)
        self.assertFalse(result.approved)
        self.assertIn(rule, result.reasons)
        self.assertEqual(result.maximum_quantity, 0)
        self.assertEqual(result.approved_notional, 0)
        return result

    def test_valid_buy_at_100_is_approved_with_adverse_price_quantity(self):
        result = self.assess()
        self.assertTrue(result.approved, result.reasons)
        self.assertEqual(result.approved_notional, D("100"))
        self.assertLess(result.maximum_quantity, D("0.002"))
        self.assertEqual(result.estimated_fee, D("0.1"))
        self.assertTrue(all(check.passed for check in result.checks))

    def test_101_order_rejected(self):
        self.rejected("ORDER_NOTIONAL_LIMIT", p=proposal(requested_notional_usd=D("101")))

    def test_exposure_over_300_rejected(self):
        self.rejected(
            "TOTAL_EXPOSURE_LIMIT",
            c=context("700", "0.003", "0.06"),
            p=proposal(requested_notional_usd=D("1")),
        )

    def test_total_exposure_exactly_300_allowed(self):
        # Keep both symbol exposures below the cost-adjusted allocation ceiling.
        result = self.assess(c=context("850", "0.001", "0.06"))
        self.assertTrue(result.approved, result.reasons)

    def test_unapproved_symbol(self):
        self.rejected("SYMBOL_WHITELIST", p=proposal(symbol="SOLUSDT"))

    def test_seventh_trade_rejected(self):
        self.rejected("DAILY_TRADE_LIMIT", history=TradeHistory(6, NOW - timedelta(hours=1)))

    def test_sixth_trade_allowed(self):
        self.assertTrue(self.assess(history=TradeHistory(5, NOW - timedelta(minutes=30))).approved)

    def test_cooldown_boundary(self):
        self.rejected("COOLDOWN", history=TradeHistory(1, NOW - timedelta(minutes=29, seconds=59)))
        self.assertTrue(self.assess(history=TradeHistory(1, NOW - timedelta(minutes=30))).approved)

    def test_previous_day_trade_still_enforces_cooldown(self):
        midnight = NOW.replace(hour=0, minute=5)
        result = self.engine.evaluate(
            proposal(),
            context(now=midnight),
            self.day,
            TradeHistory(0, midnight - timedelta(minutes=10)),
            now=midnight,
            killed=False,
        )
        self.assertIn("COOLDOWN", result.reasons)

    def test_daily_loss_exactly_20_blocks_buy(self):
        result = self.rejected("DAILY_BUY_LOSS_STOP", c=context("980"))
        self.assertEqual(result.daily_pnl, D("-20"))
        self.assertTrue(result.daily_loss_triggered)

    def test_loss_below_20_does_not_latch(self):
        result = self.assess(c=context("980.01"))
        self.assertTrue(result.approved)
        self.assertFalse(result.daily_loss_triggered)

    def test_unrealized_loss_counts(self):
        c = context("800", "0.004")
        tickers = (
            replace(c.tickers[0], last_price=D("45000"), bid=D("44999"), ask=D("45001")),
            c.tickers[1],
        )
        result = self.rejected("DAILY_BUY_LOSS_STOP", c=replace(c, tickers=tickers))
        self.assertEqual(result.current_equity, D("980"))

    def test_latched_loss_cannot_be_overridden_by_recovery(self):
        self.rejected(
            "DAILY_BUY_LOSS_STOP", day=replace(self.day, loss_latched=True), c=context("1100")
        )

    def test_sell_can_reduce_risk_after_loss_stop(self):
        result = self.assess(
            p=proposal(action=Action.SELL, requested_notional_usd=D("50")),
            c=context("880", "0.002"),
            day=replace(self.day, loss_latched=True),
        )
        self.assertTrue(result.approved, result.reasons)

    def test_hold_never_approves_quantity(self):
        self.rejected(
            "ACTIONABLE_TRADE", p=proposal(action=Action.HOLD, requested_notional_usd=D(0))
        )

    def test_leverage_margin_futures_shorting_withdrawals_rejected(self):
        for change in (
            {"leverage": D(2)},
            {"product": "MARGIN"},
            {"product": "FUTURES"},
            {"shorting": True},
            {"withdrawal": True},
            {"shorting": "false"},
        ):
            with self.subTest(change=change):
                self.rejected("SPOT_ONLY", p=proposal(**change))

    def test_sell_without_position_rejected(self):
        self.rejected("BALANCE_AND_NO_SHORTING", p=proposal(action=Action.SELL))

    def test_kill_blocks_orders(self):
        self.rejected("KILL_SWITCH_CLEAR", killed=True)

    def test_free_text_invalidation_conditions_are_not_ignored(self):
        self.rejected(
            "NO_UNINTERPRETED_CONDITIONS", p=proposal(invalid_if=("price moves more than 1%",))
        )

    def test_prices_age_boundary_and_future_dates(self):
        for age in (61, -1):
            c = context()
            c = replace(
                c,
                tickers=(
                    replace(c.tickers[0], timestamp=NOW - timedelta(seconds=age)),
                    c.tickers[1],
                ),
            )
            self.rejected("MARKET_DATA_FRESH_VALID", c=c)
        self.assertTrue(self.assess(c=context(now=NOW - timedelta(seconds=60))).approved)

    def test_stale_account_blocks_even_with_fresh_prices(self):
        c = context()
        self.rejected(
            "ACCOUNT_DATA_FRESH",
            c=replace(c, account=replace(c.account, fetched_at=NOW - timedelta(seconds=61))),
        )

    def test_old_account_update_time_not_confused_with_fetch_time(self):
        c = context()
        c = replace(c, account=replace(c.account, updated_at=NOW - timedelta(days=30)))
        self.assertTrue(self.assess(c=c).approved)

    def test_connectivity_permissions_and_reconciliation_required(self):
        for change in ({"connectivity_verified": False}, {"account_reconciled": False}):
            self.rejected("ACCOUNT_VERIFIED", c=replace(context(), **change))
        for change in (
            {"can_trade": False},
            {"key_permissions_verified": False},
            {"account_type": "MARGIN"},
        ):
            c = context()
            self.rejected("ACCOUNT_VERIFIED", c=replace(c, account=replace(c.account, **change)))

    def test_history_and_pending_order_uncertainty_rejected(self):
        self.rejected("HISTORY_COMPLETE", c=replace(context(), history_complete=False))
        self.rejected("NO_PENDING_ORDERS", c=replace(context(), no_pending_orders=False))

    def test_fee_slippage_spread_and_edge_checks(self):
        self.rejected(
            "FEES_SLIPPAGE_AND_STRATEGY_EDGE", c=replace(context(), expected_edge_bps=D("40"))
        )
        c = context()
        self.rejected(
            "FEES_SLIPPAGE_AND_STRATEGY_EDGE",
            c=replace(
                c, tickers=(replace(c.tickers[0], bid=D("49000"), ask=D("51000")), c.tickers[1])
            ),
        )

    def test_confidence_boundary(self):
        self.rejected("CONFIDENCE", p=proposal(confidence=D("0.6499")))
        self.assertTrue(self.assess(p=proposal(confidence=D("0.65"))).approved)

    def test_allocation_uses_current_equity_not_starting_capital(self):
        # Current equity 400 implies 80 maximum, despite configured starting capital 1000.
        self.rejected(
            "SYMBOL_ALLOCATION_LIMIT", c=context("400"), day=DayState(NOW.date(), D("400"))
        )

    def test_symbol_allocation_applies_to_existing_positions(self):
        self.rejected("SYMBOL_ALLOCATION_LIMIT", c=context("850", "0.003"))

    def test_fee_must_fit_free_cash(self):
        c = context()
        balances = (Balance("USDT", D("100"), D("900")), *c.account.balances[1:])
        self.rejected(
            "BALANCE_AND_NO_SHORTING", c=replace(c, account=replace(c.account, balances=balances))
        )

    def test_locked_crypto_counts_as_exposure_but_cannot_be_sold(self):
        c = context("900")
        balances = (c.account.balances[0], Balance("BTC", D(0), D("0.002")), c.account.balances[2])
        c = replace(c, account=replace(c.account, balances=balances))
        self.assertEqual(value_portfolio(c.account, c.tickers).equity, D("1000"))
        self.rejected("BALANCE_AND_NO_SHORTING", c=c, p=proposal(action=Action.SELL))

    def test_missing_nonzero_asset_valuation_rejected(self):
        c = context()
        c = replace(
            c,
            account=replace(c.account, balances=c.account.balances + (Balance("BNB", D(1), D(0)),)),
        )
        self.rejected("PORTFOLIO_VALUED", c=c)

    def test_missing_held_symbol_price_rejected(self):
        c = context("950", eth="0.02")
        self.rejected("PORTFOLIO_VALUED", c=replace(c, tickers=c.tickers[:1]))

    def test_duplicate_balances_and_prices_rejected(self):
        c = context()
        self.rejected("PORTFOLIO_VALUED", c=replace(c, tickers=c.tickers + c.tickers[:1]))
        self.rejected(
            "PORTFOLIO_VALUED",
            c=replace(
                c, account=replace(c.account, balances=c.account.balances + c.account.balances[:1])
            ),
        )

    def test_invalid_proposals_fail_closed(self):
        for change in (
            {"confidence": D("NaN")},
            {"requested_notional_usd": D("Infinity")},
            {"action": "BUY"},
            {"time_horizon_minutes": True},
            {"requested_notional_usd": D("-1")},
            {"confidence": D("1.1")},
        ):
            with self.subTest(change=change):
                self.rejected("VALID_PROPOSAL", p=proposal(**change))

    def test_missing_or_old_baseline_rejected(self):
        result = self.engine.evaluate(
            proposal(), context(), None, TradeHistory(0, None), now=NOW, killed=False
        )
        self.assertIn("DAY_BASELINE_AVAILABLE", result.reasons)
        self.rejected(
            "DAY_BASELINE_AVAILABLE", day=DayState((NOW - timedelta(days=1)).date(), D("1000"))
        )

    def test_history_future_timestamp_and_invalid_count(self):
        for history in (
            TradeHistory(-1, None),
            TradeHistory(1, None),
            TradeHistory(1, NOW + timedelta(seconds=1)),
        ):
            self.rejected("TRADE_HISTORY_VALID", history=history)

    def test_naive_now_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.evaluate(
                proposal(),
                context(),
                self.day,
                TradeHistory(0, None),
                now=NOW.replace(tzinfo=None),
                killed=False,
            )

    def test_new_configuration_bounds(self):
        for change in (
            {"max_data_age_seconds": 0},
            {"estimated_fee_rate": D("1")},
            {"estimated_slippage_rate": D("NaN")},
            {"max_execution_cost_bps": 0},
        ):
            with self.assertRaises(ConfigError):
                RiskConfig(**change)
        self.assertEqual(RiskConfig(estimated_fee_rate=D(0)).estimated_fee_rate, 0)

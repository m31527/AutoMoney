"""Fail-closed configuration; credentials are loaded separately for private queries only."""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path


class ConfigError(ValueError):
    pass


class TradingMode(StrEnum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


@dataclass(frozen=True)
class RiskConfig:
    starting_capital_usd: Decimal = Decimal("1000")
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    max_order_notional_usd: Decimal = Decimal("100")
    max_total_position_usd: Decimal = Decimal("300")
    max_daily_loss_usd: Decimal = Decimal("20")
    max_trades_per_day: int = 6
    min_minutes_between_trades: int = 30
    max_symbol_allocation_pct: Decimal = Decimal("20")
    minimum_confidence: Decimal = Decimal("0.65")
    leverage_allowed: bool = False
    shorting_allowed: bool = False
    withdrawals_allowed: bool = False
    max_data_age_seconds: int = 60
    estimated_fee_rate: Decimal = Decimal("0.001")
    estimated_slippage_rate: Decimal = Decimal("0.001")
    max_execution_cost_bps: Decimal = Decimal("50")
    minimum_net_edge_bps: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(field.default, Decimal):
                try:
                    number = Decimal(str(value))
                except InvalidOperation:
                    raise ConfigError(f"{field.name} must be a finite number") from None
                allow_zero = field.name in ("estimated_fee_rate", "estimated_slippage_rate")
                if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
                    raise ConfigError(f"{field.name} must be positive and finite")
                object.__setattr__(self, field.name, number)
        for name in ("max_trades_per_day", "min_minutes_between_trades", "max_data_age_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ConfigError(f"{name} must be a positive integer")
        for name in ("leverage_allowed", "shorting_allowed", "withdrawals_allowed"):
            if getattr(self, name) is not False:
                raise ConfigError(f"{name} must remain false")
        if not isinstance(self.symbols, (tuple, list)) or not self.symbols:
            raise ConfigError("symbols must be a nonempty list")
        if any(s not in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT") for s in self.symbols):
            raise ConfigError("Only BTCUSDT, ETHUSDT, SOLUSDT and XRPUSDT are supported")
        if len(set(self.symbols)) != len(self.symbols):
            raise ConfigError("symbols must not contain duplicates")
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if self.minimum_confidence > 1 or self.max_symbol_allocation_pct > 100:
            raise ConfigError("Confidence or allocation percentage exceeds its range")
        if self.estimated_fee_rate >= 1 or self.estimated_slippage_rate >= 1:
            raise ConfigError("Fee and slippage rates must be below one")
        if self.max_order_notional_usd > self.max_total_position_usd:
            raise ConfigError("Order limit must not exceed total exposure limit")
        if self.max_total_position_usd > self.starting_capital_usd:
            raise ConfigError("Exposure limit must not exceed starting capital")


@dataclass(frozen=True)
class AppConfig:
    mode: TradingMode = TradingMode.TESTNET
    enable_live_trading: bool = False
    database_url: str = "sqlite:///data/trading.db"
    risk: RiskConfig = RiskConfig()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, TradingMode):
            raise ConfigError("mode must be a TradingMode")
        if type(self.enable_live_trading) is not bool:
            raise ConfigError("enable_live_trading must be boolean")
        if self.mode == TradingMode.LIVE and not self.enable_live_trading:
            raise ConfigError("LIVE requires ENABLE_LIVE_TRADING=true")
        if not self.database_url.startswith("sqlite:///"):
            raise ConfigError("Only sqlite:/// URLs are currently supported")
        path = self.database_url.removeprefix("sqlite:///")
        if not path or path == ":memory:" or "?" in path:
            raise ConfigError("Database must be a persistent file without URL query parameters")

    @property
    def database_path(self) -> Path:
        return Path(self.database_url.removeprefix("sqlite:///"))


def load_config(path: Path | None = None, environ: Mapping[str, str] | None = None) -> AppConfig:
    env = os.environ if environ is None else environ
    values = {}
    if path is not None:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
    if set(values) - {field.name for field in fields(RiskConfig)}:
        raise ConfigError("Unknown risk configuration field")
    flag = env.get("ENABLE_LIVE_TRADING", "false")
    if flag not in ("true", "false"):
        raise ConfigError("ENABLE_LIVE_TRADING must be exactly true or false")
    try:
        mode = TradingMode(env.get("TRADING_MODE", "TESTNET"))
    except ValueError:
        raise ConfigError("TRADING_MODE must be PAPER, TESTNET or LIVE") from None
    return AppConfig(
        mode=mode,
        enable_live_trading=flag == "true",
        database_url=env.get("DATABASE_URL", "sqlite:///data/trading.db"),
        risk=RiskConfig(**values),
    )

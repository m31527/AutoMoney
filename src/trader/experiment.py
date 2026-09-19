"""Versioned, independent PAPER comparisons with shared observations and durable baselines."""

import fcntl
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trader.config import AppConfig, TradingMode
from trader.exchange.binance import BinanceSpotAdapter
from trader.exchange.errors import ExchangeError
from trader.exchange.models import AveragePrice
from trader.execution.paper import PaperEngine
from trader.market.data import MarketObservation, StaleMarketData, collect_market
from trader.safety.kill_switch import KillSwitch
from trader.storage.db import connect
from trader.storage.repository import encode, json_default
from trader.storage.transaction import transaction
from trader.strategy.baseline import HourlyTrendStrategy, SMAStrategy, Strategy

D = Decimal
ARMS: dict[str, tuple[Strategy, str, int]] = {
    "sma5m": (SMAStrategy(), "candles_5m", 5),
    "trend1h": (HourlyTrendStrategy(), "candles_1h", 60),
}
LABELS = {
    "sma5m": "5 分鐘 SMA",
    "trend1h": "1 小時趨勢",
    "hold": "持有配置幣種各 15%",
    "cash": "全現金",
}
REASONS = {
    "FEES_SLIPPAGE_AND_STRATEGY_EDGE": "成本／訊號門檻未通過",
    "COOLDOWN": "交易冷卻期間",
    "DAILY_TRADE_LIMIT": "當日成交次數上限",
    "KILL_SWITCH_CLEAR": "已暫停交易",
    "ORDER_NOTIONAL_LIMIT": "單筆額度上限",
    "TOTAL_EXPOSURE_LIMIT": "總持倉上限",
    "SYMBOL_ALLOCATION_LIMIT": "單幣配置上限",
    "BALANCE_AND_NO_SHORTING": "可用資金或持幣不足",
}


class Experiment:
    def __init__(self, config: AppConfig, switch: KillSwitch) -> None:
        if config.mode != TradingMode.PAPER or len(config.risk.symbols) != 2:
            raise ValueError("Comparison requires PAPER with exactly two configured symbols")
        self.config, self.switch = config, switch
        self.root = config.database_path.parent / "experiment-v2"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = connect(self.root / "comparison.db")
        self.engines: dict[str, PaperEngine] = {}
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS experiment_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS experiment_samples (
                    bucket INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS experiment_errors (
                    id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, code TEXT NOT NULL);
            """)
            definition = encode(
                {
                    "version": 2,
                    "risk": asdict(config.risk),
                    "arms": {k: v[0].name for k, v in ARMS.items()},
                    "benchmark_weight_per_symbol": "0.15",
                }
            )
            with transaction(self.db):
                old = self.db.execute(
                    "SELECT value FROM experiment_meta WHERE key='definition'"
                ).fetchone()
                if (
                    old
                    and encode(
                        {
                            **json.loads(old[0]),
                            "risk": {"exit_policy_version": 1, **json.loads(old[0])["risk"]},
                        }
                    )
                    != definition
                ):
                    raise ValueError(
                        "Experiment settings are immutable; use a new database directory"
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO experiment_meta VALUES ('definition',?)", (definition,)
                )
            for name in ARMS:
                path = self.root / (name + ".db")
                connection = connect(path)
                self.engines[name] = PaperEngine(
                    connection, replace(config, database_url="sqlite:///" + str(path)), switch
                )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for engine in self.engines.values():
            engine.connection.close()
        self.db.close()

    @contextmanager
    def worker(self) -> Iterator[None]:
        # OS lock is released on crash; reporting and kill/resume remain available.
        with (self.root / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another comparison worker is running") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def sample(
        self,
        markets: dict[str, MarketObservation],
        averages: dict[str, AveragePrice],
        now: datetime,
    ) -> dict[str, Any]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Aware timestamp required")
        now = now.astimezone(UTC)
        bucket = int(now.timestamp()) // 300
        if set(markets) != set(self.config.risk.symbols):
            raise ValueError("Both markets required")
        for symbol, market in markets.items():
            ticker = market.ticker
            if (
                symbol != market.symbol
                or symbol != ticker.symbol
                or not ticker.last_price.is_finite()
                or ticker.last_price <= 0
                or not -5
                <= (now - ticker.timestamp).total_seconds()
                <= self.config.risk.max_data_age_seconds
            ):
                raise StaleMarketData()
        # Persist a single inception independently of arm commits. A crash cannot reprice it.
        with transaction(self.db):
            initial = self.db.execute(
                "SELECT value FROM experiment_meta WHERE key='baseline'"
            ).fetchone()
            capital = self.config.risk.starting_capital_usd
            fee, slip = (
                self.config.risk.estimated_fee_rate,
                self.config.risk.estimated_slippage_rate,
            )
            if initial:
                baseline = json.loads(initial[0])
            else:
                budget = capital * D("0.15")
                quantities = {
                    s: budget / ((max(m.ticker.ask, m.ticker.last_price) * (1 + slip)) * (1 + fee))
                    for s, m in markets.items()
                }
                baseline = {
                    "timestamp": json_default(now),
                    "cash": capital - 2 * budget,
                    "quantities": quantities,
                    "fees": 2 * budget * fee / (1 + fee),
                }
                self.db.execute(
                    "INSERT INTO experiment_meta VALUES ('baseline',?)", (encode(baseline),)
                )
        with transaction(self.db):
            previous = self.db.execute(
                "SELECT bucket,payload FROM experiment_samples ORDER BY bucket DESC LIMIT 1"
            ).fetchone()
            if previous and bucket < previous[0]:
                raise ValueError("Experiment clock moved backward")
            if previous and bucket == previous[0]:
                result: dict[str, Any] = json.loads(previous[1])
                return result
            portfolios: dict[str, Any] = {}
            outcomes: list[dict[str, Any]] = []
            for name, (strategy, field, minutes) in ARMS.items():
                # Hourly strategies need hourly rotation; half-hour rotation aliases their clock.
                rotation_seconds = 3600 if minutes == 60 else 1800
                symbols = sorted(markets, reverse=int(now.timestamp()) // rotation_seconds % 2 == 1)
                engine = self.engines[name]
                engine.initialize(now)
                for symbol in symbols:
                    candles = [
                        c
                        for c in getattr(markets[symbol], field)
                        if c.timestamp + timedelta(minutes=minutes) <= now
                    ]
                    if not candles:
                        continue
                    candle = candles[-1].timestamp
                    identifier = name + ":" + symbol + ":" + json_default(candle)
                    exists = engine.connection.execute(
                        "SELECT 1 FROM paper_cycles WHERE cycle_id=?", (identifier,)
                    ).fetchone()
                    if exists:
                        continue
                    outcome = engine.step(
                        symbol,
                        markets,
                        strategy,
                        cycle_id=identifier,
                        now=now,
                        average=averages.get(symbol),
                    )
                    outcomes.append({"arm": name, "symbol": symbol, **outcome})
                portfolios[name] = engine.mark(markets, now)
                portfolios[name]["stats"] = statistics(engine.connection)
                if "equity" not in portfolios[name]:
                    raise StaleMarketData()
            hold_equity = D(str(baseline["cash"])) + sum(
                (
                    D(str(q)) * markets[s].ticker.last_price
                    for s, q in baseline["quantities"].items()
                ),
                D(0),
            )
            portfolios["hold"] = {"equity": str(hold_equity), "fees": str(baseline["fees"])}
            portfolios["cash"] = {"equity": str(capital), "fees": "0"}
            result = {
                "timestamp": json_default(now),
                "portfolios": portfolios,
                "outcomes": outcomes,
                "markets": {
                    s: {
                        "price": str(m.ticker.last_price),
                        "timestamp": json_default(m.ticker.timestamp),
                    }
                    for s, m in markets.items()
                },
            }
            self.db.execute(
                "INSERT INTO experiment_samples VALUES (?,?,?)",
                (bucket, json_default(now), encode(result)),
            )
            return result

    def run(
        self,
        exchange: BinanceSpotAdapter,
        *,
        cycles: int,
        emit: Callable[[str], None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(cycles) is not int or cycles < 0:
            raise ValueError("Nonnegative cycles required")
        with self.worker():
            count = 0
            while cycles == 0 or count < cycles:
                try:
                    markets = {
                        s: collect_market(exchange, s, 100, now=clock)
                        for s in self.config.risk.symbols
                    }
                    averages = {s: exchange.get_average_price(s) for s in self.config.risk.symbols}
                    result = self.sample(markets, averages, clock())
                    messages = []
                    for outcome in result["outcomes"]:
                        status = {"HOLD": "等待", "FILLED": "模擬成交", "REJECTED": "風控拒絕"}[
                            outcome["status"]
                        ]
                        reason = (
                            "、".join(REASONS.get(r, r) for r in outcome["reasons"])
                            if outcome["status"] == "REJECTED"
                            else ""
                        )
                        messages.append(
                            f"{LABELS[outcome['arm']]} {outcome['symbol']}：{status} {reason}"
                        )
                    emit(
                        result["timestamp"]
                        + " | "
                        + ("；".join(messages) or "行情已更新，等待下一根收盤 K 線")
                    )
                except ExchangeError as error:
                    with transaction(self.db):
                        self.db.execute(
                            "INSERT INTO experiment_errors(timestamp,code) VALUES (?,?)",
                            (json_default(clock()), error.code),
                        )
                    emit("行情取得失敗，本輪不交易，五分鐘後重試：" + error.code)
                count += 1
                if cycles == 0 or count < cycles:
                    sleep(300)

    def report(self, now: datetime) -> str:
        # Snapshot all published samples; counts/fees are captured at that same observation.
        rows = self.db.execute(
            "SELECT timestamp,payload FROM experiment_samples ORDER BY bucket"
        ).fetchall()
        if not rows:
            return "新版比較實驗尚無有效行情資料；請查看 docker compose logs --tail 20 trader。"
        payloads = [json.loads(row[1]) for row in rows]
        latest = payloads[-1]
        capital = self.config.risk.starting_capital_usd
        stamp = datetime.fromisoformat(rows[-1][0])
        inception = json.loads(
            self.db.execute("SELECT value FROM experiment_meta WHERE key='baseline'").fetchone()[0]
        )["timestamp"]
        age = max(0, int((now - stamp).total_seconds()))
        lines = [
            "Crypto 模擬比較報告 v2（非真實交易／未啟用 AI）",
            "每組獨立模擬本金：$" + str(capital) + "；配置幣種共用各組風控上限",
            "開始："
            + datetime.fromisoformat(inception)
            .astimezone(ZoneInfo("Asia/Taipei"))
            .strftime("%m/%d %H:%M"),
            "最後更新："
            + stamp.astimezone(ZoneInfo("Asia/Taipei")).strftime("%m/%d %H:%M")
            + f" 台灣時間（{age // 60} 分鐘前）",
            f"有效觀測：{len(rows)} 輪；全域交易暫停：{'是' if self.switch.active else '否'}",
        ]
        if age > 900:
            lines.append("注意：超過 15 分鐘未更新，請檢查容器、網路或電腦睡眠。")
        errors = self.db.execute("SELECT COUNT(*) FROM experiment_errors").fetchone()[0]
        lines.append(f"行情錯誤累計：{errors} 次（失敗輪次跳過交易）")
        lines.append("策略 | 淨值 | 損益 | 報酬 | 最大回落 | 手續費")
        for name in (*ARMS, "hold", "cash"):
            values = [D(p["portfolios"][name]["equity"]) for p in payloads]
            peak, drawdown = capital, D(0)
            for value in values:
                peak = max(peak, value)
                drawdown = max(drawdown, (peak - value) / peak * 100)
            equity = values[-1]
            fees = D(latest["portfolios"][name]["fees"])
            lines.append(
                f"{LABELS[name]} | ${equity:.2f} | ${equity - capital:+.2f} | "
                f"{(equity / capital - 1) * 100:+.2f}% | {drawdown:.2f}% | ${fees:.4f}"
            )
        for name in ARMS:
            stats = latest["portfolios"][name].get("stats", {})
            lines.append(
                f"{LABELS[name]}：成交 {stats.get('fills', 0)} 次；"
                f"等待 {stats.get('holds', 0)} 次；拒絕 {stats.get('rejected', 0)} 次"
            )
            for reason, count in stats.get("reasons", {}).items():
                lines.append(f"  {REASONS.get(reason, reason)}：{count} 次")
            rejected = stats.get("last_rejected")
            if rejected:
                lines.append(
                    f"  最近拒絕：{rejected['symbol']}；訊號代理值 "
                    f"{rejected['signal_proxy_bps']} bps；預估來回成本 "
                    f"{rejected['estimated_round_trip_cost_bps']} bps（1 bps = 0.01%）"
                )
            delta = D(latest["portfolios"][name]["equity"]) - D(
                latest["portfolios"]["hold"]["equity"]
            )
            lines.append(f"  相對持有基準：${delta:+.2f}")
        lines.extend(
            [
                "持有基準為理論組合：70% 現金、兩種配置幣各投入 15%，含入場費用與滑價、不再平衡。",
                "損益含已付費用；未扣尚未賣出的退出費用。最大回落依五分鐘觀測計算。",
                "1 小時組同時改變週期與進出場規則，並非只比較週期。MA 差距不是收益預測。",
                "資料不足以判斷穩定獲利；舊版帳本保留且未混入比較。",
            ]
        )
        return "\n".join(lines)


def statistics(connection: sqlite3.Connection) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    last_rejected = None
    for row in connection.execute("SELECT result_json FROM paper_cycles ORDER BY timestamp, rowid"):
        result = json.loads(row[0])
        counts[result["status"]] += 1
        if result["status"] == "REJECTED":
            reasons.update(result["reasons"])
            last_rejected = {
                key: result.get(key)
                for key in ("symbol", "signal_proxy_bps", "estimated_round_trip_cost_bps")
            }
    return {
        "fills": counts["FILLED"],
        "holds": counts["HOLD"],
        "rejected": counts["REJECTED"],
        "reasons": dict(reasons),
        "last_rejected": last_rejected,
    }

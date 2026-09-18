# AI Crypto Spot Trading MVP — Codex Development Spec

## 1. Goal
Build a safety-first autonomous crypto **spot trading experiment**. The system observes the market, asks an AI strategy layer for BUY / SELL / HOLD proposals, validates every proposal through deterministic risk controls, executes approved orders, and records a complete audit trail.

This is an experimental trading system, **not a guaranteed-profit system**.

### Phase 1
- Exchange: Binance Spot Testnet
- Assets: BTC and ETH only
- Quote currency: USDT initially
- Trading: Spot only
- Leverage / margin / futures: prohibited
- Withdrawal capability: prohibited
- Starting mode: TESTNET
- Future live capital target: ~US$1,000
- First live deployment: optionally start with only ~US$200 enabled

## 2. Core Principle
AI may propose trades. AI must **never** bypass risk controls.

Flow:

`Market Data -> Strategy/AI -> Trade Proposal -> Deterministic Risk Engine -> Execution Engine -> Exchange -> Portfolio/Logs -> Evaluation`

The Risk Engine has final veto authority.

## 3. Non-goals for MVP
Do NOT implement initially:
- Futures
- Margin
- Leverage
- Short selling
- Options
- Lending/staking
- Withdrawal automation
- Cross-exchange arbitrage
- High-frequency trading
- Martingale / unlimited averaging down
- Autonomous modification of risk limits

## 4. Operating Modes
Support exactly three modes:

### PAPER
Use real market data but simulate fills locally.

### TESTNET
Send orders to Binance Spot Testnet.

### LIVE
Real Binance Spot API. LIVE must be disabled by default and require an explicit environment flag.

Example:

```env
TRADING_MODE=TESTNET
ENABLE_LIVE_TRADING=false
```

If `TRADING_MODE=LIVE` while `ENABLE_LIVE_TRADING != true`, terminate safely.

## 5. Initial Risk Configuration
All values must be configurable, but use these MVP defaults:

```yaml
starting_capital_usd: 1000
symbols:
  - BTCUSDT
  - ETHUSDT
max_order_notional_usd: 100
max_total_position_usd: 300
max_daily_loss_usd: 20
max_trades_per_day: 6
min_minutes_between_trades: 30
max_symbol_allocation_pct: 20
minimum_confidence: 0.65
leverage_allowed: false
shorting_allowed: false
withdrawals_allowed: false
```

Risk calculations must use current portfolio equity rather than trusting AI-provided numbers.

## 6. Hard Risk Rules
Before EVERY order, reject it if any condition is true:

1. Order notional > configured maximum.
2. Resulting total crypto exposure > maximum total position.
3. Resulting symbol exposure > symbol allocation limit.
4. Daily realized + unrealized loss reaches daily stop.
5. Daily trade-count limit reached.
6. Cooldown period has not elapsed.
7. Symbol is not explicitly whitelisted.
8. Proposal requests leverage, margin, futures or shorting.
9. Market/account data is stale.
10. Exchange connectivity/account state cannot be verified.
11. Estimated fees/slippage make the proposed trade invalid under strategy rules.
12. Global kill switch is active.

When daily loss reaches US$20:
- Reject new BUY orders for the remainder of the trading day.
- Log `DAILY_LOSS_LIMIT_TRIGGERED`.
- Do not allow AI to override it.

Do not automatically liquidate positions merely because the daily limit was reached unless a separately defined protective exit rule requires it.

## 7. Trading Frequency
The MVP is deliberately NOT HFT.

Default:
- Strategy evaluation: every 5 minutes.
- Minimum 30-minute cooldown after an executed trade.
- Maximum 6 executed trades/day.
- HOLD does not count as a trade.

The scheduler can observe continuously while execution remains rate-limited.

## 8. AI Strategy Contract
The AI layer returns structured JSON only.

Example:

```json
{
  "symbol": "BTCUSDT",
  "action": "HOLD",
  "confidence": 0.72,
  "requested_notional_usd": 0,
  "reason": "No sufficiently strong setup after fees and volatility.",
  "time_horizon_minutes": 240,
  "invalid_if": [
    "price moves more than 1% before execution"
  ]
}
```

Allowed actions:

```text
BUY
SELL
HOLD
```

The AI must be explicitly told:
- HOLD is a valid and often preferable decision.
- Do not invent account balances, prices or indicators.
- Do not decide final order quantity.
- Do not modify risk parameters.
- Do not request leverage.

Risk Engine calculates the final allowed quantity.

## 9. Strategy Inputs
Create a normalized `MarketSnapshot` containing at minimum:

```text
symbol
timestamp
last_price
bid
ask
spread
1m candles
5m candles
15m candles
1h candles
24h volume
portfolio position
available quote balance
average entry price
realized pnl today
unrealized pnl
trades today
```

Indicators may initially include:
- SMA / EMA
- RSI
- ATR / volatility
- Volume change
- Short-term momentum

Keep indicators deterministic and independently testable.

## 10. Execution Engine
Execution Engine responsibilities:
- Validate symbol filters and minimum order size.
- Convert approved USD notional to valid exchange quantity.
- Round using exchange precision/filter metadata.
- Generate unique client order IDs.
- Submit order.
- Reconcile exchange response.
- Persist fill(s), fees and timestamps.
- Handle partial fills.
- Never blindly retry an order when its execution state is unknown.

Start with MARKET orders for MVP simplicity, but isolate order-type logic so LIMIT orders can be added later.

## 11. Binance Integration
Implement an exchange abstraction:

```python
class ExchangeAdapter:
    get_account()
    get_balances()
    get_ticker(symbol)
    get_klines(symbol, interval, limit)
    get_exchange_info(symbol)
    place_order(...)
    get_order(...)
    cancel_order(...)
    get_open_orders(...)
```

Then implement:

```text
BinanceSpotAdapter
```

Do not scatter Binance-specific API calls throughout strategy code.

Use official/current Binance API documentation when implementing endpoints and authentication.

## 12. Secrets & Security
Never commit API credentials.

Use environment variables:

```env
BINANCE_API_KEY=
BINANCE_API_SECRET=
TRADING_MODE=TESTNET
ENABLE_LIVE_TRADING=false
AI_PROVIDER=
AI_API_KEY=
DATABASE_URL=sqlite:///data/trading.db
```

Requirements:
- `.env` in `.gitignore`.
- Provide `.env.example` without secrets.
- API key must have trading/read permissions only.
- Withdrawal permission must remain disabled.
- Prefer IP restrictions when supported.
- Never print secrets in logs.

## 13. Kill Switch
Implement both persistent and runtime kill switches.

Examples:

```bash
python -m trader kill
python -m trader resume
python -m trader status
```

When killed:
- no new orders;
- monitoring may continue;
- existing state remains readable;
- restart must not silently reset the kill state.

Also automatically enter SAFE mode after repeated critical exchange/API errors.

## 14. Persistence
SQLite is sufficient for MVP. Design repository layer so PostgreSQL can replace it later.

Suggested tables:

### market_snapshots
- id
- timestamp
- symbol
- snapshot_json

### ai_decisions
- id
- timestamp
- symbol
- action
- confidence
- requested_notional
- reason
- raw_response

### risk_decisions
- id
- ai_decision_id
- approved
- rejection_reason
- approved_notional
- rules_snapshot_json

### orders
- id
- exchange_order_id
- client_order_id
- symbol
- side
- type
- requested_qty
- executed_qty
- average_fill_price
- status
- fee
- timestamps

### portfolio_snapshots
- timestamp
- cash
- positions_json
- equity
- realized_pnl
- unrealized_pnl

### system_events
- timestamp
- severity
- event_type
- payload_json

## 15. Auditability
For every attempted trade it must be possible to answer:

1. What market data did the system see?
2. What did AI propose?
3. Why did AI propose it?
4. Which risk rules were evaluated?
5. Why was it approved/rejected?
6. What exactly was submitted to Binance?
7. What actually filled?
8. What fees/slippage occurred?
9. What happened to portfolio equity afterward?

Never overwrite historical decisions.

## 16. Fees and Slippage
Do not evaluate performance using raw price movement alone.

For each fill calculate:

```text
Gross PnL
- Trading fees
- Estimated/actual slippage
= Net PnL
```

Record fee asset and convert to USD/USDT value when possible.

## 17. Performance Metrics
Dashboard/report should include:

- Starting equity
- Current equity
- Net PnL
- Return %
- Realized PnL
- Unrealized PnL
- Total fees
- Number of trades
- Win rate
- Average win
- Average loss
- Profit factor
- Maximum drawdown
- Daily drawdown
- Turnover
- HOLD decision rate

### Benchmarks
Compare strategy against passive benchmarks over exactly the same period:
- BTC buy-and-hold
- ETH buy-and-hold
- A simple fixed BTC/ETH benchmark

The important question is not merely whether the bot made money, but whether its risk-adjusted/net result justified active trading after costs.

## 18. Error Handling
Classify errors:

### Recoverable
- temporary timeout
- HTTP 429
- websocket disconnect

Use bounded exponential backoff.

### Ambiguous order state
If request timed out after submission:
- DO NOT immediately submit another order.
- Query using client order ID.
- Reconcile first.

### Critical
- authentication failure
- account permissions unexpected
- malformed exchange metadata
- repeated reconciliation failure

Critical errors activate SAFE/KILL mode and generate a high-severity event.

## 19. Suggested Repository Structure

```text
ai-trader/
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── config/
│   └── default.yaml
├── src/trader/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── scheduler.py
│   ├── models.py
│   ├── exchange/
│   │   ├── base.py
│   │   └── binance.py
│   ├── market/
│   │   ├── data.py
│   │   └── indicators.py
│   ├── strategy/
│   │   ├── base.py
│   │   └── ai_strategy.py
│   ├── risk/
│   │   └── engine.py
│   ├── execution/
│   │   └── engine.py
│   ├── portfolio/
│   │   └── manager.py
│   ├── storage/
│   │   ├── db.py
│   │   └── repository.py
│   ├── reporting/
│   │   └── metrics.py
│   └── safety/
│       └── kill_switch.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── data/
```

Python 3.12+ preferred.

## 20. Testing Requirements
Risk Engine requires extensive unit tests.

At minimum test:
- $101 order rejected when max is $100.
- position exceeding $300 rejected.
- unapproved symbol rejected.
- seventh trade rejected after six trades.
- trade inside cooldown rejected.
- BUY rejected after daily loss limit.
- HOLD never creates an order.
- leverage request rejected.
- stale price data rejected.
- live trading cannot start without explicit enable flag.
- kill switch survives process restart.
- duplicate client order IDs do not create duplicate trades.

Integration tests should mock exchange responses before Testnet integration.

## 21. Development Phases

### Phase A — Foundation
- Initialize repo.
- Config loader.
- Domain models.
- SQLite persistence.
- Structured logging.
- CLI status/kill/resume.

### Phase B — Exchange
- Exchange abstraction.
- Binance Spot Testnet adapter.
- Market data retrieval.
- Account/balance retrieval.
- Order placement/reconciliation.

### Phase C — Risk
- Implement deterministic Risk Engine.
- Unit-test every hard limit.

### Phase D — Baseline Strategy
Before AI, implement a simple deterministic strategy or random/no-op test strategy so the entire trading pipeline can be tested independently of an LLM.

### Phase E — AI Strategy
- Structured-output schema.
- Market context builder.
- BUY/SELL/HOLD proposals.
- Store prompts/responses safely.
- Risk Engine remains authoritative.

### Phase F — Testnet Burn-in
Run continuously for at least 2 weeks.

Measure:
- crashes
- duplicate orders
- reconciliation failures
- API errors
- risk violations
- trading costs
- strategy performance

### Phase G — Small Live Pilot
Only after explicit human approval.

Suggested progression:
1. ~$200 enabled capital.
2. Keep same $100/order ceiling or lower it further.
3. Observe for several weeks.
4. Increase toward ~$1,000 only after operational stability is demonstrated.

Do not automatically scale capital based solely on AI recommendation.

## 22. Acceptance Criteria for MVP
MVP is complete when:

- Runs unattended on Binance Spot Testnet.
- Supports BTCUSDT and ETHUSDT.
- AI can return BUY/SELL/HOLD in validated structured format.
- Risk Engine can veto AI decisions.
- Hard limits are covered by automated tests.
- Orders and fills reconcile correctly.
- No duplicate order occurs after timeout/retry scenarios.
- Every decision/order is auditable.
- Kill switch works across restart.
- Performance includes fees and benchmark comparison.
- LIVE mode cannot accidentally activate.

## 23. Instructions to Codex

Work incrementally. Do not attempt the entire system in one unreviewed change.

Start with **Phase A only**.

For every phase:
1. Inspect the existing repository before modifying it.
2. Write a short implementation plan.
3. Implement the smallest coherent change.
4. Add/update tests.
5. Run tests and lint/type checks.
6. Report files changed and commands executed.
7. Stop if credentials, exchange permissions, or a safety-critical assumption is unclear.

Safety constraints are requirements, not suggestions.

Never:
- enable withdrawals;
- introduce leverage/futures/margin;
- hard-code credentials;
- allow the LLM to bypass the Risk Engine;
- enable LIVE trading by default;
- silently increase risk limits;
- retry an ambiguous order without reconciliation.

### First Codex Task

```text
Read AI_Crypto_Trading_MVP_Codex.md completely.

Implement Phase A — Foundation only.

Create the Python project structure, typed configuration models, domain models,
SQLite persistence foundation, structured logging, and persistent CLI kill switch
(status/kill/resume). Add tests for configuration validation and kill-switch
persistence.

Do NOT implement live exchange trading yet.
Do NOT request or store real Binance credentials yet.

After implementation, run the test suite and provide:
1. summary of architecture,
2. files changed,
3. test results,
4. unresolved questions,
5. recommended next task for Phase B.
```

## 24. Human Gate Before LIVE
Before changing from TESTNET to LIVE, require a manual checklist confirming:

- Testnet burn-in completed.
- No unresolved duplicate-order bug.
- Kill switch tested.
- Daily loss stop tested.
- API key withdrawal permission disabled.
- API key stored securely.
- Account permissions verified.
- Fee/slippage accounting verified.
- Operator explicitly changes LIVE configuration.

There must be no automatic promotion from TESTNET to LIVE.

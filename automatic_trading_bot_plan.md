# Automatic Options Trading Bot MVP Plan

Date: 2026-06-03
Status: Working plan, intended to guide the first implementation

## 1. Objective

Build an automatic trading bot that can first run in paper trading, then graduate to a small live account using a simple, bounded-risk options strategy. The first goal is not to build the perfect trading system. The first goal is to get a controlled v1 running quickly, with enough logging, alerts, and risk controls that we can learn from realistic execution before risking capital.

Target live account size: USD 10,000
Initial mode: Paper trading
Live mode: later, after paper-trading promotion criteria are met
Primary broker target: Alpaca
Fallback/alternative broker: none for v1
Notification target: Discord first, WhatsApp later if needed

Important note: this is an engineering and research plan, not financial advice. Options are risky, and automated trading can lose money quickly. The plan intentionally restricts the bot to defined-risk trades first.

## 2. Current Decisions

### Broker

Use Alpaca only for v1.

Reasons:

- Alpaca is API-first and simpler to integrate.
- Alpaca supports live options trading and multi-leg options orders.
- Alpaca supports `order_class: "mleg"` for multi-leg option orders, which is important for spreads.
- Alpaca has paper and live environments, which lets us build the real execution flow without placing live orders first.

Required Alpaca setup:

- Alpaca account.
- Paper trading API keys.
- Paper account configured to simulate USD 10,000 starting capital if possible.
- Market data access suitable for options scanning.
- Discord webhook for notifications.

Required before live promotion:

- Live Alpaca brokerage account.
- Live API keys.
- Options trading enabled.
- Options approval at Level 3, because the first strategy uses multi-leg spreads.
- Market data access suitable for live options trading, preferably OPRA.

Alpaca options levels relevant to us:

- Level 0: options disabled.
- Level 1: covered calls and cash-secured puts.
- Level 2: Level 1 plus buying calls and puts.
- Level 3: Level 1 and 2 plus option spreads.

If Level 3 approval is not granted, the planned put credit spread strategy cannot run as designed. We should not downgrade v1 to naked or cash-secured puts automatically, because that changes the risk profile and capital requirements.

Alpaca API endpoints and capabilities for v1:

- Trading API: use Alpaca's `/v2` Trading API for account, positions, orders, and order status.
- Order submission: use `POST /v2/orders`.
- Options contracts: use `/v2/options/contracts?underlying_symbols=...` to discover tradable option contracts.
- Option chain snapshots: use `https://data.alpaca.markets/v1beta1/options/snapshots/{underlying_symbol}` for latest trade, latest quote, and Greeks.
- Real-time option data: use Alpaca's option websocket later if polling snapshots becomes too slow or stale.
- News: use Alpaca news REST/websocket data first, then add other news vendors only if coverage is insufficient.
- Positions: Alpaca's existing Positions API works for options positions.
- Activities: use account activities to capture fills, exercise, assignment, expiry, and other option events.

Market data note:

- Alpaca Basic includes limited real-time data: IEX for equities and the indicative feed for options.
- Alpaca Algo Trader Plus includes broader equity coverage and OPRA options data.
- For live options trading, OPRA data is strongly preferred because option execution depends heavily on accurate bid/ask quotes.
- Paper trading can start before OPRA if necessary, but the bot should record which feed was used for every decision.
- If we start without OPRA, the bot must be more conservative: fewer trades, wider stale-data blocks, and stricter quote checks.

Order rules specific to Alpaca:

- Use `order_class: "mleg"` for the put credit spread.
- Use `type: "limit"` only.
- Use `time_in_force: "day"` for options.
- Do not use `notional`; options order quantity must be whole contracts.
- Do not set `extended_hours` for options.
- For multi-leg orders, the parent order has no `side`; each leg has its own `side`, `ratio_qty`, and `position_intent`.
- For an opening put credit spread, the short put leg uses `sell_to_open` and the protective long put leg uses `buy_to_open`.
- For an MLeg credit order, Alpaca's API reference says a negative `limit_price` represents a credit received. Example: a USD 0.80 target credit should be submitted as `limit_price: "-0.80"`.
- Every Alpaca order must have a deterministic `client_order_id` so retries do not accidentally duplicate trades.

Example Alpaca put credit spread payload shape:

```json
{
  "order_class": "mleg",
  "qty": "1",
  "type": "limit",
  "limit_price": "-0.80",
  "time_in_force": "day",
  "legs": [
    {
      "symbol": "AAPL260619P00190000",
      "ratio_qty": "1",
      "side": "sell",
      "position_intent": "sell_to_open"
    },
    {
      "symbol": "AAPL260619P00185000",
      "ratio_qty": "1",
      "side": "buy",
      "position_intent": "buy_to_open"
    }
  ],
  "client_order_id": "bot-v1-aapl-put-credit-20260602-001"
}
```

Implementation notes:

- Do not build support for other brokers in v1.
- Keep a broker adapter interface anyway, but implement only `alpaca.py`.
- Store the raw Alpaca request and response for every order.
- Reconcile broker state on every loop before asking the LLM for a new decision.
- Treat a broker/API mismatch as a stop-trading event.

### Strategy

Start with put credit spreads on liquid US tech stocks and ETFs.

This is related to "selling a put", but it is safer than naked short puts because the bot also buys a lower-strike put as protection.

Example:

- Sell 1 AAPL 190 put.
- Buy 1 AAPL 185 put.
- Receive USD 1.00 credit.
- Max profit: USD 100.
- Max loss: `(5.00 - 1.00) * 100 = USD 400`.

This is a defined-risk bullish-to-neutral strategy. It benefits if the stock stays above the short put strike and from time decay.

Why not naked options in v1:

- Naked short puts can require large buying power and can create assignment risk.
- Naked short calls can have theoretically unlimited loss.
- Spreads give us a clear max loss before the trade is placed.

### Notifications

Use Discord first. Discord notifications are a required v1 dependency, not an optional extra.

Reasons:

- Discord webhooks are simple.
- No phone-number or business account setup.
- Easy to post structured trade alerts, P&L updates, and error alerts.
- Fast enough for paper-trading supervision and later live-trading safety alerts.

Required Discord setup:

- Create a dedicated Discord channel for the bot.
- Create a Discord webhook for that channel.
- Store the webhook URL in `DISCORD_WEBHOOK_URL`.
- Send a startup heartbeat whenever the bot starts.
- Send a message for every candidate, LLM decision, order submit, order cancel, fill, close, rejected decision, risk alert, error, and daily summary.
- If Discord notifications fail repeatedly, the bot must stop opening new trades.

WhatsApp can be added later through Twilio or WhatsApp Business API.

### AI Role

The LLM should make the trading decision, because it is the component best suited to interpreting organic inputs such as news, headlines, summaries, market commentary, and unusual context that is hard to reduce to fixed indicators.

The bot should therefore be an LLM-directed trading system with hard coded execution constraints.

Practical meaning:

- Code gathers the data.
- Code creates a clean decision packet.
- Code generates valid spread candidates.
- The LLM decides whether to open, skip, hold, close, or disable trading.
- Code validates the LLM decision against non-negotiable rules.
- Code places the order only if validation passes.
- Every LLM decision is logged with the exact prompt version, input packet, JSON response, and validator result.

This gives the LLM real trading authority, but only inside an executable envelope.

LLM can decide:

- Whether current news makes a symbol too risky.
- Whether market tone is supportive enough to sell a put spread.
- Which candidate spread to select from the generated candidate list.
- Whether to close an existing spread early.
- Whether to pause trading for a symbol or the whole bot.
- Whether a technically valid trade should still be skipped because the context feels bad.

LLM cannot do these in v1:

- Invent a new options strategy.
- Trade naked options.
- Trade symbols outside the approved watchlist.
- Trade contracts outside the candidate list generated by code.
- Place market orders.
- Override max loss, max open risk, max daily loss, or the kill switch.
- Access broker API keys.
- Call the broker API directly.
- Increase quantity beyond the config limit.
- Hold a position through expiration if the code's expiry rule says to close.

Non-negotiable validator rules:

- Invalid JSON means no trade.
- Missing required fields means no trade.
- Any unsupported action means no trade.
- Any contract not in the code-generated candidate list means no trade.
- Any order exceeding risk limits means no trade.
- Any stale data means no trade.
- Any quote/liquidity rule failure means no trade.
- Any active kill switch means no trade.
- Any unclear credit/debit sign means no trade.

The LLM decision schema should be strict.

```json
{
  "action": "open | close | hold | skip | disable_trading",
  "symbol": "AAPL",
  "candidate_id": "AAPL-20260619-190P-185P",
  "quantity": 1,
  "limit_price": "-0.80",
  "confidence": 0.72,
  "time_horizon": "7-21 DTE",
  "decision_reason": "Short explanation of the trade thesis or skip reason.",
  "news_assessment": {
    "risk_level": "low | medium | high",
    "sentiment": "positive | neutral | negative | mixed",
    "summary": "Short explanation."
  },
  "risk_checklist": {
    "defined_risk": true,
    "within_max_loss": true,
    "liquidity_ok": true,
    "earnings_ok": true,
    "no_material_negative_news": true,
    "market_trend_ok": true
  },
  "exit_plan": {
    "profit_take_credit_pct": 50,
    "loss_trigger": "2x initial credit or short put delta above 0.45",
    "close_before_expiry_days": 3
  }
}
```

The prompt should instruct the LLM to be conservative when information is ambiguous. Even in paper mode, "no trade" is always a valid decision.

## 3. V1 Strategy Specification

Strategy name: Put credit spread  
Trade type: Multi-leg options spread  
Direction: Bullish or neutral  
Risk type: Defined risk  
Initial trade frequency: Low, likely 0-3 trades per day maximum  
Initial size: 1 spread per trade

### Universe

Initial watchlist:

- QQQ
- AAPL
- MSFT
- NVDA
- AMZN
- META
- GOOGL
- TSLA

Preferred first symbols:

- QQQ
- AAPL
- MSFT

Reason: they tend to have better liquidity and more reliable option chains than smaller names. TSLA and NVDA can move violently, so they should be included only after stricter risk checks are working.

### Entry Window

Only trade during regular US market hours.

Initial allowed entry window:

- US market time: 10:00 to 15:00 Eastern Time.
- Singapore time during US daylight saving: 22:00 to 03:00.
- Avoid first 30 minutes after market open.
- Avoid last 60 minutes before market close for new entries.

### Option Selection

For each eligible underlying:

- Expiration: 7-21 calendar days to expiration.
- Short put delta target: approximately -0.20 to -0.30.
- Long put strike: lower than short put, usually USD 5 or USD 10 wide depending on the stock price and liquidity.
- Same expiration for both legs.
- Same underlying.
- Quantity: 1 spread initially.

Example spread:

- Underlying: QQQ
- Expiration: 14 DTE
- Sell put: about -0.25 delta
- Buy put: 5 points lower

### Liquidity Filters

Reject trade candidates if:

- Bid/ask spread is too wide.
- Option open interest is too low.
- Option volume is too low.
- Short leg bid is unreliable or near zero.
- Combined spread mid-price cannot be estimated clearly.
- Broker quote data is stale.

Initial numerical filters:

- Short leg open interest: at least 500.
- Long leg open interest: at least 100.
- Short leg bid: at least USD 0.30.
- Each leg bid/ask spread: less than 15% of mid price, or less than USD 0.20, whichever is more permissive.
- Net credit: at least 20% of spread width if fills are realistic.

These thresholds are starting points and should be adjusted after observing real quotes.

### Market Filters

Reject new entries if:

- Underlying is down more than 2.5% intraday.
- Underlying has broken a major short-term support rule used by the bot.
- Market is in a sharp selloff.
- VIX or broad-market volatility is spiking unusually.
- Major scheduled event is close: FOMC, CPI, major jobs report, or earnings for the underlying.
- News classifier says there is material negative company-specific news.

Initial technical filter:

- Underlying price must be above its 20-period moving average on 30-minute bars, or the bot must skip that symbol.

This keeps the first version from selling puts into obvious downside momentum.

### Earnings Filter

Reject trades if the underlying has earnings within the next 7 calendar days.

Also reject trades if earnings occurred within the last 1 trading day and price action is unstable.

### News Filter

Every minute, retrieve recent market news for watchlist symbols.

The LLM/news classifier returns structured output:

```json
{
  "symbol": "AAPL",
  "risk_level": "low | medium | high",
  "sentiment": "positive | neutral | negative | mixed",
  "reason": "short explanation",
  "should_block_new_trades": true
}
```

Rules:

- If news risk is high, block new trades for that symbol.
- If sentiment is negative and material, block new trades.
- If confidence is low, do not use the news as a positive signal. At most, use it as a reason to skip.
- News should never force a trade open.

## 4. Risk Management

The risk engine is the most important part of v1.

### Account-Level Limits

Starting capital: USD 10,000

Initial limits:

- Max risk per trade: USD 400-500.
- Max open risk across all positions: USD 1,500.
- Max number of open spreads: 3.
- Max number of new trades per day: 3.
- Max daily realized plus unrealized loss: USD 500.
- Max weekly realized plus unrealized loss: USD 1,000.
- Emergency stop if account equity drops below USD 8,000.

If any limit is breached:

- Do not open new trades.
- Notify immediately.
- Close existing positions only if exit rules or emergency rules say to close.
- Require manual reset before trading resumes.

### Position Sizing

For a put credit spread:

```text
max_loss = (spread_width - net_credit) * 100 * quantity
```

The bot must calculate max loss before placing an order.

Rules:

- Quantity starts at 1.
- Never size by "available buying power" alone.
- Never use all buying power.
- Never open a trade if max loss exceeds per-trade limit.
- Never open a trade if it would push total open risk above account-level limit.

### Exit Rules

Close the spread when any of these occur:

- Profit target: can close for 50% of initial credit.
- Loss stop: spread value reaches about 2x initial credit, subject to max loss constraints.
- Short put delta rises above about 0.45.
- Underlying price touches or falls below short strike.
- News risk becomes high.
- Earnings/event risk appears.
- Expiration is 3 calendar days away.
- Broker/account data becomes inconsistent.

Never intentionally hold spreads through expiration in v1.

### Order Rules

Use limit orders only.

Entry:

- Submit multi-leg order as one spread.
- Start near mid-price.
- Do not chase beyond minimum acceptable credit.
- Cancel if not filled within a short window.

Exit:

- Submit closing multi-leg order.
- Start near mid-price.
- If risk exit is urgent, gradually improve price within predefined bounds.
- Notify on every submit, cancel, replace, fill, partial fill, and error.

No market orders for options in v1.

### Kill Switches

The bot stops opening trades if:

- Account data cannot be fetched.
- Position data cannot be reconciled.
- Market data is stale.
- Option quote data is stale.
- Broker API returns repeated errors.
- Discord notification fails repeatedly.
- System clock/timezone check fails.
- Daily or weekly loss limit is hit.
- Manual kill switch file or command is active.

Manual kill switch idea:

- If file `KILL_SWITCH` exists in the project root, the bot must not open trades.
- It may still monitor and notify.

## 5. System Architecture

Use Python for v1.

### Components

```text
trading-bot/
  config/
    settings.yaml
    secrets.example.env
  src/
    main.py
    brokers/
      base.py
      alpaca.py
    data/
      market_data.py
      option_chain.py
      news.py
    strategy/
      put_credit_spread.py
      filters.py
    risk/
      limits.py
      position_sizing.py
      kill_switch.py
    execution/
      orders.py
      reconciliation.py
    notifications/
      discord.py
    storage/
      db.py
      models.py
    llm/
      decision_engine.py
      prompt_versions/
        put_credit_spread_v1.md
      schemas.py
    utils/
      time.py
      logging.py
  tests/
  logs/
  data/
```

### Main Loop

Run every minute during market hours.

```text
1. Load config and risk state.
2. Check kill switches.
3. Fetch account, positions, orders.
4. Reconcile local state with broker state.
5. Fetch market data and option chains.
6. Fetch recent news.
7. Build candidate spreads using deterministic filters.
8. Build an LLM decision packet with account, position, candidate, market, and news context.
9. Ask the LLM to decide: open, close, hold, skip, or disable trading.
10. Validate the LLM decision through the hard rule/risk engine.
11. Submit Alpaca limit order if the validated decision requires execution.
12. Log the decision packet, LLM response, validator result, and order result.
13. Send notifications.
```

Existing position management must always run before new entries.

Important sequencing:

- Existing positions are included in the LLM decision packet before any new entries are considered.
- The LLM can recommend closing or holding positions.
- New entries are allowed only after the validator confirms no existing-position action takes priority.
- If LLM and validator disagree, the validator result wins operationally, and the disagreement is logged.

### Storage

Start with SQLite.

Tables:

- `trades`
- `orders`
- `positions`
- `signals`
- `llm_decisions`
- `risk_events`
- `news_events`
- `account_snapshots`
- `bot_runs`

Every decision should be logged, including skipped trades.

Example skipped-trade log:

```json
{
  "time": "2026-06-02T22:31:00+08:00",
  "symbol": "AAPL",
  "decision": "skip",
  "reason": "earnings within 7 days",
  "candidate": null
}
```

## 6. Infrastructure

### Local First

Build and run locally first. The initial bot should run on the user's computer in paper mode.

Reasons:

- Faster iteration.
- Easier debugging.
- No cloud secrets or deployment complexity during initial build.
- Easy to inspect logs, SQLite data, Alpaca responses, LLM decisions, and Discord messages.
- Easy to stop the process manually while behavior is still being shaped.

Local paper mode should still behave like production:

- `mode: paper`.
- Alpaca paper endpoint.
- Discord notifications required.
- SQLite trade journal.
- Structured logs.
- Kill switch file.
- One-paper-trade limit at first.
- Same LLM decision schema and same validator intended for live mode.

The local stage is complete when:

- The bot can run a full read-only loop.
- The bot can generate candidates.
- The LLM can make schema-valid decisions.
- The validator can reject bad decisions.
- Discord receives startup, decision, risk, and summary messages.
- At least one full paper trade lifecycle is opened, monitored, notified, logged, and closed.

### Cloud Deployment

After local paper trading is stable, deploy the same bot to a small cloud server in paper mode. Do not move directly from local paper trading to cloud live trading.

Candidate setup:

- Provider: AWS Lightsail, DigitalOcean, Fly.io, or a small VPS.
- Runtime: Docker container.
- Process manager: systemd or Docker restart policy.
- Secrets: environment variables or cloud secret manager.
- Logs: local file plus optional log shipping.
- Persistent storage for SQLite and log files.
- Manual kill switch file or remote pause command.

Minimum server:

- 1 vCPU.
- 1 GB RAM.
- Ubuntu LTS.

Recommended deployment staircase:

1. Local paper read-only.
2. Local paper with one-trade execution.
3. Local paper with normal paper limits.
4. Cloud paper read-only.
5. Cloud paper with one-trade execution.
6. Cloud paper with normal paper limits.
7. Cloud live read-only.
8. Cloud live with one-trade execution.

Cloud paper mode should run before cloud live mode until:

- Cloud uptime is stable.
- Discord heartbeat and daily summary are reliable.
- Restart behavior is safe.
- No duplicate orders occur after restarts.
- SQLite/log persistence works across restarts.
- Kill switch behavior is verified on the server.

### Monitoring

Required:

- Discord heartbeat when bot starts.
- Discord daily summary after market close.
- Alert if bot crashes.
- Alert if no heartbeat for more than 5 minutes during market hours.
- Alert on every trade/order/fill/error.

Future:

- Simple dashboard.
- Grafana/Prometheus.
- Web UI to pause/resume trading.

## 7. Notification Design

Discord messages should be concise but complete.

### Startup Message

```text
Bot started
Mode: PAPER
Broker: Alpaca
Account equity: 10000.00
Open risk: 0.00
Trading enabled: yes
```

### Candidate Message

```text
Candidate found: AAPL put credit spread
Expiry: 2026-06-19
Sell: 190P
Buy: 185P
Credit target: 1.00
Max loss: 400
Reason: delta/liquidity/trend filters passed
```

### Order Message

```text
Order submitted
Symbol: AAPL
Spread: sell 190P / buy 185P
Qty: 1
Limit credit: 1.00
Order ID: abc123
```

### Fill Message

```text
Order filled
Symbol: AAPL
Entry credit: 1.02
Max profit: 102
Max loss: 398
Exit target: buy back at 0.51
```

### Risk Alert

```text
Risk alert
Reason: daily loss limit hit
New entries disabled
Manual reset required
```

## 8. Paper Trading, Backtesting, and Dry Runs

The first operating mode is paper trading. The paper bot should use the same strategy, LLM decision flow, validator, order construction, logging, alerts, and risk limits intended for live mode. The goal is to make the live switch mostly a broker credential/base-URL change, not a redesign.

Required before any paper order placement:

- Broker connection works.
- Account read works.
- Option chain fetch works.
- Quote data freshness check works.
- OPRA or indicative feed status is known.
- Alpaca MLeg order payload can be generated with correct credit/debit sign.
- LLM decision packet can be generated.
- LLM returns valid JSON matching the schema.
- Validator rejects at least one intentionally bad LLM decision in tests.
- Order preview or dry-run object generation works.
- Discord webhook works.
- Max loss calculation works.
- Kill switch works.
- The bot can run one full loop without submitting an order.

This can be done quickly, potentially in one evening.

Paper rollout:

1. Run in paper-read-only mode for one market session.
2. Allow one paper trade with 1 spread and simulated max loss under USD 500.
3. Disable additional entries after first paper fill.
4. Observe management, fills, exits, logs, and notifications.
5. Review the full paper trade lifecycle.
6. Re-enable paper trading with up to 3 open spreads only after the first lifecycle is understood.

Promotion criteria before live mode:

- At least 20 paper trades or 2 full trading weeks, whichever comes first.
- No unreconciled positions or orders.
- No duplicate order submissions.
- No invalid LLM decisions accepted by the validator.
- No missed exit caused by bot logic.
- Discord notifications were received for every order, fill, close, risk alert, and daily summary.
- Paper max drawdown stayed within the configured daily and weekly limits.
- Live account has Level 3 options approval.
- Live API keys are configured and tested in read-only mode.
- OPRA/live options market data decision is made.

Live rollout after promotion:

1. Run in live-read-only mode for one market session.
2. Allow one live trade with 1 spread and max loss under USD 500.
3. Disable additional live entries after first live fill.
4. Monitor until closed.
5. Review logs before allowing multiple live positions.

## 9. Implementation Phases

### Phase 0: Account and API Setup

Tasks:

- Create or confirm Alpaca account.
- Generate paper trading API keys.
- Configure paper account buying power to approximate USD 10,000 if Alpaca allows it.
- Confirm options market data access.
- Create Discord webhook.
- For later live promotion: enable live trading API.
- For later live promotion: apply for options Level 3.
- For later live promotion: generate live API keys.
- For later live promotion: decide whether to subscribe to Algo Trader Plus / OPRA before live options trading.

Exit criteria:

- In paper mode, we can fetch account info, option contract data, option chain snapshots, quotes, positions, and orders.
- Live Level 3 approval can remain pending while paper development proceeds.

### Phase 1: Project Skeleton

Tasks:

- Create Python project.
- Add config loading.
- Add structured logging.
- Add SQLite storage.
- Add Discord notifications.
- Add broker adapter interface.
- Implement Alpaca adapter only.
- Add LLM decision schema and prompt-version storage.

Exit criteria:

- Bot starts, sends heartbeat, fetches account, writes logs.

### Phase 2: Data and Scanning

Tasks:

- Fetch watchlist prices.
- Fetch option expirations.
- Fetch option chains with Greeks where available.
- Filter contracts by DTE, delta, liquidity, and spread width.
- Build candidate put credit spreads.
- Build LLM decision packets from account, position, market, option, candidate, and news data.
- Log accepted and rejected candidates.

Exit criteria:

- Bot can identify possible spreads without trading.
- LLM can return a valid open/skip/hold/close decision without trading.

### Phase 3: Risk Engine

Tasks:

- Calculate max profit/loss.
- Track open risk.
- Enforce trade count and daily loss limits.
- Implement kill switch.
- Implement expiry and earnings blocks.
- Implement stale data blocks.
- Implement LLM decision validator.

Exit criteria:

- No LLM decision can reach execution unless all risk checks pass.

### Phase 4: Execution

Tasks:

- Submit multi-leg limit order.
- Poll or stream order status.
- Handle fills, partial fills, cancels, rejects.
- Store order and trade records.
- Create closing order logic.

Exit criteria:

- Bot can open and close one spread with correct logs and notifications.

### Phase 5: Paper Trading Rollout

Tasks:

- Enable paper execution mode for one trade only.
- Simulated max loss under USD 500.
- Disable further entries after first paper fill.
- Monitor until closed.
- Review trade journal.

Exit criteria:

- One complete paper trade lifecycle is recorded and understood.

### Phase 6: Cloud Paper Deployment

Tasks:

- Containerize the bot.
- Deploy to a small Ubuntu server.
- Configure paper Alpaca keys, OpenAI key, and Discord webhook as environment variables.
- Mount persistent storage for SQLite and logs.
- Configure process restart policy.
- Verify startup heartbeat.
- Verify daily summary.
- Verify kill switch behavior on the server.
- Run cloud paper read-only before enabling cloud paper execution.

Exit criteria:

- Cloud paper mode runs reliably without missed heartbeats, duplicate orders, or lost logs.

### Phase 7: Live Tiny Rollout

Tasks:

- Confirm live Level 3 options approval.
- Confirm live API keys and live-read-only access.
- Confirm market data plan.
- Run cloud live read-only for one market session.
- Enable cloud live mode for one trade only.
- Max loss under USD 500.
- Disable further live entries after first live fill.
- Monitor until closed.
- Review trade journal.

Exit criteria:

- One complete live trade lifecycle is recorded and understood.

## 10. Configuration Defaults

Initial config:

```yaml
mode: paper
broker: alpaca

alpaca:
  trading_base_url: https://api.alpaca.markets
  paper_base_url: https://paper-api.alpaca.markets
  data_base_url: https://data.alpaca.markets
  active_trading_base_url: https://paper-api.alpaca.markets
  require_options_level: 3
  option_data_feed: indicative
  live_option_data_feed: opra
  stock_data_feed: iex
  allow_indicative_feed_for_live: false
  use_mleg_orders: true
  credit_limit_price_must_be_negative: true
  require_client_order_id: true
  request_timeout_seconds: 30
  request_retries: 2
  request_retry_backoff_seconds: 1

account:
  paper_starting_capital: 10000
  live_starting_capital: 10000
  emergency_equity_floor: 8000

risk:
  max_loss_per_trade: 500
  max_open_risk: 1500
  max_open_positions: 3
  max_new_trades_per_day: 3
  max_daily_loss: 500
  max_weekly_loss: 1000
  disable_after_first_paper_trade: true
  disable_after_first_live_trade: true

strategy:
  name: put_credit_spread
  watchlist:
    - QQQ
    - AAPL
    - MSFT
    - NVDA
    - AMZN
    - META
    - GOOGL
    - TSLA
  preferred_symbols:
    - QQQ
    - AAPL
    - MSFT
  min_dte: 7
  max_dte: 21
  short_put_delta_min: -0.30
  short_put_delta_max: -0.20
  spread_widths:
    - 5
    - 10
  min_credit_as_width_pct: 0.20
  close_profit_pct: 0.50
  close_before_expiry_days: 3

liquidity:
  min_short_leg_open_interest: 500
  min_long_leg_open_interest: 100
  min_short_leg_bid: 0.30
  max_leg_spread_pct_of_mid: 0.15
  max_leg_spread_absolute: 0.20

market_filters:
  block_if_underlying_down_intraday_pct: 2.5
  require_above_30m_ma_period: 20
  block_earnings_within_days: 7

decision_engine:
  type: llm
  prompt_version: put_credit_spread_v1
  max_concurrent_symbols: 8
  temperature: 0
  allowed_actions:
    - open
    - close
    - hold
    - skip
    - disable_trading
  require_json_schema: true
  reject_invalid_json: true
  reject_candidates_not_generated_by_code: true
  log_full_decision_packet: true
  allow_no_trade_decision: true

execution:
  order_type: limit
  pre_submit_revalidate_quotes: true
  entry_limit_credit_buffer: 0.05
  entry_price_adjustment_step: 0.05
  entry_order_poll_seconds: 5
  entry_order_timeout_seconds: 60
  max_entry_price_adjustments: 3
  manage_entry_orders: true
  no_market_orders: true

notifications:
  provider: discord
  webhook_env_var: DISCORD_WEBHOOK_URL
  required_for_execution: true
  stop_new_trades_on_repeated_failure: true
  heartbeat_on_startup: true
  daily_summary: true
```

## 11. Information Needed From User

Broker/account:

- Do you already have an Alpaca account, or do we need to open one?
- Are paper trading API keys available?
- Can the paper account be set to approximately USD 10,000 buying power?
- Is options trading available in the paper account?
- For later live promotion: is live trading enabled?
- For later live promotion: is options trading approved at Level 3?
- For later live promotion: are live API keys available?
- For later live promotion: are you willing to subscribe to Alpaca Algo Trader Plus / OPRA data if Basic only gives indicative options quotes?

Trading preferences:

- Are you comfortable simulating max loss of about USD 400-500 on the first paper trade?
- Should the bot be allowed to trade QQQ first, or only individual tech stocks?
- Should TSLA and NVDA be disabled for the first week because of volatility?

Notifications:

- Discord server/channel for webhook.
- Whether alerts should mention exact P&L and account equity.

Ops:

- v1 should run locally on the user's computer first.
- Later, choose a cloud provider for cloud paper trading.
- Preferred cloud provider, if any.
- Whether the cloud deployment should use Docker plus systemd, or Docker Compose.

## 12. Source References

- Alpaca Options Trading: https://docs.alpaca.markets/us/docs/options-trading
- Alpaca Options Level 3 / Multi-leg Trading: https://docs.alpaca.markets/us/docs/options-level-3-trading
- Alpaca Create Order API Reference: https://docs.alpaca.markets/us/reference/postorder
- Alpaca Option Chain API Reference: https://docs.alpaca.markets/us/reference/optionchain
- Alpaca Market Data API: https://docs.alpaca.markets/us/docs/about-market-data-api
- Alpaca Historical Stock Bars API: https://docs.alpaca.markets/reference/stockbars
- Alpaca Historical News Data: https://docs.alpaca.markets/us/docs/historical-news-data
- Alpaca News Articles API: https://docs.alpaca.markets/reference/news-3
- Alpaca Real-time News Stream: https://docs.alpaca.markets/docs/streaming-real-time-news
- Alpaca Real-time Option Data: https://docs.alpaca.markets/us/docs/real-time-option-data
- OpenAI Responses API: https://developers.openai.com/api/reference/responses
- OpenAI Structured Outputs: https://developers.openai.com/api/docs/guides/structured-outputs
- FINRA Options Overview: https://www.finra.org/investors/investing/investment-products/options
- OCC Options Disclosure Document: https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document

## 13. Immediate Next Step

Next milestone: first filled supervised paper order and full paper lifecycle review.

Build this next:

- Enable paper open execution deliberately for one supervised trade.
- Use fresh quote revalidation for all accepted open candidates before allocation, then the final pre-submit revalidation and entry cancel/replace management.
- Keep quantity at 1 spread and keep paper execution locks easy to turn off.
- Watch Discord order lifecycle, position monitor, daily summary, and SQLite logs.
- Close the spread through the guarded paper close path and review P&L.

The next milestone is not live trading. The next milestone is "one complete paper trade lifecycle is filled, monitored, closed, and understood."

## 14. Execution Progress

Last updated: 2026-06-05

Current state:

- Repo is initialized and pushed to `https://github.com/wcm/trading.git`.
- Mode remains paper trading; no live orders have been submitted.
- One supervised Alpaca paper order was submitted for a META put credit spread and canceled unfilled; no paper positions are currently open.
- Alpaca paper connectivity works for account, clock, positions, orders, stock bars, option contracts, option snapshots, and news.
- Discord notifications work and are required before execution gates can submit orders.
- SQLite logging is active for bot runs, option scans, LLM decisions, execution attempts, and order status events.
- The bot can scan put credit spreads, build LLM decision packets, validate decisions, generate MLeg previews, monitor positions, run one local cycle, and run a market-hours scheduler.
- Watchlist decisions can run concurrently up to `decision_engine.max_concurrent_symbols`, currently 8.
- Alpaca API requests use a 30-second timeout plus two retries for transport failures, rate limits, and 5xx responses.
- Paper open and close execution paths exist but are disabled by default behind config and CLI locks.
- Paper entry execution now refreshes all accepted open spread quotes before allocation, selects only execution-eligible candidates, refreshes the selected spread again immediately before submit, recalculates bounded limit pricing, polls stale entries, cancels unfilled orders, and can submit limited replacements within configured credit bounds.
- The scheduler now uses a split cadence: 1-minute checks, monitor-only supervision when positions exist, 5-minute new-open decision cycles, order lifecycle polling each check, and one after-market daily summary.
- Daily summaries focus on account equity, daily P&L, buying power, open positions, estimated open spread P&L, order lifecycle events, and execution attempts.
- `main.py` has been refactored into a thin CLI dispatcher with separate modules for parser, bootstrap, commands, run cycles, scheduler, summaries, notifications, order lifecycle, and shared utilities.
- Order lifecycle polling works and currently reports zero recent Alpaca paper orders.
- Paper option data uses Alpaca `indicative` because OPRA is not signed; live options trading should require OPRA.

Completed milestone groups:

- Project foundation: Python/uv scaffold, config, ignored secrets, logging, kill switch, Discord, SQLite.
- Alpaca integration: paper broker client for account, market data, options data, news, positions, orders, single-order lookup/cancel, transient request retries, and MLeg submission payloads.
- Strategy and data: put credit spread scanner, conservative credit/max-loss math, liquidity checks, trend checks, quote freshness, event/earnings context, and news context.
- LLM decisioning: OpenAI Responses API, strict JSON schema, prompt versioning, per-symbol watchlist decisions, mock mode, decision persistence, and validator guard rails.
- Risk and execution gates: max-loss/open-risk checks, symbol/candidate checks, stale-data blocks, all-candidate fresh quote revalidation, pre-submit quote revalidation, default-disabled paper open/close order submission, entry cancel/replace management, and execution-attempt logging.
- Monitoring loop: position reconstruction, close previews, P&L estimates, hard exit flags, monitor-before-open `run-cycle`, non-overlap lock, split-cadence local scheduler, and daily trading summary.
- Notifications: Discord summaries for scans, decisions, run cycles, execution attempts, scheduler heartbeat/errors, order lifecycle changes, and daily P&L/open-position summaries.
- Tests: unit coverage for scanning, LLM packets, validation, liquidity/events/news blocks, allocation fallback after revalidation errors, order previews, execution gates, all-candidate entry quote revalidation/cancel-replace, position monitoring, run-cycle/scheduler behavior, daily summaries, and order lifecycle polling.

Latest verification:

- `uv run python -m unittest discover -s tests` passed with 56 tests.
- `uv run python -m compileall src tests` passed.
- Parallel mock `run-cycle` with real Alpaca data completed successfully for all 8 watchlist symbols without OpenAI calls or order submission after all-candidate fresh quote revalidation changes.
- Live paper checks confirmed Alpaca connectivity, zero open positions, paper order lifecycle submit/new/cancel handling, daily-summary JSON generation, scheduler one-shot mock validation, smoke CLI validation, and Discord notification delivery.

Known gaps:

- External earnings/calendar provider integration is still not implemented.
- No filled paper trade lifecycle has been opened, monitored, closed, and reviewed yet.
- Cloud deployment is not started.
- Later-stage risk-pause optimization is not implemented yet: when daily/weekly open-risk gates block new opens and there are no open positions or open orders, the scheduler should enter a quiet risk-pause mode instead of continuing normal monitor/order-poll cadence. In that mode it should skip open discovery, LLM open decisions, position monitoring, and frequent order polling; continue heartbeats, error alerts, and the after-market daily summary; and wake at the next relevant reset boundary, meaning the next trading day for a daily-loss pause or the next trading week for a weekly-loss pause. If any open position or open order exists, monitoring should continue and guarded close/reduce-risk actions should remain allowed.
- Live trading remains out of scope until paper promotion criteria, Level 3 options approval, and OPRA/live data readiness are satisfied.

Recent implementation commits:

- `66e7aa1 Expand Discord decision details`
- `d7d9202 Add local market-hours scheduler`
- `a75ee44 Add guarded paper close execution`
- `dbb9a32 Add order lifecycle polling`

Latest milestone:

- Refactored the monolithic `main.py` into smaller modules.
- Kept CLI behavior and compatibility re-exports intact.
- Kept all paper order execution disabled unless config and CLI locks are deliberately enabled.

# Automatic Options Trading Bot Plan

Last updated: 2026-06-05
Status: Cloud paper trading, not live trading

This is the strategy and implementation plan for the trading bot. Operational
commands, cloud deployment steps, log viewing, and emergency procedures live in
[automatic_trading_bot_runbook.md](automatic_trading_bot_runbook.md).

## 1. Purpose

Build an automatic options trading bot that can learn in paper trading first and
later graduate to a small live account only after promotion criteria are met.
The first objective is not a perfect trading system. The first objective is a
controlled v1 that runs continuously, records decisions, sends reliable alerts,
and exposes real paper-trading behavior for review.

Current intent:

- Initial account context: USD 10,000 live account target, later.
- Current mode: Alpaca paper trading.
- Live mode: out of scope until paper promotion criteria are met.
- Broker: Alpaca only for v1.
- Notification channel: Discord.
- Strategy: defined-risk put credit spreads.
- Decision engine: LLM-directed decisions inside hard-coded guardrails.

This is an engineering and research plan, not financial advice. Options are
risky, and automated trading can lose money quickly. V1 deliberately avoids
naked options and market orders.

## 2. Current State

The bot is already running in cloud paper mode:

- Repo: `https://github.com/wcm/trading.git`.
- Cloud host: DigitalOcean Droplet, Ubuntu 24.04 LTS, 1 vCPU, 2 GB RAM.
- Cloud runtime: Docker Compose from `/opt/trading`.
- Container restart policy: `restart: unless-stopped`.
- Scheduler command: `trading-bot schedule-local --send-discord --send-cycle-discord --cycle-summary-only --json-output-dir data/scheduler_cycles --submit-paper --submit-paper-close`.
- Local scheduler: not running. The cloud scheduler is the single active scheduler.
- Paper execution: paper open and close paths are deliberately enabled in config and CLI flags.
- Live orders: none submitted.
- Alpaca paper positions: existing paper option legs were detected during the
  deployment smoke test; use the runbook commands or Alpaca dashboard for the
  current count.
- Market data: Alpaca indicative option feed because OPRA is not signed.

Key current operating posture from `config/settings.yaml`:

- `mode: paper`.
- Paper open and close submission locks are enabled for the cloud experiment.
- `risk.max_open_risk` is USD 5,000.
- `risk.max_new_trades_per_day` is 3.
- `risk.max_daily_loss` is USD 500.
- `risk.max_weekly_loss` is USD 1,000.
- `risk.max_open_positions` is intentionally high at 50; the main aggregate
  exposure cap is `risk.max_open_risk`.
- Scheduler tick is 1 minute; new-open discovery cadence is 5 minutes.

## 3. Core Decisions

### Broker

Use Alpaca only for v1.

Reasons:

- Alpaca is API-first and supports paper and live environments.
- Alpaca supports options and multi-leg orders through `order_class: "mleg"`.
- Paper trading can exercise the real order-construction flow before live mode.

Live promotion requires:

- Live Alpaca brokerage account.
- Live API keys.
- Options trading enabled.
- Options Level 3 approval for spreads.
- OPRA or otherwise suitable live options data.

If Level 3 approval is not granted, do not automatically downgrade to naked or
cash-secured options. That changes the risk profile and capital requirements.

### Strategy

Start with put credit spreads on liquid US tech stocks and ETFs.

A put credit spread sells a put and buys a lower-strike put as protection:

- Sell 1 AAPL 190 put.
- Buy 1 AAPL 185 put.
- Receive USD 1.00 credit.
- Max profit: USD 100.
- Max loss: `(5.00 - 1.00) * 100 = USD 400`.

This is a bullish-to-neutral, defined-risk strategy. It benefits if the
underlying stays above the short put strike and from time decay.

Why not naked options in v1:

- Naked short puts can require large buying power and assignment risk.
- Naked short calls can have theoretically unlimited loss.
- Spreads provide a known max loss before order submission.

### LLM Role

The LLM makes trading decisions because it can interpret organic context such as
news, headlines, market commentary, and unusual qualitative risk.

The LLM can decide:

- Whether to open, skip, hold, close, or disable trading.
- Whether news or market tone makes a symbol unsuitable.
- Which generated candidate spread to select.
- Whether to close or hold an existing spread when that flow is used.

Current scheduler note: existing-position supervision is primarily deterministic
today. The monitor path reconstructs put spreads and flags profit target,
loss trigger, short-strike threat, and close-before-expiry conditions. The
schema supports `close`, but the live cloud scheduler is not yet using a full
LLM-driven close decision loop.

The LLM cannot:

- Trade naked options.
- Trade symbols outside the approved watchlist.
- Trade contracts outside code-generated candidates.
- Place market orders.
- Override max loss, max open risk, daily/weekly loss gates, or kill switch.
- Call broker APIs or access secrets directly.
- Increase quantity beyond deterministic allocation/risk rules.

Invalid JSON, stale data, missing fields, unsupported actions, unknown
contracts, unclear credit/debit sign, failed risk checks, or active kill switch
all mean no new trade.

### Notifications

Discord is a required v1 dependency, not an optional nice-to-have.

The bot should notify for:

- Startup heartbeat.
- Scheduler heartbeat and errors.
- Candidate scan summaries.
- LLM decisions.
- Order submit/cancel/fill/reject events.
- Position monitor and close recommendations.
- Risk blocks.
- Daily account and P&L summary.

If Discord repeatedly fails, new openings should stop.

Current implementation note: execution gates block when the Discord webhook is
not configured. A repeated-notification-failure circuit breaker is still a
near-term hardening item.

## 4. Trading Specification

### Universe

Current watchlist:

- QQQ
- AAPL
- MSFT
- NVDA
- AMZN
- META
- GOOGL
- TSLA

Preferred lower-noise first symbols remain QQQ, AAPL, and MSFT. NVDA and TSLA
are allowed in paper mode but should be watched carefully because volatility can
move spreads quickly.

### Entry Cadence

Implemented now:

- The scheduler checks every minute.
- Existing-position monitoring runs before new-open discovery.
- New-open discovery runs on the slower open interval, currently every 5
  minutes.
- Account/risk gates can skip discovery and LLM open calls before symbols are
  scanned.

Planned timing guard:

- New entries should avoid the first 30 minutes after market open and the last
  60 minutes before market close. This is not implemented yet.

Implemented entry blockers include sharp symbol selloffs, stale bars/quotes,
failed moving-average trend, earnings/event blocks, high/negative LLM news
assessment, and account/risk gates.

### Candidate Selection

Initial put spread shape:

- Expiration: 7-21 calendar days to expiration.
- Short put delta target: approximately -0.20 to -0.30.
- Long put strike: lower than short put, usually USD 5 or USD 10 wide.
- Same expiration and same underlying for both legs.
- Quantity: starts from deterministic allocation and risk budget.

Liquidity filters reject candidates with:

- Wide bid/ask spread.
- Low open interest.
- Low or unreliable short-leg bid.
- Unclear spread mid-price.
- Stale broker quote data.

Current liquidity defaults:

- Short leg open interest: at least 500.
- Long leg open interest: at least 100.
- Short leg bid: at least USD 0.30.
- Leg spread: less than 15% of mid, or less than USD 0.50 absolute.
- Net credit: at least 20% of spread width.

### Market And News Filters

Implemented market/event validation rejects new open decisions when:

- Underlying is down more than 2.5% intraday.
- Underlying is below the configured 30-minute moving-average trend check.
- Quote or bar data is stale.
- Earnings or configured event context is too close.
- The LLM's returned news assessment is high risk or negative.

The news context is included in the LLM packet. News should never force an open.
It can support a decision, increase caution, or block a symbol.

Planned improvements:

- Add a broad-market regime filter instead of relying only on per-symbol trend
  context.
- Replace the manual earnings calendar with an external provider.

### Exit Rules

Implemented deterministic monitor flags recommend closing when any of these
happen:

- Profit target is reached, currently around 50% of entry credit.
- Spread value reaches around 2x entry credit.
- Underlying touches or falls below the short strike.
- Expiration is near, currently 3 calendar days.

V1 should not intentionally hold spreads through expiration.

Planned close-decision improvements:

- Include news/event risk in the close monitor path.
- Use the LLM for existing-position close/hold decisions once the deterministic
  close path is stable.
- Treat broker/account/position inconsistencies as explicit stop-trading or
  reduce-risk events with dedicated alerts.

## 5. Risk And Execution Rules

Risk engine rules are non-negotiable. The LLM can recommend, but execution
gates decide whether an order is allowed.

Account-level gates:

- Block new opens if projected open risk exceeds `risk.max_open_risk`.
- Block new opens if daily new-trade count exceeds `risk.max_new_trades_per_day`.
- Block new opens if daily loss exceeds `risk.max_daily_loss`.
- Block new opens if weekly loss exceeds `risk.max_weekly_loss`.
- Block new opens if account equity falls below `account.emergency_equity_floor`.

The daily/weekly/account gates run before symbol discovery and LLM open
decisions, so the bot does not spend time or tokens on new openings that cannot
be submitted anyway. Monitoring and guarded close paths remain allowed.

Execution rules:

- Use Alpaca MLeg orders.
- Use limit orders only.
- No market orders for options.
- For MLeg credit orders, submit negative `limit_price`.
- Use deterministic `client_order_id`.
- Revalidate accepted candidate quotes before allocation.
- Revalidate selected quote again immediately before submit.
- Poll stale entries, cancel unfilled orders, and allow bounded replacements.
- Log every execution attempt and broker response.

Manual stop:

- Reliable immediate stop on cloud today: `docker compose down`.
- File kill switch exists in code, but the durable cloud kill-switch path still
  needs to be verified and improved.

## 6. System Architecture

Python is the v1 implementation language.

Current code organization:

- `src/trading_bot/main.py`: thin CLI dispatcher.
- `src/trading_bot/cli/`: argument parser.
- `src/trading_bot/app.py`: config, logging, SQLite, kill switch, notifier bootstrap.
- `src/trading_bot/brokers/`: Alpaca adapter.
- `src/trading_bot/data/`: market data, options chain, news, events.
- `src/trading_bot/strategy/`: put credit spread scanner.
- `src/trading_bot/llm/`: OpenAI client, prompt versioning, strict schemas.
- `src/trading_bot/risk/`: account gates and kill switch.
- `src/trading_bot/execution/`: previews, gates, revalidation, entry management.
- `src/trading_bot/monitoring/`: position reconstruction and close analysis.
- `src/trading_bot/orders/`: order lifecycle polling.
- `src/trading_bot/cycles/`: monitor-before-open run cycle.
- `src/trading_bot/scheduler/`: split-cadence local/cloud scheduler.
- `src/trading_bot/summaries/`: daily trading summary.
- `src/trading_bot/storage/`: SQLite persistence.
- `src/trading_bot/notifications/`: Discord formatting and delivery.

Main cycle:

1. Load config and check kill switch.
2. Fetch account, positions, and recent orders.
3. Monitor existing positions first.
4. Submit guarded close orders only when allowed.
5. If position action takes priority, skip new opens.
6. Run account-level open-risk gates before discovery and LLM calls.
7. Scan each watchlist symbol independently.
8. Build LLM decision packets with account, position, market, option, and news context.
9. Run LLM decisions concurrently up to `decision_engine.max_concurrent_symbols`.
10. Validate all decisions and refresh all accepted candidates.
11. Allocate to the strongest eligible open under risk limits.
12. Revalidate selected quote and submit paper MLeg order if allowed.
13. Poll/manage entry order lifecycle.
14. Persist artifacts, SQLite records, logs, and Discord summaries.

Persistent state:

- SQLite: `data/trading_bot.sqlite3`.
- Scheduler artifacts: `data/scheduler_cycles/`.
- Logs: `logs/bot.log`.

## 7. Roadmap

### Current Milestone

First cloud-hosted paper market session review and full paper lifecycle review.

Next actions:

- Let the DigitalOcean scheduler wake at the next US market open.
- Watch logs and Discord during the session.
- Confirm monitor-only supervision runs for existing paper positions.
- Confirm new-open discovery runs every 5 minutes only when risk gates allow it.
- Confirm order lifecycle polling and daily summary are reliable.
- Review SQLite/log artifacts after the session.
- Close paper spreads through the guarded close path when appropriate.
- Review realized P&L and execution behavior.

Success means cloud paper mode runs through a real market session and at least
one paper spread lifecycle is opened, monitored, closed, and understood.

### Completed Milestones

- Python/uv project scaffold.
- Config, `.env` example, logging, SQLite, and kill switch.
- Discord notifications.
- Alpaca paper adapter for account, positions, orders, stock bars, option contracts, option snapshots, and news.
- Put credit spread scanner and candidate filters.
- LLM decision packets, strict schema validation, prompt versioning, and mock mode.
- Independent per-symbol watchlist LLM decisions with concurrency.
- Allocation and fresh quote revalidation.
- Alpaca MLeg preview and paper order submission.
- Entry cancel/replace management.
- Position monitoring and guarded paper close path.
- Order lifecycle polling and spread-trade persistence.
- Account-level risk gates before open discovery.
- Split-cadence scheduler with market-closed sleep.
- Daily trading summary.
- Dockerfile, Docker Compose worker, Ubuntu Docker bootstrap script.
- DigitalOcean cloud paper deployment.

Latest verification:

- `uv run python -m unittest` passed with 71 tests.
- `uv run python -m compileall src tests` passed.
- Docker image built successfully on the Droplet.
- Docker smoke test on the Droplet succeeded.
- Cloud scheduler started, sent Discord heartbeat, polled order lifecycle, saw market closed, and slept until Alpaca next open.

### Near-Term Improvements

- Update the LLM prompt wording now that cloud paper execution can submit
  orders after validation; it still contains historical read-only phrasing.
- Add explicit time-of-day entry blocks for the first 30 minutes after market
  open and last 60 minutes before market close.
- Add a repeated Discord failure circuit breaker that blocks new opens after
  notification delivery becomes unreliable.
- Verify cloud daily summary after market close.
- Verify remote kill switch behavior and make it durable on the mounted `data/` volume.
- Add external earnings/calendar provider instead of manual earnings dates.
- Add a broad-market regime filter.
- Add news/event-aware close monitoring.
- Add external uptime/dead-man monitoring for scheduler heartbeats.
- Add log shipping or periodic backup for SQLite/log artifacts.
- Implement risk-pause optimization when daily/weekly open-risk blocks are active and there are no open positions or open orders.
- Review and tune config after the first cloud market session.

### Live Promotion Criteria

Do not move to live trading until all are true:

- At least 20 paper trades or 2 full trading weeks, whichever comes first.
- No unreconciled positions or orders.
- No duplicate order submissions after restarts.
- No invalid LLM decision accepted by the validator.
- No missed exit caused by bot logic.
- Discord notifications received for every order, fill, close, risk alert, and daily summary.
- Paper drawdown stayed within configured daily and weekly limits.
- Live Alpaca account has Level 3 options approval.
- Live API keys are configured and tested in live read-only mode.
- OPRA/live options market data decision is made.

First live rollout should be one live trade only, with live additional entries
disabled until that lifecycle is reviewed.

## 8. Known Gaps

- No complete paper spread lifecycle has been opened, monitored, closed, and reviewed end to end yet.
- External earnings/calendar provider is not implemented.
- Time-of-day entry blocks are planned but not implemented.
- Existing-position close monitoring is deterministic and does not yet include news/event context.
- The LLM prompt still contains old read-only wording even though validated paper decisions can now lead to paper orders.
- Repeated Discord delivery failure does not yet trigger an automatic new-open circuit breaker.
- Durable cloud kill-switch path is not verified.
- No external uptime monitor or log shipping.
- Risk-pause optimization is documented but not implemented.
- Weekly loss is based on bot-observed account snapshots, so it is only as complete as recorded history.
- Paper options data uses Alpaca indicative feed; live options trading should require OPRA or equivalent.
- Live trading remains out of scope until promotion criteria are met.

## 9. References

- Alpaca Options Trading: https://docs.alpaca.markets/us/docs/options-trading
- Alpaca Options Level 3 / Multi-leg Trading: https://docs.alpaca.markets/us/docs/options-level-3-trading
- Alpaca Create Order API Reference: https://docs.alpaca.markets/us/reference/postorder
- Alpaca Option Chain API Reference: https://docs.alpaca.markets/us/reference/optionchain
- Alpaca Market Data API: https://docs.alpaca.markets/us/docs/about-market-data-api
- Alpaca Historical News Data: https://docs.alpaca.markets/us/docs/historical-news-data
- Alpaca Real-time Option Data: https://docs.alpaca.markets/us/docs/real-time-option-data
- OpenAI Responses API: https://developers.openai.com/api/reference/responses
- OpenAI Structured Outputs: https://developers.openai.com/api/docs/guides/structured-outputs
- Docker Engine on Ubuntu: https://docs.docker.com/engine/install/ubuntu/
- Docker Compose plugin: https://docs.docker.com/compose/install/linux/
- DigitalOcean Droplets: https://docs.digitalocean.com/products/droplets/
- FINRA Options Overview: https://www.finra.org/investors/investing/investment-products/options
- OCC Options Disclosure Document: https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document

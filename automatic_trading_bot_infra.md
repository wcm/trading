# Automatic Trading Bot Infrastructure Plan

Last updated: 2026-06-10
Status: Cloud paper trading, shared-infrastructure design in progress

This document describes the shared infrastructure for the automatic trading bot.
Strategy-specific rules live outside this file. For the current put credit
spread strategy, see [strategy/put_credit_strategy.md](strategy/put_credit_strategy.md).

Operational commands, cloud deployment steps, log viewing, and emergency
procedures live in [automatic_trading_bot_runbook.md](automatic_trading_bot_runbook.md).

## 1. Purpose

Build a trading bot platform that can run one or more strategy modules through
the same safe infrastructure:

- one broker/account adapter
- one scheduler
- one data layer
- one notification system
- one persistence layer
- one shared account risk engine
- one execution/order lifecycle engine
- strategy-specific scanners, decision packets, validators, and close logic

The goal is to avoid rebuilding broker, deployment, logging, alerts, risk, and
execution plumbing every time we add a new strategy.

Current live-money status: no live trading. The bot is running in Alpaca paper
mode only.

## 2. Current State

Currently implemented:

- Cloud paper bot runs on a DigitalOcean Ubuntu 24.04 Droplet.
- Runtime is Docker Compose from `/opt/trading`.
- Local scheduler should stay stopped while the cloud scheduler is running.
- Alpaca is the only broker adapter.
- Discord is the notification provider.
- SQLite is the local/cloud persistence layer.
- One strategy is implemented: `put_credit_strategy`.
- Paper open and close execution are deliberately enabled for the current paper
  experiment.

Not implemented yet:

- Multiple strategies running side by side.
- Per-strategy risk budgets.
- Per-strategy scheduler cadence.
- Per-strategy order allocation across competing signals.
- Strategy registry/config format.
- Generic strategy interface in code.

Important current operating posture:

- `mode: paper`
- `risk.max_open_risk`: USD 5,000 aggregate paper open risk
- `risk.max_new_trades_per_day`: 1
- `risk.max_daily_loss`: USD 500
- `risk.max_weekly_loss`: USD 1,000
- Scheduler tick: 1 minute
- Current new-open discovery cadence: 5 minutes

## 3. Strategy Separation Model

The bot should treat each strategy as a plugin-like module.

A strategy owns:

- strategy name and version
- tradable universe
- candidate generation
- strategy-specific market filters
- strategy-specific hard pre-LLM filters
- strategy-specific decision packet
- strategy-specific LLM prompt
- strategy-specific validator
- strategy-specific order preview builder
- strategy-specific position reconstruction
- strategy-specific close/hold rules
- strategy-specific reporting details

Shared infrastructure owns:

- broker API calls
- account state
- positions and orders
- scheduler loop
- shared market/news/event data fetches
- account-wide risk gates
- allocation across strategies
- execution submission
- order lifecycle polling
- SQLite storage
- Discord notifications
- daily summaries
- cloud deployment
- kill switch and emergency stop

The LLM may help decide inside a strategy, but it should never bypass shared
infrastructure gates or strategy hard filters.

## 4. Shared Runtime Flow

The target multi-strategy cycle should look like this:

1. Load config and secrets.
2. Check kill switch.
3. Fetch account, positions, open orders, recent orders, and clock.
4. Run shared account-level risk gates.
5. Reconstruct current positions by strategy.
6. Run close/monitor logic for all active strategy positions.
7. Submit guarded close/reduce-risk orders when allowed.
8. Refresh account state and shared risk after monitor/close finishes.
9. For each enabled strategy, run its discovery/decision path if its cadence is due and risk gates allow new opens.
10. Apply strategy hard filters before any LLM call.
11. Call the LLM only for candidates that passed hard filters, so it focuses on news and subjective judgment.
12. Validate every strategy decision with strategy-specific and shared validators.
13. Allocate risk across all accepted open decisions.
14. Revalidate fresh quotes immediately before submit.
15. Submit bounded limit orders only.
16. Poll/manage order lifecycle.
17. Persist artifacts, SQLite rows, logs, and Discord summaries.

Today this flow exists mostly for `put_credit_strategy`. The infrastructure plan
is to generalize it without losing the hard safety gates already working.

## 5. Shared Risk Model

Account-level risk gates apply before any strategy can open a new trade:

- daily loss limit
- weekly loss limit
- daily new-trade count
- emergency equity floor
- aggregate open risk
- kill switch
- broker/account uncertainty
- required notification configuration

Future multi-strategy risk should add:

- per-strategy max open risk
- per-strategy max daily trades
- per-symbol concentration limit
- per-underlying exposure limit across strategies
- correlation/group exposure limit, for example mega-cap tech exposure
- strategy priority when risk is scarce
- strategy cooldown after losses or repeated rejects

Shared principle: a strategy can ask for risk, but the infrastructure grants or
denies risk.

## 6. Shared Execution Model

Execution should remain centralized.

Strategies should produce order intents or previews. The shared execution layer
decides whether an order can actually be sent.

Shared execution rules:

- limit orders only for options
- no market orders
- deterministic `client_order_id`
- final broker/account/order/position refresh before submit
- fresh quote revalidation before submit
- duplicate-order checks
- max loss/open risk checks
- Discord configuration required before opening
- full request/response logging
- order lifecycle polling after submission

For options strategies, Alpaca MLeg support is shared infrastructure. Strategy
modules should not each invent their own broker API behavior.

## 7. Shared Data Model

The data layer should be reusable by all strategies.

Shared inputs:

- account state
- positions
- open orders
- recent orders
- stock bars
- option contracts
- option snapshots and Greeks
- news
- earnings/events
- market clock

Strategy-specific data processing can sit on top of shared raw data.

Example:

- Shared layer fetches AAPL option snapshots.
- `put_credit_strategy` builds put credit spread candidates.
- A future covered-call strategy could reuse the same option snapshots but build
  call candidates.

## 8. Shared Persistence Model

SQLite should store common records with strategy identifiers.

Shared records should include:

- bot runs
- strategy runs
- candidate scans
- LLM decisions
- validator results
- execution attempts
- order status events
- spread/trade records
- account snapshots
- daily summaries
- risk events

Future persistence improvement:

- Add `strategy_name` and `strategy_version` consistently across strategy-level
  tables.
- Add a normalized `strategy_positions` or `trades` table that can represent
  non-put-credit strategies.
- Keep raw broker payloads for audit/debugging.

## 9. Shared Notifications

Discord remains the first notification provider.

Shared notifications:

- scheduler error notifications
- scheduler errors
- account risk blocks
- strategy run summaries
- accepted/rejected decisions
- order submit/cancel/fill/reject
- close recommendations
- daily account/P&L summary
- emergency stop / kill switch alerts

Multi-strategy notifications should identify:

- strategy name
- symbol/underlying
- action
- selected candidate or position
- risk used
- reason for open/skip/close
- execution status

## 10. Strategy Configuration Shape

Current config is still mostly single-strategy. The target shape should make
strategies explicit:

```yaml
strategies:
  put_credit_strategy:
    enabled: true
    module: trading_bot.strategy.put_credit_spread
    open_interval_minutes: 5
    max_open_risk: 5000
    max_new_trades_per_day: 1
    watchlist:
      - QQQ
      - AAPL
      - MSFT

  covered_call_strategy:
    enabled: false
    module: trading_bot.strategy.covered_call
    open_interval_minutes: 15
    max_open_risk: 1000
    watchlist: []
```

Shared config should remain outside strategy blocks:

- broker
- global mode
- account-level risk
- notifications
- storage
- runtime
- cloud/deployment assumptions

## 11. Strategy Interface Target

Target interface concept:

```text
StrategyModule
  name
  version
  discover(context) -> candidates
  build_decision_packet(context, candidates) -> packet
  decide(packet) -> decision
  validate(decision, packet) -> validator_result
  build_open_order_preview(decision, candidate) -> order_preview
  reconstruct_positions(account_positions) -> strategy_positions
  monitor_positions(strategy_positions, context) -> close_or_hold_actions
  build_close_order_preview(action) -> order_preview
```

Shared context should contain:

- config
- account
- clock
- positions
- orders
- market data
- news/events
- current risk state

## 12. Current Strategy Inventory

### `put_credit_strategy`

Status: implemented and running in paper mode.

Summary:

- defined-risk bullish/neutral options strategy
- sells a put and buys a lower put for protection
- uses LLM for open decisions
- uses deterministic monitor flags for close recommendations
- uses Alpaca MLeg limit orders

Documentation:

- [strategy/put_credit_strategy.md](strategy/put_credit_strategy.md)

### Future Strategies

Possible future strategies:

- covered calls
- cash-secured puts
- call credit spreads
- long call/put event trades
- ETF-only lower-volatility spreads

Any future strategy should be added behind the shared infrastructure interface,
not as a separate bot.

## 13. Roadmap

Current milestone:

- Let the cloud paper bot run through market sessions.
- Review the current strategy lifecycle end to end.
- Keep live trading out of scope.

Near-term infrastructure work:

- Replace single-strategy config shape with explicit `strategies:` config.
- Add strategy registry and enabled-strategy loader.
- Add strategy-scoped run artifacts and SQLite records.
- Add per-strategy risk budgets.
- Add shared allocator across strategies.
- Update Discord summaries to include strategy name consistently.
- Update prompt wording now that paper execution can submit orders after
  validation.
- Add repeated Discord failure circuit breaker.
- Verify durable cloud kill switch path.
- Add external uptime monitoring.
- Add log/SQLite backup or shipping.

Later infrastructure work:

- Generalize broad-market regime filters across future strategies.
- Add external earnings/calendar provider.
- Add risk-pause mode when no positions/orders need supervision.
- Add strategy performance reports.
- Add live-read-only mode before any live execution.

## 14. Live Promotion Criteria

Do not move to live trading until all are true:

- paper mode has at least 20 trades or 2 full trading weeks
- no unreconciled positions or orders
- no duplicate order submissions after restarts
- no invalid LLM decision accepted by validators
- no missed exit caused by bot logic
- Discord notifications received for every critical event
- paper drawdown stayed within daily and weekly limits
- live Alpaca account has required options approval for enabled strategies
- live API keys are configured and tested in read-only mode
- OPRA/live options market data decision is made
- multi-strategy risk allocation is understood if more than one strategy is enabled

First live rollout should be one live trade only, with additional live entries
disabled until that lifecycle is reviewed.

## 15. Known Gaps

- Multi-strategy orchestration is planned but not implemented.
- Current config is still mostly single-strategy.
- Current SQLite schema is only partly strategy-aware.
- Per-strategy risk budgets are not implemented.
- Cross-strategy allocation is not implemented.
- Existing-position close monitoring is deterministic for the current strategy
  and does not yet include news/event context.
- The current LLM prompt still contains old read-only wording.
- Repeated Discord delivery failure does not yet trigger an automatic new-open
  circuit breaker.
- Durable cloud kill-switch path is not verified.
- No external uptime monitor or log shipping.
- Weekly loss is based on bot-observed account snapshots, so it is only as
  complete as recorded history.
- Paper options data uses Alpaca indicative feed; live options trading should
  require OPRA or equivalent.

## 16. References

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

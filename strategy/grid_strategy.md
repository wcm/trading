# TQQQ Grid Strategy Proposal (v1)

Last updated: 2026-07-18

This is the initial proposal for a separate paper-trading grid strategy using
`TQQQ`. It is an engineering note for experimentation, not financial advice.

Shared infrastructure details live in
[../automatic_trading_bot_infra.md](../automatic_trading_bot_infra.md).

Backtest build details live in
[grid_backtest_plan.md](grid_backtest_plan.md).

## 1. Strategy Summary

The grid strategy trades small pieces around price levels.

Plain English:

```text
If TQQQ falls to a lower grid level, buy a small amount.
If TQQQ rises to the next grid level, sell that small amount.
Repeat while the market moves up and down.
```

This strategy is designed for a market that wiggles. It can perform badly when
the price trends strongly downward, because the bot can keep buying while the
position becomes more and more underwater.

For v1, this strategy should trade **TQQQ shares only**:

- no options
- no short selling
- no margin assumptions
- no market orders unless we explicitly add an emergency liquidation mode later
- limit orders only for normal grid orders

## 2. Why TQQQ Is Special

`TQQQ` is a leveraged ETF. ProShares describes it as targeting three times the
daily performance of the Nasdaq-100 Index before fees and expenses.

This matters because:

- TQQQ can move much more violently than QQQ.
- A normal down day in QQQ can become a much larger down day in TQQQ.
- Leveraged ETFs reset daily, so longer holding periods can behave differently
  from simply multiplying the index return by three.
- In a choppy market, leverage and daily reset effects can help or hurt
  depending on the exact price path.

So the grid can be interesting on TQQQ, but we must avoid infinite averaging
down. The bot needs hard limits.

Sources:

- [ProShares TQQQ page](https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq)
- [SEC investor bulletin on leveraged and inverse ETFs](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/sec)

## 3. Separate Account, Separate Risk Gates

The grid strategy should run on the new paper account.

It should not share the same active risk limits as `put_credit_strategy`.
The codebase and infrastructure can be shared, but the account, config, state,
and risk budget should be separate.

Recommended separation:

```text
Put credit bot:
  env:      .env
  config:   config/settings.yaml
  database: data/trading_bot.sqlite3
  logs:     logs/

Grid bot:
  env:      .env.grid
  config:   config/settings.grid.yaml
  database: data/grid/trading_bot.sqlite3
  logs:     logs/grid/
```

This keeps the experiments independent:

- one strategy cannot accidentally use the other account's keys
- one strategy's loss gate does not pause the other strategy
- daily summaries are easier to understand
- debugging is cleaner

## 4. Current Risk Gates

These are the active Profile 3 paper-testing values. Profile 3 was selected
after a seven-profile, six-month comparison. The corrected backtester keeps the
grid anchor after a complete exit, matching the live strategy.

- The `2026-01-17` through `2026-07-17` test returned `14.97%` with a
  `-15.25%` maximum simulated drawdown.
- The split-adjusted `2025-07-17` through `2026-07-17` test returned `26.83%`
  with a `-16.13%` maximum simulated drawdown.

Both tests used the active `5%` recenter rule. Historical backtests use
split-adjusted Alpaca bars so a share split cannot be mistaken for a market
loss or leave simulated share quantities and sell targets on incompatible
price scales.

Historical stress tests use split-adjusted daily bars where older intraday data
is unavailable:

| Period | Profile 2 with 8% recenter | Profile 3 with 5% recenter |
| --- | ---: | ---: |
| `2018-08-01` to `2019-12-31` | `+26.24%`, `-37.53%` drawdown | `+26.91%`, `-37.28%` drawdown |
| `2020-01-01` to `2020-12-31` | `+14.62%`, `-52.03%` drawdown | `+22.65%`, `-53.02%` drawdown |
| `2022-01-01` to `2022-12-31` | `-70.54%`, `-72.18%` drawdown | `-62.51%`, `-64.08%` drawdown |
| `2021-11-01` to `2023-12-31` | `-19.04%`, `-58.25%` drawdown | `-19.54%`, `-61.18%` drawdown |

These stress tests show that neither profile has adequate protection against a
long TQQQ bear market. Keep the strategy in paper mode until a bear-market and
inventory-protection rule has been implemented and tested.

```yaml
grid_risk:
  strategy_capital: 10000
  max_inventory_value: 9000
  cash_reserve: 1000
  max_unrealized_loss: 1800
  pause_new_buys_after_consecutive_down_levels: null
```

Plain English:

- The grid can use up to about `$10,000` of paper capital.
- It should not hold more than about `$9,000` of TQQQ inventory.
- It keeps about `$1,000` in reserve.
- Each buy order starts around `$500` and grows on deeper levels, capped at `$1,100`.
- There is no fixed limit on the number of active grid levels.
- New buys continue only while the inventory, reserve, and unrealized-loss gates allow them.
- If losses are too large, it stops opening new buys.
- The daily/weekly loss and TQQQ/QQQ crash gates remain planned work. They are
  not enforced during the first supervised paper test.

Important distinction:

```text
"Pause new buys" does not mean "stop managing the position."
```

Even when new buys are paused, the bot should still:

- monitor open inventory
- keep or replace sell orders
- send alerts
- calculate P&L
- obey emergency rules

## 5. Current Grid Settings

Active Profile 3 paper settings:

```yaml
grid_strategy:
  name: grid_tqqq
  symbol: TQQQ
  grid_spacing_pct: 3.0
  base_order_notional: 500
  max_buy_levels_below_anchor: 16
  take_profit_levels: 1
  anchor_mode: latest_bar_close
  recenter_only_when_flat: true
  recenter_up_pct: 5.0
  allow_overnight_inventory: true
  allow_fractional_shares: true

adaptive_sizing:
  enabled: true
  scale_factor: 8.0
  max_order_multiplier: 2.25
  max_single_order_notional: 1100
```

Plain English:

- Start with one symbol: `TQQQ`.
- Place grid levels roughly every `3.0%`.
- Start around `$500` per buy level.
- Buy a little more when TQQQ has dropped farther from the grid anchor.
- Never let one adaptive buy exceed `$1,100` or `2.25x` the base amount.
- Sell each bought lot one grid level higher.
- Use fractional shares so each buy stays close to its intended dollar amount.
- Only move the grid upward when we have no TQQQ position.
- If TQQQ rises about `5.0%` while we are flat, move the grid anchor up instead
  of buying immediately.
- Allow overnight inventory for paper testing. An end-of-day new-buy cutoff is
  planned but not implemented yet.

Why allow overnight inventory in v1?

Grid strategies often need time to recover from dips. If we force the bot to
sell everything at the close each day, the strategy becomes more like intraday
mean-reversion scalping, and it may lock in losses too often.

But this is risky with TQQQ, so the max inventory, loss gates, and down-day
pause rules are important.

## 6. Open Logic

The bot should run during US market hours.

Current v1 command:

```bash
uv run trading-bot \
  --settings config/settings.grid.yaml \
  --env .env.grid \
  grid-cycle \
  --json-output data/grid/grid_cycle_preview.json
```

To allow paper orders:

```bash
uv run trading-bot \
  --settings config/settings.grid.yaml \
  --env .env.grid \
  grid-cycle \
  --submit-paper \
  --send-discord \
  --json-output data/grid/grid_cycle_latest.json
```

To run it continuously during market hours:

```bash
uv run trading-bot \
  --settings config/settings.grid.yaml \
  --env .env.grid \
  grid-schedule-local \
  --submit-paper \
  --send-discord \
  --json-output-dir data/grid/cycles
```

This first bot version:

- reads recent Alpaca `TQQQ` bars
- stores grid state in `data/grid/grid_state.json`
- uses the latest bar to initialize or update the grid anchor
- reconciles any previous grid order IDs saved in the state file
- creates buy/sell intents from deterministic grid rules
- sends paper limit orders only when `--submit-paper` is used and the market is open
- can run continuously with `grid-schedule-local`
- sleeps until Alpaca's next market open when the market is closed
- never sends market orders

Every minute:

1. Fetch current TQQQ price.
2. Reconcile the real Alpaca TQQQ position and open orders with local grid state.
3. Check the currently implemented grid risk gates.
4. If flat and no grid exists, create a new grid around the latest bar close.
5. If flat and price rises enough above the anchor, move the anchor up.
6. If price reaches a lower buy level, place a small `DAY` buy limit order.
7. If a buy fills, place the paired sell limit immediately, one grid level
   higher. Whole-share sells use `GTC`; fractional sells use `DAY`.
8. If new buys are paused, do not place new buy orders, but continue managing
   sell orders and inventory.

The first version should not ask the LLM to choose every order. The grid order
rules should be deterministic.

Plain English:

```text
Buy dips.
Sell rebounds.
If TQQQ runs upward while we own nothing, move the grid upward.
Do not buy just because price went up.
```

Adaptive sizing formula:

```text
buy amount = base amount * (1 + scale factor * drop from anchor)
```

Example with `$500` base and scale factor `8.0`:

```text
3% drop:  500 * (1 + 8 * 0.03) = about $620
6% drop:  500 * (1 + 8 * 0.06) = about $740
10% drop: 500 * (1 + 8 * 0.10) = about $900
```

The multiplier and dollar caps mean a single buy cannot exceed `$1,100`.

## 7. Close / Sell Logic

Each filled buy should create a sell target.

Example:

```text
Buy TQQQ at 95.00
Grid spacing is 3.0%
Sell target is about 97.85
```

The bot should track each filled lot.

When the paired sell fills:

- record realized P&L
- send a Discord fill message
- free up inventory budget
- allow the next lower buy level if risk gates still pass

Order duration:

- triggered buy orders use `DAY` and expire at that trading day's close if unfilled
- whole-share profit-taking sells use `GTC` and remain open across trading days
- fractional profit-taking sells use `DAY`, as required by Alpaca; if one
  expires unfilled, the bot returns the lot to open state and recreates the
  paired sell on the next market cycle

## 8. LLM Role

For this strategy, the LLM should be a **market risk assistant**, not the main
order engine.

Good LLM jobs:

- read recent news about Nasdaq, mega-cap tech, Fed/rates, earnings shocks, or
  market stress
- classify the current regime as normal, risky, or extreme
- recommend whether to allow new buys, pause new buys, or alert the user
- explain unusual behavior in the daily summary

Bad LLM jobs for v1:

- inventing grid prices
- choosing random share sizes
- overriding max inventory
- bypassing loss gates
- deciding to average down without limit

Recommended v1 LLM output:

```json
{
  "action": "allow_new_buys | pause_new_buys | alert_only",
  "confidence": 0.0,
  "reason": "short plain-English reason"
}
```

The numeric gates should run before the LLM. If the account is already blocked
by hard risk rules, there is no reason to pay for an LLM call.

## 9. Discord Messages

Keep messages short and easy to scan.

Implemented event message titles:

- `Grid Buy Filled`
- `Grid Sell Filled`
- `Grid Buy Submitted`
- `Grid Sell Submitted`
- `Grid Buy/Sell Partially Filled`
- `Grid Buy/Sell Canceled`, `Expired`, or `Rejected`
- `Grid Safety Block`
- `Grid Bot Error`

During the initial paper test, every one-minute scheduler cycle also sends a
`Grid Status` message. It shows the current TQQQ price, latest 5-minute price
change, distance from the grid anchor, next buy level, shares held, working
orders, unrealized P&L, and a short plain-English status. Set
`notifications.grid_status_every_cycle` to `false` in
`config/settings.grid.yaml` to return to event-only messages.

The most important daily summary fields:

- current TQQQ shares
- average inventory cost
- current TQQQ price
- realized P&L today
- unrealized P&L
- total P&L
- cash used
- active buy orders
- active sell orders
- whether new buys are allowed or paused

## 10. Implementation Phases

### Phase 1: Read-Only Grid Preview - Done

Build the grid calculator without placing orders.

It should show:

- anchor price
- buy levels
- sell levels
- proposed order size
- current risk status
- what the bot would do now

Implemented with:

```bash
uv run trading-bot --settings config/settings.grid.yaml grid-cycle
```

### Phase 2: Paper Orders, Small Size - Started

Enable paper orders using the new paper account.

Start with:

- one symbol: TQQQ
- one grid
- limit orders only
- small order size
- separate SQLite database
- separate logs
- separate Discord title prefix

Current status:

- `grid-cycle --submit-paper` can submit paper limit orders
- buy order fills are reconciled into open grid lots
- open lots can create paired sell limit orders
- sell fills record realized P&L inside the grid state file
- `grid-schedule-local` can run the cycle every minute during market hours
- the runtime fails closed if Alpaca TQQQ positions/orders disagree with local state
- paired sells are submitted immediately after buy fills; whole-share sells use
  `GTC`, while fractional sells use `DAY` and are recreated after expiration

### Phase 3: Monitoring And Reporting

Add:

- fill tracking
- paired lot tracking
- realized/unrealized P&L
- daily summary
- pause/resume reasons
- inventory age

Event-only order and safety notifications are implemented. The daily grid
summary remains future work.

## 11. Local Paper Test

Archive any state left by an older account before the first run:

```bash
mv data/grid/grid_state.json data/grid/grid_state.before-new-account.json
```

Run a read-only preview:

```bash
uv run trading-bot \
  --settings config/settings.grid.yaml \
  --env .env.grid \
  grid-cycle \
  --json-output data/grid/grid_cycle_preview.json
```

Start supervised paper execution:

```bash
uv run trading-bot \
  --settings config/settings.grid.yaml \
  --env .env.grid \
  grid-schedule-local \
  --submit-paper \
  --send-discord \
  --json-output-dir data/grid/cycles
```

Use only one scheduler process for this account.

## 12. Separate Grid Container

The grid bot has its own Compose file and does not use the options bot's
`.env` or `settings.yaml`:

```bash
docker compose -f docker-compose.grid.yml build
docker compose -f docker-compose.grid.yml run --rm grid-bot \
  uv run --no-sync trading-bot \
  --settings config/settings.grid.yaml \
  smoke --check-alpaca --send-discord
docker compose -f docker-compose.grid.yml up -d
docker compose -f docker-compose.grid.yml logs -f grid-bot
```

## 13. Optional LLM News Gate

Add LLM only after deterministic grid behavior is correct.

The LLM should decide whether market/news conditions are too strange for new
buys, but it should not create orders directly.

## 14. Current Starting Decision

```text
Use separate config/account/state.
Start TQQQ only.
Allow overnight inventory in paper.
Use $10,000 strategy capital.
Use $500 base grid buys with adaptive sizing.
Use fractional shares to keep each purchase close to its intended dollar size.
Use 3.0% grid spacing based on the first 1-month, 3-month, and 6-month
intraday backtest comparisons.
Add LLM as a phase 2/3 risk pause layer, not as the first order engine.
```

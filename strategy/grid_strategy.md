# TQQQ Grid Strategy Proposal

Last updated: 2026-06-14

This is the initial proposal for a separate paper-trading grid strategy using
`TQQQ`. It is an engineering note for experimentation, not financial advice.

Shared infrastructure details live in
[../automatic_trading_bot_infra.md](../automatic_trading_bot_infra.md).

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

## 4. Initial Risk Gates

These are proposed starting values for paper testing. They are intentionally
simple and can be adjusted after we see behavior.

```yaml
grid_risk:
  strategy_capital: 10000
  max_inventory_value: 8000
  cash_reserve: 2000
  max_single_order_notional: 500
  max_open_buy_orders: 16
  max_open_sell_orders: 16
  max_daily_realized_loss: 500
  max_weekly_realized_loss: 1000
  max_unrealized_loss: 1200
  max_intraday_tqqq_drop_pct: 8.0
  max_intraday_qqq_drop_pct: 3.0
  pause_new_buys_after_consecutive_down_levels: 5
```

Plain English:

- The grid can use up to about `$10,000` of paper capital.
- It should not hold more than about `$8,000` of TQQQ inventory.
- It keeps about `$2,000` in reserve.
- Each buy order is small, around `$500`.
- If losses are too large, it stops opening new buys.
- If TQQQ or QQQ is falling too hard today, it stops opening new buys.

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

## 5. Initial Grid Settings

Recommended v1 settings:

```yaml
grid_strategy:
  name: grid_tqqq
  symbol: TQQQ
  reference_symbol: QQQ
  grid_spacing_pct: 0.80
  base_order_notional: 500
  max_buy_levels_below_anchor: 16
  take_profit_levels: 1
  anchor_mode: session_start_price
  recenter_only_when_flat: true
  allow_overnight_inventory: true
  stop_new_buys_minutes_before_close: 15
  keep_sell_orders_after_new_buys_stop: true
```

Plain English:

- Start with one symbol: `TQQQ`.
- Use QQQ as the calmer reference market.
- Place grid levels roughly every `0.80%`.
- Buy about `$500` at each lower level.
- Sell each bought lot one grid level higher.
- Only rebuild the whole grid when we have no TQQQ position.
- Allow overnight inventory for paper testing, but stop adding new buys near the
  end of the trading day.

Why allow overnight inventory in v1?

Grid strategies often need time to recover from dips. If we force the bot to
sell everything at the close each day, the strategy becomes more like intraday
mean-reversion scalping, and it may lock in losses too often.

But this is risky with TQQQ, so the max inventory, loss gates, and down-day
pause rules are important.

## 6. Open Logic

The bot should run during US market hours.

Every minute:

1. Fetch current TQQQ price.
2. Fetch QQQ reference price and trend.
3. Check grid risk gates.
4. Check current TQQQ position and open orders.
5. If flat and no grid exists, create a new grid around the anchor price.
6. If price reaches a lower buy level, place a small buy limit order.
7. If a buy fills, place the paired sell limit order one grid level higher.
8. If new buys are paused, do not place new buy orders, but continue managing
   sell orders and inventory.

The first version should not ask the LLM to choose every order. The grid order
rules should be deterministic.

## 7. Close / Sell Logic

Each filled buy should create a sell target.

Example:

```text
Buy TQQQ at 100.00
Grid spacing is 0.80%
Sell target is about 100.80
```

The bot should track each filled lot.

When the paired sell fills:

- record realized P&L
- send a Discord fill message
- free up inventory budget
- allow the next lower buy level if risk gates still pass

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

Suggested message titles:

- `Grid Started`
- `Grid Buy Filled`
- `Grid Sell Filled`
- `Grid Paused`
- `Grid Risk Limit Hit`
- `Grid Daily Summary`

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

### Phase 1: Read-Only Grid Preview

Build the grid calculator without placing orders.

It should show:

- anchor price
- buy levels
- sell levels
- proposed order size
- current risk status
- what the bot would do now

### Phase 2: Paper Orders, Small Size

Enable paper orders using the new paper account.

Start with:

- one symbol: TQQQ
- one grid
- limit orders only
- small order size
- separate SQLite database
- separate logs
- separate Discord title prefix

### Phase 3: Monitoring And Reporting

Add:

- fill tracking
- paired lot tracking
- realized/unrealized P&L
- daily summary
- pause/resume reasons
- inventory age

### Phase 4: Optional LLM News Gate

Add LLM only after deterministic grid behavior is correct.

The LLM should decide whether market/news conditions are too strange for new
buys, but it should not create orders directly.

## 11. First Decisions To Make

Before coding, we should confirm:

1. Should v1 allow overnight TQQQ inventory?
2. Should the first paper capital cap be `$10,000`, or should it use the full
   paper account?
3. Is `$500` per buy level aggressive enough for the first test?
4. Should the same Discord webhook be used, with titles clearly saying `Grid`,
   or should grid have its own webhook/channel?
5. Should the LLM news gate be added immediately, or after the deterministic
   grid is proven in paper?

My default recommendation:

```text
Use separate config/account/state.
Start TQQQ only.
Allow overnight inventory in paper.
Use $10,000 strategy capital.
Use $500 per grid buy.
Use 0.80% grid spacing.
Add LLM as a phase 2/3 risk pause layer, not as the first order engine.
```

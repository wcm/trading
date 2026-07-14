# TQQQ Grid Backtest Build Plan

Last updated: 2026-06-17

This plan describes how to build a backtest mechanism before we run the TQQQ
grid strategy in paper trading. The goal is not to prove the strategy will make
money. The goal is to understand how often it trades, how much inventory it
builds, and how badly it can hurt during large drawdowns.

Related strategy proposal:
[grid_strategy.md](grid_strategy.md)

## 1. Questions The Backtest Must Answer

The first version should answer these questions:

1. Is `0.80%` grid spacing too tight for TQQQ?
2. Is `5.0%` grid spacing too wide, too slow, or reasonable?
3. How many trades happen per day, week, and year?
4. How much TQQQ inventory can build up during a selloff?
5. What is the worst unrealized loss?
6. How long can the bot be stuck holding inventory?
7. How much cash does the strategy need before it stops buying?
8. Which settings survive ugly periods like 2020 and 2022 better?

Plain English:

```text
Before letting the bot trade, replay old TQQQ prices and ask:
"What would Max have done?"
```

## 2. Scope For Version 1

Backtest only:

- `TQQQ` shares
- long-only grid
- no options
- no margin
- no short selling
- limit-order style fills
- deterministic rules
- no LLM decisions

The LLM news gate can be tested later. First we need to know whether the raw
grid mechanics make sense.

## 3. Data Plan

### Phase 1 Data: Daily OHLC

Use daily open/high/low/close bars first.

Why:

- fast to build
- enough to compare `0.80%`, `2%`, `3%`, `5%`, and `8%`
- enough to reveal inventory blowups

Limitation:

Daily bars do not tell us the exact order of events inside the day. If the high
and low both hit grid levels, we do not know which happened first.

So the daily backtest must use conservative fill assumptions.

### Phase 2 Data: Intraday OHLC

Use 5-minute or 1-minute TQQQ bars after the daily prototype works.

Why:

- closer to the real bot
- better trade count estimate
- better fill timing
- less ambiguity about whether buy or sell happened first

Data source should initially reuse Alpaca's stock bars API because the project
already has an Alpaca client.

Raw downloaded bars should be cached locally:

```text
data/backtests/cache/TQQQ_1Day_2010-02-11_2026-06-17.json
data/backtests/cache/TQQQ_5Min_2022-01-01_2026-06-17.json
```

This avoids repeatedly downloading the same history while we tune the simulator.

## 4. Conservative Fill Model

This is the most important part of the backtest.

The simulator should avoid pretending it can perfectly buy the low and sell the
high inside the same candle.

Recommended v1 rules:

1. At the start of each bar, process sell orders that already existed before the
   bar started.
2. If the bar high reaches an existing sell limit, count that sell as filled at
   the limit price.
3. If any sell fills in this bar, skip new buys until the next bar.
4. Then process buy levels.
5. If the bar low reaches one or more buy levels, fill buys at their limit
   prices, as long as risk gates allow them.
6. A sell target created by a buy cannot fill until the next bar.
7. Do not assume same-bar buy-and-sell profit.
8. If several buy levels are crossed in one bar, fill them from highest price to
   lowest price until risk gates stop more buying.

Plain English:

```text
Be a little pessimistic.
Do not let the backtest cheat.
```

## 5. Grid Mechanics

### Anchor

The first version should use:

```yaml
anchor_mode: first_bar_open
recenter_only_when_flat: true
recenter_up_pct: 5.0
```

The anchor is the price used to build the grid.
Once a grid is created, its buy levels persist across bars. This matters for
intraday backtests because a 5% level can be reached after a slow drift over
many 5-minute candles, not only after one large candle.

When there are no open lots, the grid can move upward if TQQQ rises enough:

```text
If anchor is 100 and recenter_up_pct is 5.0:
  close at 105 or higher means the new anchor becomes the latest close
```

The simulator does not allow a recenter and a new buy in the same bar. The new
buy levels become active on the next bar.

Example with `anchor = 100` and `grid_spacing_pct = 5.0`:

```text
Buy level 1: 95.00
Buy level 2: 90.25
Buy level 3: 85.74
Buy level 4: 81.45
```

Use geometric levels:

```text
next lower level = previous level * (1 - grid_spacing_pct / 100)
```

### Buy Size

Use fixed notional size:

```yaml
base_order_notional: 500
```

The simulator can start with whole shares:

```text
shares = floor(base_order_notional / buy_price)
```

If this makes order sizes too uneven, later we can support fractional shares.

### Adaptive Buy Size

The backtester should also support adaptive sizing:

```yaml
adaptive_sizing:
  enabled: true
  scale_factor: 8.0
  max_order_multiplier: 2.0
  max_single_order_notional: 800
```

Formula:

```text
buy amount = base amount * (1 + scale factor * drop from anchor)
```

This lets deeper grid levels buy more, but the cap prevents the bot from
doubling down without limit.

### Sell Target

Each filled buy gets its own sell target.

For v1:

```text
sell target = buy_price * (1 + grid_spacing_pct / 100)
```

This is simpler than tracking the exact next grid line and is good enough for
the first comparison.

## 6. Risk Gates To Simulate

The backtest should use grid-specific risk gates, separate from the put credit
strategy.

Initial test values:

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
  pause_new_buys_after_consecutive_down_levels: null
```

The active-level cap was disabled after the initial comparison runs. The
inventory, cash-reserve, and unrealized-loss gates remain active.

For the first daily backtest, intraday loss gates can be approximated using the
daily low versus the daily open.

The most important gates for v1:

- stop buying when max inventory value is reached
- stop buying when cash reserve would be breached
- stop buying when unrealized loss is too large
- keep managing sell orders even when new buys are paused

## 7. Metrics

Each backtest run should output:

- start date and end date
- grid spacing
- order size
- final equity
- realized P&L
- unrealized P&L
- total P&L
- maximum equity drawdown
- worst unrealized loss
- maximum TQQQ shares held
- maximum inventory market value
- maximum cash used
- number of buys
- number of sells
- average trades per day
- number of days with at least one trade
- longest inventory holding period
- number of days new buys were paused
- number of risk-gate blocks by reason
- number of upward recenters
- number of open lots left at the end

The key comparison table should look like this:

```text
Spacing | P&L | Max DD | Worst Unrealized | Buys | Sells | Recenters | Open Lots
0.80%   | ... | ...    | ...              | ...  | ...   | ...       | ...
2.00%   | ... | ...    | ...              | ...  | ...   | ...       | ...
3.00%   | ... | ...    | ...              | ...  | ...   | ...       | ...
5.00%   | ... | ...    | ...              | ...  | ...   | ...       | ...
8.00%   | ... | ...    | ...              | ...  | ...   | ...       | ...
```

## 8. Parameter Sweep

The first parameter sweep should test:

```yaml
grid_spacing_pct:
  - 0.80
  - 2.00
  - 3.00
  - 5.00
  - 8.00

base_order_notional:
  - 400
  - 250
  - 500
  - 1000

adaptive_scale_factor:
  - 0
  - 5
  - 8
  - 10
  - 15

max_inventory_value:
  - 5000
  - 8000
  - 10000
```

Do not test too many combinations at first. We want clarity, not a giant table
that nobody can understand.

## 9. Proposed Code Structure

Add a small backtesting package:

```text
src/trading_bot/backtesting/
  __init__.py
  bars.py              # load/cache historical bars
  grid.py              # grid simulator
  metrics.py           # P&L, drawdown, trade statistics
  reports.py           # JSON/CSV/Markdown output
```

Add tests:

```text
tests/test_grid_backtest.py
```

Add CLI commands:

```bash
trading-bot backtest-grid \
  --settings config/settings.grid.yaml \
  --env .env.grid \
  --symbol TQQQ \
  --timeframe 1Day \
  --start 2010-02-11 \
  --end 2026-06-17 \
  --grid-spacing-pct 5.0 \
  --json-output data/backtests/tqqq_grid_5pct.json

trading-bot sweep-grid \
  --settings config/settings.grid.yaml \
  --env .env.grid \
  --symbol TQQQ \
  --timeframe 1Day \
  --start 2010-02-11 \
  --end 2026-06-17 \
  --csv-output data/backtests/tqqq_grid_sweep.csv
```

## 10. Output Files

Recommended output paths:

```text
data/backtests/
  tqqq_grid_5pct_daily.json
  tqqq_grid_5pct_daily_trades.csv
  tqqq_grid_sweep_daily.csv
  tqqq_grid_sweep_daily.md
```

The JSON file should contain full details.

The CSV/Markdown files should be easy to read and compare.

## 11. Build Phases

### Phase 1: Pure Simulator With Fake Bars

Build the grid simulator with hand-made test bars.

Acceptance criteria:

- buy fills when low touches buy level
- sell fills when high touches sell target
- same-bar buy and sell is not allowed
- max inventory gate blocks extra buys
- cash reserve gate blocks extra buys
- metrics calculate correctly

### Phase 2: Daily Historical Backtest

Load daily TQQQ bars and run one backtest.

Acceptance criteria:

- `backtest-grid` command works
- JSON output includes trades and metrics
- test period can cover TQQQ inception through today
- results are deterministic

### Phase 3: Parameter Sweep

Run many spacing/order-size combinations.

Acceptance criteria:

- `sweep-grid` command works
- output table compares the main metrics
- `0.80%` and `5.0%` can be compared directly

### Phase 4: Intraday Backtest

Use 5-minute or 1-minute bars.

Acceptance criteria:

- same simulator can run on intraday bars
- cache prevents repeated downloads
- intraday results can be compared with daily results

### Phase 5: Paper Trading Readiness Review

Before any paper grid bot runs, review:

- best spacing candidates
- worst drawdown periods
- inventory blowup behavior
- whether risk gates are too loose
- whether the bot should allow overnight inventory

## 12. First Implementation Recommendation

Start with this order:

1. Build simulator and tests using fake bars.
2. Add daily TQQQ data loading.
3. Run spacing sweep for `0.80%`, `2%`, `3%`, `5%`, and `8%`.
4. Use the result to choose the first paper-trading setting.
5. Only then build the actual paper grid scheduler.

My current hypothesis:

```text
0.80% will trade too often for TQQQ.
5.0% will be calmer and easier to control.
Backtest before trusting either.
```

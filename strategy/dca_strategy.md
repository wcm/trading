# Recurring Investment Strategy

## Purpose

This bot invests a configured dollar amount into one asset on a regular
schedule. It is deterministic and does not use an LLM.

The selected paper configuration uses:

```yaml
dca_strategy:
  symbol: TQQQ
  frequency: biweekly
  base_contribution: 250
  day_of_month: 1
  biweekly_anchor_date: 2026-07-20

dca_sizing:
  mode: drawdown_scaled
  drawdown_lookback_days: 365
  drawdown_scale_factor: 4
  max_contribution_multiplier: 5

dca_risk:
  max_contribution_per_purchase: 1250
  max_annual_contribution: null
```

The DCA bot has its own Alpaca paper account, Discord webhook, config, state,
database, logs, and Compose service.

## Current Rule

Every 14 calendar days, invest at least `$250` in `TQQQ` using an Alpaca paper
notional market order during regular market hours. If TQQQ is below its highest
close in the trailing 365 calendar days, increase the contribution using scale
factor `4`.

If a scheduled day is a weekend or market holiday, the bot buys on the first
open market day afterward. The state file records the period key, so repeated
scheduler checks cannot intentionally buy the same period twice.

The market order is intentional for this strategy: a notional order supports
an exact dollar contribution and fractional shares. The bot still requires
both `--submit-paper` and `execution.enable_paper_orders: true`.

## Drawdown Sizing

The same sizing function supports `drawdown_scaled` contributions:

```text
amount = base amount * (1 + scale factor * drawdown from trailing peak)
```

With the selected `$250` base and scale factor `4`, a `10%` drawdown invests
`$350`. The multiplier is capped at `5x`, and the matching per-purchase limit is
`$1,250`. The annual limit remains off so it cannot skip scheduled purchases.
The default trailing peak is the highest close in the previous 365 calendar
days, in both the bot and the backtester.

Scale `4` has a theoretical maximum contribution of `5x` the base if TQQQ
approaches a 100% drawdown, so these limits match the formula instead of
restricting its normal behavior. In the 2020-2026 backtest, the largest
contribution was about `4.23x` the base. TQQQ still experienced an `81.75%`
contribution-adjusted drawdown, so this remains a paper-only strategy.

The selected runtime mode is now `drawdown_scaled`. Paper execution remains
locked until the preview cycle has been reviewed.

## Backtest

Fixed monthly comparison:

```bash
uv run trading-bot \
  --settings config/settings.dca.yaml \
  --env .env.dca \
  backtest-dca \
  --symbol TQQQ \
  --start 2015-01-01 \
  --end 2026-07-17 \
  --frequency monthly \
  --contribution-amount 500 \
  --sizing-mode fixed \
  --json-output data/dca/backtest_fixed.json \
  --purchases-csv data/dca/backtest_fixed_purchases.csv
```

Selected biweekly drawdown-scaled strategy:

```bash
uv run trading-bot \
  --settings config/settings.dca.yaml \
  --env .env.dca \
  backtest-dca \
  --symbol TQQQ \
  --start 2020-01-01 \
  --end 2026-07-17 \
  --frequency biweekly \
  --biweekly-anchor-date 2020-01-02 \
  --contribution-amount 250 \
  --sizing-mode drawdown_scaled \
  --drawdown-lookback-days 365 \
  --drawdown-scale-factor 4 \
  --max-contribution-multiplier 5 \
  --max-contribution-per-purchase 1250 \
  --max-annual-contribution off \
  --json-output data/dca/backtest_drawdown.json \
  --purchases-csv data/dca/backtest_drawdown_purchases.csv
```

The result reports total contributions, shares accumulated, average cost,
final value, investment gain, simple return on contributed money,
contribution-adjusted portfolio drawdown, worst unrealized loss, and every
simulated purchase. It does not yet model dividends, taxes, or slippage.

## Local Paper Preview

Create the local secrets file:

```bash
cp config/secrets.dca.example.env .env.dca
```

Run one safe preview:

```bash
uv run trading-bot \
  --settings config/settings.dca.yaml \
  --env .env.dca \
  dca-cycle \
  --send-discord \
  --json-output data/dca/dca_cycle_preview.json
```

No order is submitted without `--submit-paper`. Even with that flag, the
current config refuses execution because `execution.enable_paper_orders` is
`false`.

## Continuous Paper Scheduler

After reviewing previews, explicitly set:

```yaml
execution:
  enable_paper_orders: true
```

Then run:

```bash
uv run trading-bot \
  --settings config/settings.dca.yaml \
  --env .env.dca \
  dca-schedule-local \
  --submit-paper \
  --send-discord \
  --json-output-dir data/dca/cycles
```

The scheduler sleeps while the US market is closed. During market hours it
checks once per hour, reconciles any submitted purchase, and does nothing after
the current contribution period has been recorded.

## Separate Container

```bash
docker compose -f docker-compose.dca.yml build
docker compose -f docker-compose.dca.yml run --rm dca-bot \
  uv run --no-sync trading-bot \
  --settings config/settings.dca.yaml \
  smoke --check-alpaca --send-discord
docker compose -f docker-compose.dca.yml up -d
docker compose -f docker-compose.dca.yml logs -f dca-bot
```

Only one DCA scheduler should use the dedicated paper account at a time.

# Trading Bot

Cloud-paper, paper-first options trading bot experiment.

Start here:

- [automatic_trading_bot_plan.md](automatic_trading_bot_plan.md): strategy, current state, roadmap, and known gaps.
- [automatic_trading_bot_runbook.md](automatic_trading_bot_runbook.md): beginner-friendly operations commands, cloud access, deploy workflow, logs, smoke tests, and emergency stop.
- `deploy/update-cloud.sh`: one-command cloud update after local changes are committed and pushed.

Current status:

- Alpaca paper mode only; live trading is out of scope for now.
- Normal scheduler runs on the DigitalOcean VPS through Docker Compose.
- Local scheduler should stay stopped while the cloud scheduler is running.
- Paper open/close execution is protected by both config locks and CLI flags; the current cloud paper experiment deliberately enables both in paper mode.

The bot cycle:

- load config from `config/settings.yaml`
- load secrets from `.env`
- initialize logs and SQLite storage
- check the kill switch
- monitor existing option positions first
- skip new opens if a spread should be closed
- otherwise run independent watchlist decisions
- optionally send Discord summaries

Paper order submission exists for allocator-selected opens, but it requires both
the CLI flag and config lock to be enabled.

## Common Operations

Update the cloud bot after committing and pushing local changes:

```bash
deploy/update-cloud.sh
```

Watch cloud logs:

```bash
ssh trading-bot-vps
cd /opt/trading
docker compose logs -f trading-bot
```

Emergency stop:

```bash
ssh trading-bot-vps
cd /opt/trading
docker compose down
```

See [automatic_trading_bot_runbook.md](automatic_trading_bot_runbook.md) for the
full command list.

## Code Organization

- `trading_bot/main.py`: thin CLI dispatcher and temporary compatibility re-exports.
- `trading_bot/cli/parser.py`: command-line parser.
- `trading_bot/app.py`: config, logging, SQLite, kill switch, and notifier bootstrap.
- `trading_bot/commands/`: command wrappers for smoke, scans, decisions, and position monitoring.
- `trading_bot/cycles/`: monitor-before-open run-cycle and watchlist decision orchestration.
- `trading_bot/execution/`: Alpaca MLeg previews, execution gates, pre-submit revalidation, and entry order management.
- `trading_bot/scheduler/`: local split-cadence scheduler.
- `trading_bot/summaries/`: daily trading summary construction.
- `trading_bot/notifications/messages.py`: Discord message formatting and chunking.
- `trading_bot/orders/lifecycle.py`: order status polling and lifecycle change recording.
- `trading_bot/utils/`: small shared helpers for artifacts, money, market time, and symbols.

## Setup

```bash
cp config/secrets.example.env .env
uv sync
```

Fill `.env` with:

- `ALPACA_API_KEY_ID`
- `ALPACA_API_SECRET_KEY`
- `DISCORD_WEBHOOK_URL`
- `OPENAI_API_KEY`

## Smoke Tests

Run config/logging/storage checks:

```bash
uv run trading-bot smoke
```

Send a Discord heartbeat:

```bash
uv run trading-bot smoke --send-discord
```

Check Alpaca paper account connectivity:

```bash
uv run trading-bot smoke --check-alpaca
```

Run a read-only QQQ put credit spread scan:

```bash
uv run trading-bot scan-options --symbols QQQ --max-candidates 5 --send-discord
```

Paper mode defaults to Alpaca's `indicative` option data feed because OPRA requires a signed agreement/subscription. Live mode should use OPRA before real option execution.

Run a read-only LLM decision from live paper-market candidates:

```bash
uv run trading-bot decide --symbols QQQ --max-candidates 2 --send-discord
```

Run independent per-symbol decisions across the watchlist and let the deterministic allocator pick the best accepted open:

```bash
uv run trading-bot decide-watchlist --max-candidates 20 --send-discord --json-output data/last_decision_watchlist.json
```

Discord sends one compact watchlist summary plus one full-detail message for
each symbol decision. Watchlist decisions are parallelized up to
`decision_engine.max_concurrent_symbols`; the default is 8. Alpaca requests use
configured timeouts/retries so transient data-call timeouts do not immediately
drop a symbol from the cycle.

Accepted `open` decisions include a read-only Alpaca MLeg order preview in the JSON artifact. The preview contains the `/v2/orders` payload.

Paper order submission has two locks and is disabled by default:

```bash
uv run trading-bot decide-watchlist --max-candidates 20 --submit-paper --send-discord
```

The command above still refuses to submit unless `execution.enable_paper_orders: true` is set in `config/settings.yaml`.
When paper submission is enabled, the bot refreshes all accepted open spread
quotes before allocation, filters out accepted opens whose fresh credit no
longer meets execution rules, refreshes the selected spread again immediately
before submitting, recalculates the credit and max loss, and keeps the order as
a bounded limit order. Stale unfilled entries are polled, canceled, and
optionally replaced with a slightly more aggressive credit until
`execution.max_entry_price_adjustments` is reached. Market orders remain
disabled.

Monitor existing paper option positions and generate read-only close previews:

```bash
uv run trading-bot monitor-positions --send-discord --json-output data/last_position_monitor.json
```

Paper close submission has its own two locks and is disabled by default:

```bash
uv run trading-bot monitor-positions --submit-paper-close --send-discord
```

The command above still refuses to submit close orders unless
`execution.enable_paper_close_orders: true` is set in `config/settings.yaml`.
Blocked or submitted close attempts are logged to SQLite `execution_attempts`.
`run-cycle` and `schedule-local` also accept `--submit-paper-close`.

Poll recent Alpaca orders and record lifecycle changes:

```bash
uv run trading-bot poll-orders --status all --limit 50 --send-discord --json-output data/last_order_poll.json
```

Repeated polls only notify when an order status or filled quantity changes,
unless `--notify-no-changes` is provided. Large lifecycle updates are split
across Discord messages instead of truncating changed orders.

Run one full local bot cycle. This monitors existing positions first, skips new
open decisions when any spread has a close recommendation, and otherwise runs
the watchlist decision/allocation path:

```bash
uv run trading-bot run-cycle --max-candidates 20 --send-discord --json-output data/last_run_cycle.json
```

`run-cycle` uses `runtime.cycle_lock_path` so overlapping scheduled cycles
refuse to start.

Test the same cycle without calling OpenAI:

```bash
uv run trading-bot run-cycle --symbols AAPL,MSFT --max-candidates 3 --mock-decision skip
```

Build a trading-focused daily summary:

```bash
uv run trading-bot daily-summary --send-discord --json-output data/daily_summary.json
```

Run the local scheduler during US market hours. It defaults to one scheduler
check every 1 minute, one new-open decision cycle every 5 minutes, and one
heartbeat every 60 minutes:

```bash
uv run trading-bot schedule-local --send-discord --json-output-dir data/scheduler_cycles
```

Validate one scheduler check safely without OpenAI:

```bash
uv run trading-bot schedule-local --symbols AAPL --max-candidates 1 --mock-decision skip --send-discord --json-output-dir data/scheduler_cycles --once
```

Use `--send-cycle-discord` only when you want every scheduled cycle to also send
the full run-cycle decision summary.
Add `--cycle-summary-only` when you want only the compact run-cycle/open-discovery
summary without per-symbol detail messages.
The scheduler also polls recent Alpaca order statuses after each check unless
`--skip-order-poll` is used.
When positions are open, the scheduler runs monitor-only supervision on the
1-minute tick; new open decisions run on the slower open interval. The
after-market daily summary is sent at `runtime.scheduler_daily_summary_time_et`
unless `--skip-daily-summary` is used. When Alpaca reports that the market is
closed, the scheduler sleeps until the daily summary is due, then sleeps until
Alpaca's next market open.

Run the paper scheduler with open and close submissions enabled:

```bash
uv run trading-bot schedule-local --send-discord --send-cycle-discord --cycle-summary-only --json-output-dir data/scheduler_cycles --submit-paper --submit-paper-close
```

New opens are mechanically blocked by account/risk gates before symbol discovery
and LLM decision calls:

- `risk.max_open_risk` as aggregate projected put-spread max loss
- `risk.max_new_trades_per_day`
- `risk.max_daily_loss`
- `risk.max_weekly_loss` based on bot-observed account snapshots
- `account.emergency_equity_floor`

Filled Alpaca MLeg open/close orders are persisted to SQLite `spread_trades`
when `poll-orders`, `run-cycle`, or `schedule-local` observes lifecycle changes.

## Cloud Worker

The first cloud path is a Docker Compose worker on a small VPS:

```bash
docker compose build
docker compose run --rm trading-bot uv run --no-sync trading-bot smoke --check-alpaca --send-discord
docker compose up -d
docker compose logs -f trading-bot
```

See `deploy/cloud-vps.md` for setup and operations.

The decision packet includes account/position/order state, option candidates,
intraday move, 30-minute moving-average trend context, option quote freshness,
and recent Alpaca/Benzinga news.

`decision_engine.reasoning_effort` controls GPT reasoning effort. The default is
`medium`; set it to `high` only when the higher output-token cost is worth it.

Test the same decision path without calling OpenAI:

```bash
uv run trading-bot decide --symbols QQQ --max-candidates 2 --mock-decision skip
uv run trading-bot decide-watchlist --symbols AAPL,MSFT --max-candidates 3 --mock-decision skip
```

Run unit tests:

```bash
uv run python -m unittest discover -s tests
```

Compile-check the package:

```bash
uv run python -m compileall src tests
```

The next milestone is reducing repeated LLM cost by running cheap monitoring
more often than expensive open-decision scans.

## First Local Checklist

1. Create `.env` from `config/secrets.example.env`.
2. Add `DISCORD_WEBHOOK_URL`.
3. Run `uv run trading-bot smoke --send-discord`.
4. Add Alpaca paper API keys.
5. Run `uv run trading-bot smoke --check-alpaca`.
6. Keep `KILL_SWITCH` absent for read-only smoke tests; create it later to test execution blocking.

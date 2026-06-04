# Trading Bot

Local-first, paper-first options trading bot experiment.

The current milestone is one local paper-mode bot cycle:

- load config from `config/settings.yaml`
- load secrets from `.env`
- initialize logs and SQLite storage
- check the kill switch
- monitor existing option positions first
- skip new opens if a spread should be closed
- otherwise run independent watchlist decisions
- optionally send Discord summaries

Paper order submission exists for allocator-selected opens, but it is disabled
unless both the CLI flag and config lock are enabled.

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
each symbol decision.

Accepted `open` decisions include a read-only Alpaca MLeg order preview in the JSON artifact. The preview contains the `/v2/orders` payload.

Paper order submission has two locks and is disabled by default:

```bash
uv run trading-bot decide-watchlist --max-candidates 20 --submit-paper --send-discord
```

The command above still refuses to submit unless `execution.enable_paper_orders: true` is set in `config/settings.yaml`.

Monitor existing paper option positions and generate read-only close previews:

```bash
uv run trading-bot monitor-positions --send-discord --json-output data/last_position_monitor.json
```

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

The decision packet includes account/position/order state, option candidates,
intraday move, 30-minute moving-average trend context, option quote freshness,
and recent Alpaca/Benzinga news.

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

The next milestone is scheduling `run-cycle` locally every minute during market
hours, with Discord heartbeat/error notifications.

## First Local Checklist

1. Create `.env` from `config/secrets.example.env`.
2. Add `DISCORD_WEBHOOK_URL`.
3. Run `uv run trading-bot smoke --send-discord`.
4. Add Alpaca paper API keys.
5. Run `uv run trading-bot smoke --check-alpaca`.
6. Keep `KILL_SWITCH` absent for read-only smoke tests; create it later to test execution blocking.

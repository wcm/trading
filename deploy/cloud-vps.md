# Cloud VPS Deployment

This project runs as a background worker, not a web app. A small Ubuntu VPS with Docker Compose is the simplest first cloud target because it can keep SQLite, JSON artifacts, and logs on persistent disk.

## First Deploy

1. Install Docker and the Compose plugin on the VPS.
2. Clone or copy this repo to the VPS.
3. Create `.env` from `config/secrets.example.env` and fill in:
   - `ALPACA_API_KEY_ID`
   - `ALPACA_API_SECRET_KEY`
   - `DISCORD_WEBHOOK_URL`
   - `OPENAI_API_KEY`
   - `OPENAI_MODEL`
4. Review `config/settings.yaml`.
   - Keep `mode: paper`.
   - Keep Alpaca paper base URL.
   - Keep paper open/close locks enabled only while intentionally running paper trading.
5. Build and smoke-test:

```bash
docker compose build
docker compose run --rm trading-bot uv run --no-sync trading-bot smoke --check-alpaca --send-discord
```

6. Start the scheduler:

```bash
docker compose up -d
docker compose logs -f trading-bot
```

The container runs:

```bash
uv run --no-sync trading-bot schedule-local --send-discord --send-cycle-discord --cycle-summary-only --json-output-dir data/scheduler_cycles --submit-paper --submit-paper-close
```

The scheduler sleeps while the US market is closed, wakes for the after-market daily summary, then sleeps until Alpaca's next market open.

## Operations

Stop the bot:

```bash
docker compose down
```

Restart after pulling code or editing config:

```bash
docker compose up -d --build
```

Inspect logs:

```bash
docker compose logs -f trading-bot
tail -f logs/bot.log
```

Persistent state lives in:

- `data/trading_bot.sqlite3`
- `data/scheduler_cycles/`
- `logs/bot.log`

## Emergency Stop

The fastest cloud stop is:

```bash
docker compose down
```

For a process-level kill switch, create the configured kill-switch file in the container filesystem or run `docker compose down`. A later deployment pass should move `runtime.kill_switch_path` to a persisted `data/KILL_SWITCH` path if we want a durable file-based cloud kill switch.

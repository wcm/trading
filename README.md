# Trading Bot

Local-first, paper-first options trading bot experiment.

The current milestone is a local paper-mode smoke test:

- load config from `config/settings.yaml`
- load secrets from `.env`
- initialize logs and SQLite storage
- check the kill switch
- optionally send a Discord startup message
- optionally read Alpaca paper account/clock/positions

No order placement is implemented yet.

## Setup

```bash
cp config/secrets.example.env .env
uv sync
```

Fill `.env` with:

- `ALPACA_API_KEY_ID`
- `ALPACA_API_SECRET_KEY`
- `DISCORD_WEBHOOK_URL`
- `OPENAI_API_KEY` later, once the LLM decision step is wired

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

Test the same decision path without calling OpenAI:

```bash
uv run trading-bot decide --symbols QQQ --max-candidates 2 --mock-decision skip
```

Run unit tests:

```bash
uv run python -m unittest discover -s tests
```

Compile-check the package:

```bash
uv run python -m compileall src tests
```

The bot refuses to place orders at this stage. The next milestone is read-only option-chain scanning.

## First Local Checklist

1. Create `.env` from `config/secrets.example.env`.
2. Add `DISCORD_WEBHOOK_URL`.
3. Run `uv run trading-bot smoke --send-discord`.
4. Add Alpaca paper API keys.
5. Run `uv run trading-bot smoke --check-alpaca`.
6. Keep `KILL_SWITCH` absent for read-only smoke tests; create it later to test execution blocking.

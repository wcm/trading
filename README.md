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

Run independent per-symbol decisions across the watchlist and let the deterministic allocator pick the best accepted open:

```bash
uv run trading-bot decide-watchlist --max-candidates 20 --send-discord --json-output data/last_decision_watchlist.json
```

Accepted `open` decisions include a read-only Alpaca MLeg order preview in the JSON artifact. The preview contains the `/v2/orders` payload.

Paper order submission has two locks and is disabled by default:

```bash
uv run trading-bot decide-watchlist --max-candidates 20 --submit-paper --send-discord
```

The command above still refuses to submit unless `execution.enable_paper_orders: true` is set in `config/settings.yaml`.

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

The bot refuses to place orders at this stage. The next milestone is a read-only Alpaca multi-leg order preview for allocator-selected `open` decisions.

## First Local Checklist

1. Create `.env` from `config/secrets.example.env`.
2. Add `DISCORD_WEBHOOK_URL`.
3. Run `uv run trading-bot smoke --send-discord`.
4. Add Alpaca paper API keys.
5. Run `uv run trading-bot smoke --check-alpaca`.
6. Keep `KILL_SWITCH` absent for read-only smoke tests; create it later to test execution blocking.

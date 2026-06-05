# Automatic Trading Bot Operations Runbook

Last updated: 2026-06-05

This runbook explains how to operate the current paper-trading bot locally and
on the DigitalOcean VPS. Strategy, risk decisions, roadmap, and known gaps live
in [automatic_trading_bot_plan.md](automatic_trading_bot_plan.md).

## Read This First

Use this file when you forget what to type. The short mental model is:

- The bot code lives locally at `/Users/chenmuwu/Documents/Trading`.
- The cloud copy lives on the VPS at `/opt/trading`.
- The cloud scheduler is the normal running bot.
- Do not run a local scheduler while the cloud scheduler is running.
- Change code/config locally, test, commit, push, then run `deploy/update-cloud.sh`.
- Stop the bot immediately with `docker compose down` on the VPS.

Important files:

- `automatic_trading_bot_plan.md`: strategy, roadmap, known gaps.
- `automatic_trading_bot_runbook.md`: commands and operations.
- `config/settings.yaml`: trading/risk/scheduler config.
- `.env`: local secrets; never commit this.
- `/opt/trading/.env`: cloud secrets on the VPS.

## First-Time Local Setup

If the repo is already on this Mac:

```bash
cd /Users/chenmuwu/Documents/Trading
```

If starting from a new Mac:

```bash
git clone https://github.com/wcm/trading.git
cd trading
cp config/secrets.example.env .env
uv sync
```

Fill `.env` with:

```text
ALPACA_API_KEY_ID
ALPACA_API_SECRET_KEY
DISCORD_WEBHOOK_URL
OPENAI_API_KEY
```

Run local checks:

```bash
uv run python -m unittest
uv run trading-bot smoke --check-alpaca --send-discord
```

Set up a convenient SSH alias on your Mac:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
nano ~/.ssh/config
```

Add this, replacing `DROPLET_IP` with the public IPv4 shown in the
DigitalOcean dashboard:

```sshconfig
Host trading-bot-vps
  HostName DROPLET_IP
  User root
  IdentityFile ~/.ssh/do_trading_bot
```

Then this should work:

```bash
ssh trading-bot-vps
```

## 1. Operating Rule

Only one scheduler should run against the Alpaca paper account at a time.

Current intended state:

- Cloud scheduler: running on the DigitalOcean VPS.
- Local scheduler: stopped.

Starting a local scheduler while the cloud scheduler is running can duplicate
open/close decisions against the same Alpaca account.

## 2. Cloud Access

`DROPLET_IP` is a placeholder for the current DigitalOcean Droplet public IP.
Find it in the DigitalOcean dashboard under Droplets. If you set the SSH alias
above, use `trading-bot-vps` instead of typing the IP.

SSH to the VPS:

```bash
ssh trading-bot-vps
```

Direct SSH without the alias:

```bash
ssh -i ~/.ssh/do_trading_bot root@DROPLET_IP
```

Cloud repo path:

```bash
cd /opt/trading
```

Check container status:

```bash
docker compose ps
```

Watch live logs:

```bash
docker compose logs -f trading-bot
```

Press `Ctrl+C` to stop watching logs. This does not stop the bot.

Other useful log commands:

```bash
docker compose logs --tail=100 trading-bot
tail -f logs/bot.log
```

Check current paper positions from the cloud without submitting orders:

```bash
cd /opt/trading
docker compose run --rm trading-bot uv run --no-sync trading-bot monitor-positions \
  --json-output data/manual_position_monitor.json
```

Poll recent orders from the cloud without Discord noise:

```bash
cd /opt/trading
docker compose run --rm trading-bot uv run --no-sync trading-bot poll-orders \
  --status all \
  --limit 50 \
  --json-output data/manual_order_poll.json
```

## 3. Start, Stop, Restart

Start or restart the bot:

```bash
cd /opt/trading
docker compose up -d
```

Rebuild and restart after code/config changes:

```bash
cd /opt/trading
docker compose up -d --build
```

Stop the bot:

```bash
cd /opt/trading
docker compose down
```

Emergency stop:

```bash
ssh -i ~/.ssh/do_trading_bot root@DROPLET_IP
cd /opt/trading
docker compose down
```

The file kill switch exists in code, but the current most reliable cloud
emergency stop is `docker compose down`. A durable mounted kill-switch path is
still a known gap.

## 4. Normal Cloud Update Workflow

Use this workflow for code changes and `config/settings.yaml` changes.
The update script deploys from GitHub. It does not deploy uncommitted local
files.

On the local Mac:

```bash
cd /Users/chenmuwu/Documents/Trading
uv run python -m unittest
git status --short
git add PATHS_YOU_CHANGED
git commit -m "Describe change"
git push origin main
deploy/update-cloud.sh
```

The script:

- checks your local git tree is clean and pushed to `origin/main`;
- SSHes to the VPS;
- pulls latest code in `/opt/trading`;
- rebuilds/restarts Docker Compose;
- prints container status and recent logs.

If you have not created the `trading-bot-vps` SSH alias:

```bash
deploy/update-cloud.sh --host root@DROPLET_IP
```

Useful variants:

```bash
deploy/update-cloud.sh --smoke
deploy/update-cloud.sh --follow-logs
deploy/update-cloud.sh --no-build
deploy/update-cloud.sh --tail-lines 300
```

For config-only changes, Docker rebuild is not strictly necessary because
`config/settings.yaml` is mounted into the container. This is enough if you are
working manually on the VPS:

```bash
cd /opt/trading
git pull
docker compose up -d
docker compose logs -f trading-bot
```

Using `--build` is still fine and keeps the workflow simple.

## 5. Secrets

Local secrets file:

```bash
/Users/chenmuwu/Documents/Trading/.env
```

Cloud secrets file:

```bash
/opt/trading/.env
```

Required variables:

```text
ALPACA_API_KEY_ID
ALPACA_API_SECRET_KEY
DISCORD_WEBHOOK_URL
OPENAI_API_KEY
```

Optional variable:

```text
OPENAI_MODEL
```

Use `OPENAI_MODEL` only when overriding `decision_engine.model` from
`config/settings.yaml`.

Never commit `.env`.

When updating cloud secrets:

```bash
scp -i ~/.ssh/do_trading_bot .env root@DROPLET_IP:/opt/trading/.env
ssh -i ~/.ssh/do_trading_bot root@DROPLET_IP 'chmod 600 /opt/trading/.env'
```

Then restart:

```bash
ssh -i ~/.ssh/do_trading_bot root@DROPLET_IP
cd /opt/trading
docker compose up -d
```

In Docker logs, this line is expected:

```text
Loaded 0 values from env file
```

Docker Compose injects `.env` values into the container environment rather than
mounting the file inside the container. The smoke test confirms whether the
values are usable.

## 6. Smoke Tests

Cloud Docker smoke test:

```bash
ssh -i ~/.ssh/do_trading_bot root@DROPLET_IP
cd /opt/trading
docker compose run --rm trading-bot uv run --no-sync trading-bot smoke --check-alpaca --send-discord
```

Expected:

- Discord smoke message sent.
- Alpaca account status is `ACTIVE`.
- Alpaca account/clock/positions can be read.
- Command exits successfully.

Local smoke test:

```bash
cd /Users/chenmuwu/Documents/Trading
uv run trading-bot smoke --check-alpaca --send-discord
```

## 7. Scheduler Behavior

Cloud scheduler command from `Dockerfile`:

```bash
uv run --no-sync trading-bot schedule-local \
  --send-discord \
  --send-cycle-discord \
  --cycle-summary-only \
  --json-output-dir data/scheduler_cycles \
  --submit-paper \
  --submit-paper-close
```

Current cadence:

- Scheduler tick: every 1 minute.
- New-open discovery: every 5 minutes.
- Position monitor: every minute when open positions exist.
- Order lifecycle poll: after each scheduler check.
- Daily summary: after market close, currently 16:05 ET.
- Off-hours behavior: sleeps until daily summary or Alpaca next market open.

The scheduler always monitors existing positions before considering new opens.
If risk gates block new opens, open discovery and LLM open decisions are skipped.

## 8. Local Commands

Run tests:

```bash
cd /Users/chenmuwu/Documents/Trading
uv run python -m unittest
```

Compile check:

```bash
uv run python -m compileall src tests
```

Run one safe mock scheduler check:

```bash
uv run trading-bot schedule-local \
  --symbols AAPL \
  --max-candidates 1 \
  --mock-decision skip \
  --send-discord \
  --json-output-dir data/scheduler_cycles \
  --once
```

Run one full cycle without order submission:

```bash
uv run trading-bot run-cycle \
  --max-candidates 20 \
  --send-discord \
  --json-output data/last_run_cycle.json
```

Run the local scheduler only when the cloud scheduler is stopped:

```bash
uv run trading-bot schedule-local \
  --send-discord \
  --send-cycle-discord \
  --cycle-summary-only \
  --json-output-dir data/scheduler_cycles \
  --submit-paper \
  --submit-paper-close
```

## 9. Order And Position Commands

Monitor positions:

```bash
uv run trading-bot monitor-positions \
  --send-discord \
  --json-output data/last_position_monitor.json
```

Monitor positions and allow paper close orders:

```bash
uv run trading-bot monitor-positions \
  --submit-paper-close \
  --send-discord \
  --json-output data/last_position_monitor.json
```

Poll order lifecycle:

```bash
uv run trading-bot poll-orders \
  --status all \
  --limit 50 \
  --send-discord \
  --json-output data/last_order_poll.json
```

Build daily summary:

```bash
uv run trading-bot daily-summary \
  --send-discord \
  --json-output data/daily_summary.json
```

## 10. Data And Logs

Local paths:

```text
data/trading_bot.sqlite3
data/scheduler_cycles/
logs/bot.log
```

Cloud paths:

```text
/opt/trading/data/trading_bot.sqlite3
/opt/trading/data/scheduler_cycles/
/opt/trading/logs/bot.log
```

The Docker Compose file mounts `./data` and `./logs`, so these survive container
restarts and image rebuilds.

Copy cloud logs or artifacts to the local Mac:

```bash
scp -i ~/.ssh/do_trading_bot root@DROPLET_IP:/opt/trading/logs/bot.log ./bot.cloud.log
scp -i ~/.ssh/do_trading_bot -r root@DROPLET_IP:/opt/trading/data/scheduler_cycles ./scheduler_cycles.cloud
```

## 11. First Cloud Market Session Checklist

Before market open:

- `docker compose ps` shows the bot is up.
- Discord startup heartbeat was received.
- `docker compose logs -f trading-bot` shows off-hours sleep until next open.
- Local scheduler is not running.

During market hours:

- Position monitor runs before open discovery.
- New-open discovery runs on the 5-minute cadence.
- Discord summaries arrive without truncating important content.
- Order lifecycle polling does not repeatedly spam unchanged orders.
- No duplicate orders appear in Alpaca.

After market close:

- Daily summary is sent.
- SQLite and logs contain the session artifacts.
- Any opened spreads are reviewed for entry price, max loss, P&L, and exit plan.

## 12. Troubleshooting

Container is not running:

```bash
cd /opt/trading
docker compose ps
docker compose logs --tail=100 trading-bot
docker compose up -d
```

Rebuild after code changes:

```bash
cd /opt/trading
git pull
docker compose up -d --build
```

Discord smoke fails:

- Check `DISCORD_WEBHOOK_URL` in `/opt/trading/.env`.
- Re-run the Docker smoke test.
- Do not enable new openings if Discord is unreliable.

Alpaca smoke fails:

- Check Alpaca paper API keys in `/opt/trading/.env`.
- Confirm `mode: paper` in `config/settings.yaml`.
- Confirm Alpaca account status in the Alpaca dashboard.

OpenAI/LLM decision fails:

- Check `OPENAI_API_KEY`.
- Check `decision_engine.model` in `config/settings.yaml` or `OPENAI_MODEL`.
- Use mock mode to separate scheduler/data problems from LLM problems.

Market is closed:

- This is normal off-hours.
- The scheduler should sleep until the next Alpaca market open or the daily summary time.

Options feed says indicative:

- This is expected in paper mode without OPRA.
- Live options trading should require OPRA or equivalent data.

## 13. New VPS Bootstrap

The current VPS is already bootstrapped. For a new Ubuntu VPS, first make sure
the host can clone the private GitHub repo. Use a read-only deploy key, GitHub
CLI auth, or a temporary copy method.

```bash
sudo apt-get update
sudo apt-get install -y git
git clone git@github.com:wcm/trading.git /opt/trading
cd /opt/trading
./deploy/bootstrap-ubuntu-docker.sh
cp config/secrets.example.env .env
chmod 600 .env
```

Fill `.env`, then:

```bash
docker compose build
docker compose run --rm trading-bot uv run --no-sync trading-bot smoke --check-alpaca --send-discord
docker compose up -d
docker compose logs -f trading-bot
```

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.config import load_config, load_env_file, resolve_path
from trading_bot.logging_config import configure_logging
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.storage.db import init_db, record_bot_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-bot")
    parser.add_argument(
        "--settings",
        default="config/settings.yaml",
        help="Path to settings YAML file.",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to local environment file.",
    )

    subparsers = parser.add_subparsers(dest="command")
    smoke = subparsers.add_parser("smoke", help="Run local paper-mode smoke checks.")
    smoke.add_argument(
        "--send-discord",
        action="store_true",
        help="Send a Discord startup test message.",
    )
    smoke.add_argument(
        "--check-alpaca",
        action="store_true",
        help="Read Alpaca paper account, clock, and positions.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "smoke"
        args.send_discord = False
        args.check_alpaca = False

    if args.command == "smoke":
        return run_smoke(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


def run_smoke(args: argparse.Namespace) -> int:
    loaded_env_count = load_env_file(args.env)
    config = load_config(args.settings)

    log_dir = resolve_path(config.get("runtime", "log_dir", default="logs"))
    logger = configure_logging(log_dir)
    logger.info("Starting local smoke test")
    logger.info("Loaded %s values from env file", loaded_env_count)
    logger.info("Mode=%s broker=%s", config.mode, config.broker)

    db_path = resolve_path(config.get("storage", "sqlite_path", default="data/trading_bot.sqlite3"))
    init_db(db_path)

    kill_switch_path = resolve_path(config.get("runtime", "kill_switch_path", default="KILL_SWITCH"))
    kill_switch = KillSwitch(kill_switch_path)
    if kill_switch.is_active():
        logger.warning("Kill switch is active at %s; execution must remain disabled", kill_switch.path)
    else:
        logger.info("Kill switch is not active")

    notifier = DiscordNotifier.from_config(config)
    discord_ok = _maybe_send_discord(args, notifier, logger)
    alpaca_ok = _maybe_check_alpaca(args, config, logger)

    status = "ok" if discord_ok and alpaca_ok else "failed"
    record_bot_run(
        db_path,
        started_at=datetime.now(UTC).isoformat(),
        mode=config.mode,
        status=status,
        details={
            "command": "smoke",
            "discord_requested": args.send_discord,
            "alpaca_requested": args.check_alpaca,
            "kill_switch_active": kill_switch.is_active(),
        },
    )

    if status == "ok":
        logger.info("Smoke test completed successfully")
        return 0

    logger.error("Smoke test completed with failures")
    return 1


def _maybe_send_discord(
    args: argparse.Namespace,
    notifier: DiscordNotifier,
    logger: logging.Logger,
) -> bool:
    if not args.send_discord:
        if notifier.is_configured:
            logger.info("Discord webhook is configured; use --send-discord to send a test message")
        else:
            logger.warning("Discord webhook is not configured yet")
        return True

    result = notifier.send(
        "Bot startup smoke test\n"
        "Mode: PAPER\n"
        "Broker: Alpaca\n"
        "Trading enabled: no\n"
        "Purpose: local connectivity check"
    )
    if result.ok:
        logger.info("Discord smoke message sent")
        return True

    logger.error("Discord smoke message failed: %s", result.error)
    return False


def _maybe_check_alpaca(args: argparse.Namespace, config, logger: logging.Logger) -> bool:
    if not args.check_alpaca:
        logger.info("Skipping Alpaca API check; use --check-alpaca to enable it")
        return True

    try:
        client = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return False

    try:
        account = client.get_account()
        clock = client.get_clock()
        positions = client.get_positions()
    except Exception as exc:  # noqa: BLE001 - report broker/API error cleanly in CLI
        logger.exception("Alpaca paper connectivity check failed: %s", exc)
        return False

    logger.info(
        "Alpaca account status=%s equity=%s buying_power=%s",
        account.get("status"),
        account.get("equity"),
        account.get("buying_power"),
    )
    logger.info(
        "Alpaca clock is_open=%s next_open=%s next_close=%s",
        clock.get("is_open"),
        clock.get("next_open"),
        clock.get("next_close"),
    )
    logger.info("Alpaca open positions: %s", len(positions))
    return True


if __name__ == "__main__":
    raise SystemExit(main())


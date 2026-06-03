from __future__ import annotations

import argparse
import logging
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.config import load_config, load_env_file, resolve_path
from trading_bot.logging_config import configure_logging
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.storage.db import init_db, record_bot_run, record_option_scan
from trading_bot.strategy.put_credit_spread import scan_put_credit_spreads


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

    scan = subparsers.add_parser("scan-options", help="Run a read-only put-spread candidate scan.")
    scan.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to strategy.preferred_symbols.",
    )
    scan.add_argument(
        "--max-candidates",
        type=int,
        default=20,
        help="Maximum candidates to log and store.",
    )
    scan.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this scan.",
    )
    scan.add_argument(
        "--send-discord",
        action="store_true",
        help="Send a Discord scan summary.",
    )
    scan.add_argument(
        "--json-output",
        help="Optional path to write the full scan result JSON.",
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
    if args.command == "scan-options":
        return run_option_scan(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


def bootstrap(args: argparse.Namespace) -> tuple[Any, logging.Logger, Path, KillSwitch, DiscordNotifier]:
    loaded_env_count = load_env_file(args.env)
    config = load_config(args.settings)

    log_dir = resolve_path(config.get("runtime", "log_dir", default="logs"))
    logger = configure_logging(log_dir)
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
    return config, logger, db_path, kill_switch, notifier


def run_smoke(args: argparse.Namespace) -> int:
    config, logger, db_path, kill_switch, notifier = bootstrap(args)
    logger.info("Starting local smoke test")
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


def run_option_scan(args: argparse.Namespace) -> int:
    config, logger, db_path, kill_switch, notifier = bootstrap(args)
    logger.info("Starting read-only put credit spread scan")
    if kill_switch.is_active():
        logger.info("Kill switch is active; continuing because scan-options is read-only")

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    symbols = _symbols_from_args_or_config(args, config)
    if not symbols:
        logger.error("No symbols configured for scan")
        return 1

    try:
        scan_result = scan_put_credit_spreads(
            config=config,
            alpaca=alpaca,
            symbols=symbols,
            max_candidates=args.max_candidates,
            option_feed=args.option_feed,
        )
    except Exception as exc:  # noqa: BLE001 - report broker/API errors cleanly in CLI
        logger.exception("Option scan failed: %s", exc)
        return 1

    scan_run_id = record_option_scan(db_path, mode=config.mode, scan_result=scan_result)
    logger.info(
        "Option scan %s complete: symbols=%s contracts=%s snapshots=%s candidates=%s feed=%s",
        scan_run_id,
        ",".join(scan_result.symbols),
        scan_result.contracts_seen,
        scan_result.snapshots_seen,
        len(scan_result.candidates),
        scan_result.feed,
    )
    for warning in scan_result.warnings:
        logger.warning("Scan warning: %s", warning)

    for candidate in scan_result.candidates[:10]:
        logger.info(
            "Candidate %s %s/%s exp=%s credit=%s max_loss=%s short_delta=%s",
            candidate.underlying_symbol,
            candidate.short_put_symbol,
            candidate.long_put_symbol,
            candidate.expiration_date,
            candidate.net_credit,
            candidate.max_loss,
            candidate.short_delta,
        )

    if args.json_output:
        output_path = resolve_path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(scan_result.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        logger.info("Wrote scan JSON to %s", output_path)

    if args.send_discord:
        discord_ok = _send_scan_summary(notifier, scan_result, logger)
        if not discord_ok:
            return 1

    return 0


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


def _symbols_from_args_or_config(args: argparse.Namespace, config) -> list[str]:
    if args.symbols:
        raw_symbols = args.symbols.split(",")
    else:
        raw_symbols = config.get("strategy", "preferred_symbols", default=[])
    return [str(symbol).strip().upper() for symbol in raw_symbols if str(symbol).strip()]


def _send_scan_summary(notifier: DiscordNotifier, scan_result, logger: logging.Logger) -> bool:
    top_lines = [
        f"- {candidate.underlying_symbol} {candidate.short_strike}/{candidate.long_strike}P "
        f"{candidate.expiration_date}: credit {candidate.net_credit}, max loss {candidate.max_loss}"
        for candidate in scan_result.candidates[:5]
    ]
    if not top_lines:
        top_lines = ["- No candidates passed filters."]

    content = (
        "Read-only option scan complete\n"
        f"Symbols: {', '.join(scan_result.symbols)}\n"
        f"Feed: {scan_result.feed}\n"
        f"Contracts: {scan_result.contracts_seen}\n"
        f"Snapshots: {scan_result.snapshots_seen}\n"
        f"Candidates: {len(scan_result.candidates)}\n"
        + "\n".join(top_lines)
    )
    result = notifier.send(content)
    if result.ok:
        logger.info("Discord scan summary sent")
        return True

    logger.error("Discord scan summary failed: %s", result.error)
    return False


if __name__ == "__main__":
    raise SystemExit(main())

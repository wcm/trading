from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime

from trading_bot.app import bootstrap
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.cycles.open_discovery import (
    _build_decision_artifact,
    _build_watchlist_decision_run,
    _log_position_monitor_result,
    _maybe_execute_recommended_closes,
)
from trading_bot.llm.openai_client import OpenAIClientError
from trading_bot.monitoring.positions import monitor_put_credit_spreads
from trading_bot.notifications.messages import (
    _maybe_send_discord,
    _send_decision_summary,
    _send_position_monitor_summary,
    _send_scan_summary,
    _send_watchlist_decision_summary,
)
from trading_bot.storage.db import record_bot_run, record_option_scan
from trading_bot.strategy.put_credit_spread import scan_put_credit_spreads
from trading_bot.utils.artifacts import write_json_artifact
from trading_bot.utils.symbols import symbols_from_args_or_config, watchlist_symbols_from_args_or_config


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

    symbols = symbols_from_args_or_config(args, config)
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
        write_json_artifact(args.json_output, scan_result.to_dict(), logger, "scan")

    if args.send_discord:
        discord_ok = _send_scan_summary(notifier, scan_result, logger)
        if not discord_ok:
            return 1

    return 0


def run_decision(args: argparse.Namespace) -> int:
    config, logger, db_path, kill_switch, notifier = bootstrap(args)
    logger.info("Starting read-only LLM decision run")
    if kill_switch.is_active():
        logger.info("Kill switch is active; continuing because decide is read-only")

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    symbols = symbols_from_args_or_config(args, config)
    if not symbols:
        logger.error("No symbols configured for decision run")
        return 1

    try:
        artifact, scan_result = _build_decision_artifact(
            config=config,
            db_path=db_path,
            alpaca=alpaca,
            symbols=symbols,
            max_candidates=args.max_candidates,
            option_feed=args.option_feed,
            mock_decision=args.mock_decision,
        )
    except OpenAIClientError as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - report broker/API errors cleanly in CLI
        logger.exception("Decision run failed: %s", exc)
        return 1

    decision_id = artifact["decision_id"]
    validator_errors = artifact["validator_errors"]
    decision = artifact["decision"]
    if validator_errors:
        logger.warning("Decision %s rejected by validator: %s", decision_id, "; ".join(validator_errors))
    else:
        logger.info("Decision %s accepted by validator", decision_id)

    logger.info(
        "LLM decision action=%s symbol=%s candidate_id=%s confidence=%s reason=%s",
        decision.get("action"),
        decision.get("symbol"),
        decision.get("candidate_id"),
        decision.get("confidence"),
        decision.get("decision_reason"),
    )

    if args.json_output:
        write_json_artifact(args.json_output, artifact, logger, "decision")

    if args.send_discord:
        discord_ok = _send_decision_summary(
            notifier,
            decision,
            validator_errors,
            scan_result,
            artifact.get("order_preview"),
            logger,
        )
        if not discord_ok:
            return 1

    return 0


def run_watchlist_decisions(args: argparse.Namespace) -> int:
    config, logger, db_path, kill_switch, notifier = bootstrap(args)
    logger.info("Starting independent read-only watchlist decision run")
    if kill_switch.is_active():
        logger.info("Kill switch is active; continuing because decide-watchlist is read-only")

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    symbols = watchlist_symbols_from_args_or_config(args, config)
    if not symbols:
        logger.error("No symbols configured for watchlist decision run")
        return 1

    combined_artifact, successful_count = _build_watchlist_decision_run(
        config=config,
        logger=logger,
        db_path=db_path,
        kill_switch=kill_switch,
        notifier=notifier,
        alpaca=alpaca,
        symbols=symbols,
        max_candidates=args.max_candidates,
        option_feed=args.option_feed,
        mock_decision=args.mock_decision,
        submit_requested=args.submit_paper,
    )

    if args.json_output:
        write_json_artifact(args.json_output, combined_artifact, logger, "watchlist decision")

    if args.send_discord:
        discord_ok = _send_watchlist_decision_summary(notifier, combined_artifact, logger)
        if not discord_ok:
            return 1

    return 0 if successful_count else 1


def run_position_monitor(args: argparse.Namespace) -> int:
    config, logger, db_path, kill_switch, notifier = bootstrap(args)
    logger.info("Starting position monitor")
    if kill_switch.is_active():
        logger.info("Kill switch is active; close execution remains blocked")

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    try:
        result = monitor_put_credit_spreads(
            config=config,
            alpaca=alpaca,
            option_feed=args.option_feed,
        )
    except Exception as exc:  # noqa: BLE001 - report broker/API errors cleanly in CLI
        logger.exception("Position monitor failed: %s", exc)
        return 1

    _log_position_monitor_result(logger, result)

    artifact = result.to_dict()
    close_execution_attempts = _maybe_execute_recommended_closes(
        config=config,
        logger=logger,
        db_path=db_path,
        alpaca=alpaca,
        kill_switch=kill_switch,
        notifier=notifier,
        monitor_artifact=artifact,
        submit_requested=args.submit_paper_close,
    )
    artifact["close_execution_attempts"] = close_execution_attempts
    if args.json_output:
        write_json_artifact(args.json_output, artifact, logger, "position monitor")

    if args.send_discord:
        discord_ok = _send_position_monitor_summary(notifier, artifact, logger)
        if not discord_ok:
            return 1

    return 0


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

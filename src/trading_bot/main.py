from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from trading_bot.allocation import build_allocation_summary
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.config import load_config, load_env_file, resolve_path
from trading_bot.data.events import build_event_context
from trading_bot.data.market_data import build_market_context
from trading_bot.data.news import build_news_context
from trading_bot.execution.gate import maybe_submit_paper_order
from trading_bot.execution.orders import build_client_order_id, build_put_credit_spread_order_preview
from trading_bot.llm.decision import build_decision_packet, candidate_dicts_by_id, packet_candidate_ids
from trading_bot.llm.openai_client import OpenAIClient, OpenAIClientError
from trading_bot.llm.schemas import validate_decision_payload
from trading_bot.logging_config import configure_logging
from trading_bot.monitoring.positions import monitor_put_credit_spreads
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.storage.db import (
    init_db,
    record_bot_run,
    record_execution_attempt,
    record_llm_decision,
    record_option_scan,
)
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

    decide = subparsers.add_parser("decide", help="Run read-only scan plus LLM decision.")
    decide.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to strategy.preferred_symbols.",
    )
    decide.add_argument(
        "--max-candidates",
        type=int,
        default=5,
        help="Maximum candidates to pass to the LLM.",
    )
    decide.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this decision.",
    )
    decide.add_argument(
        "--send-discord",
        action="store_true",
        help="Send a Discord LLM decision summary.",
    )
    decide.add_argument(
        "--json-output",
        help="Optional path to write the full decision artifact JSON.",
    )
    decide.add_argument(
        "--mock-decision",
        choices=["skip", "disable_trading"],
        help="Use a local mock decision instead of calling OpenAI.",
    )

    decide_watchlist = subparsers.add_parser(
        "decide-watchlist",
        help="Run independent read-only LLM decisions for each watchlist symbol.",
    )
    decide_watchlist.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to strategy.watchlist.",
    )
    decide_watchlist.add_argument(
        "--max-candidates",
        type=int,
        default=20,
        help="Maximum candidates to pass to each per-symbol LLM decision.",
    )
    decide_watchlist.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this decision run.",
    )
    decide_watchlist.add_argument(
        "--send-discord",
        action="store_true",
        help="Send one Discord summary for the watchlist decision run.",
    )
    decide_watchlist.add_argument(
        "--json-output",
        help="Optional path to write the combined watchlist decision artifact JSON.",
    )
    decide_watchlist.add_argument(
        "--mock-decision",
        choices=["skip", "disable_trading"],
        help="Use local mock decisions instead of calling OpenAI.",
    )
    decide_watchlist.add_argument(
        "--submit-paper",
        action="store_true",
        help="Request paper order submission for the allocator-selected open. Requires execution.enable_paper_orders=true.",
    )

    monitor = subparsers.add_parser(
        "monitor-positions",
        help="Run a read-only monitor for existing put credit spread positions.",
    )
    monitor.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this monitor run.",
    )
    monitor.add_argument(
        "--send-discord",
        action="store_true",
        help="Send a Discord position monitor summary.",
    )
    monitor.add_argument(
        "--json-output",
        help="Optional path to write the position monitor artifact JSON.",
    )

    cycle = subparsers.add_parser(
        "run-cycle",
        help="Run one monitor-before-open bot cycle.",
    )
    cycle.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to strategy.watchlist.",
    )
    cycle.add_argument(
        "--max-candidates",
        type=int,
        default=20,
        help="Maximum candidates to pass to each per-symbol LLM decision when opens are allowed.",
    )
    cycle.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this cycle.",
    )
    cycle.add_argument(
        "--send-discord",
        action="store_true",
        help="Send one Discord summary for the full cycle.",
    )
    cycle.add_argument(
        "--json-output",
        help="Optional path to write the combined cycle artifact JSON.",
    )
    cycle.add_argument(
        "--mock-decision",
        choices=["skip", "disable_trading"],
        help="Use local mock decisions instead of calling OpenAI when opens are allowed.",
    )
    cycle.add_argument(
        "--submit-paper",
        action="store_true",
        help="Request paper order submission for an allocator-selected open. Requires execution.enable_paper_orders=true.",
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
    if args.command == "decide":
        return run_decision(args)
    if args.command == "decide-watchlist":
        return run_watchlist_decisions(args)
    if args.command == "monitor-positions":
        return run_position_monitor(args)
    if args.command == "run-cycle":
        return run_cycle(args)

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

    symbols = _symbols_from_args_or_config(args, config)
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
        output_path = resolve_path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
        logger.info("Wrote decision JSON to %s", output_path)

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

    symbols = _watchlist_symbols_from_args_or_config(args, config)
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
        _write_json_artifact(args.json_output, combined_artifact, logger, "watchlist decision")

    if args.send_discord:
        discord_ok = _send_watchlist_decision_summary(notifier, combined_artifact, logger)
        if not discord_ok:
            return 1

    return 0 if successful_count else 1


def run_cycle(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC).isoformat()
    config, logger, db_path, kill_switch, notifier = bootstrap(args)
    logger.info("Starting unified monitor-before-open run cycle")

    lock_handle = _acquire_run_cycle_lock(config, logger)
    if lock_handle is None:
        record_bot_run(
            db_path,
            started_at=started_at,
            mode=config.mode,
            status="locked",
            details={"command": "run-cycle", "reason": "another cycle is already running"},
        )
        return 1

    try:
        return _run_cycle_with_lock(
            args=args,
            config=config,
            logger=logger,
            db_path=db_path,
            kill_switch=kill_switch,
            notifier=notifier,
            started_at=started_at,
        )
    finally:
        _release_run_cycle_lock(lock_handle, logger)


def _run_cycle_with_lock(
    *,
    args: argparse.Namespace,
    config,
    logger: logging.Logger,
    db_path: Path,
    kill_switch: KillSwitch,
    notifier: DiscordNotifier,
    started_at: str,
) -> int:
    if kill_switch.is_active():
        logger.warning("Kill switch is active; monitoring continues, open execution remains blocked")

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        record_bot_run(
            db_path,
            started_at=started_at,
            mode=config.mode,
            status="failed",
            details={"command": "run-cycle", "error": str(exc)},
        )
        return 1

    try:
        monitor_result = monitor_put_credit_spreads(
            config=config,
            alpaca=alpaca,
            option_feed=args.option_feed,
        )
    except Exception as exc:  # noqa: BLE001 - report broker/API errors cleanly in CLI
        logger.exception("Run cycle position monitor failed: %s", exc)
        record_bot_run(
            db_path,
            started_at=started_at,
            mode=config.mode,
            status="failed",
            details={"command": "run-cycle", "phase": "monitor", "error": str(exc)},
        )
        return 1

    _log_position_monitor_result(logger, monitor_result)
    monitor_artifact = monitor_result.to_dict()
    close_recommended_spreads = _close_recommended_spreads(monitor_artifact)
    watchlist_artifact = None
    successful_count = 0
    skipped_open_reason = None
    cycle_status = "ok"
    phase = "monitor_then_open"

    if close_recommended_spreads:
        phase = "monitor_close_alert"
        skipped_open_reason = (
            f"{len(close_recommended_spreads)} existing spread(s) have close_recommended=true"
        )
        cycle_status = "close_recommended"
        logger.warning("Skipping new open decisions: %s", skipped_open_reason)
    else:
        symbols = _watchlist_symbols_from_args_or_config(args, config)
        if not symbols:
            logger.error("No symbols configured for run cycle")
            cycle_status = "failed"
        else:
            watchlist_artifact, successful_count = _build_watchlist_decision_run(
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
            cycle_status = "ok" if successful_count else "failed"

    cycle_artifact = _build_run_cycle_artifact(
        config=config,
        phase=phase,
        monitor_artifact=monitor_artifact,
        close_recommended_spreads=close_recommended_spreads,
        watchlist_artifact=watchlist_artifact,
        skipped_open_reason=skipped_open_reason,
    )

    if args.json_output:
        _write_json_artifact(args.json_output, cycle_artifact, logger, "run cycle")

    discord_ok = True
    if args.send_discord:
        discord_ok = _send_run_cycle_summary(notifier, cycle_artifact, logger)

    final_status = cycle_status if discord_ok else "failed"
    record_bot_run(
        db_path,
        started_at=started_at,
        mode=config.mode,
        status=final_status,
        details={
            "command": "run-cycle",
            "phase": phase,
            "close_recommended_count": len(close_recommended_spreads),
            "watchlist_successful_decision_count": successful_count,
            "discord_requested": args.send_discord,
            "discord_ok": discord_ok,
        },
    )
    return 0 if final_status in {"ok", "close_recommended"} else 1


def _build_watchlist_decision_run(
    *,
    config,
    logger: logging.Logger,
    db_path: Path,
    kill_switch: KillSwitch,
    notifier: DiscordNotifier,
    alpaca: AlpacaClient,
    symbols: list[str],
    max_candidates: int,
    option_feed: str | None,
    mock_decision: str | None,
    submit_requested: bool,
) -> tuple[dict[str, Any], int]:
    per_symbol: list[dict[str, Any]] = []
    decision_artifacts: list[dict[str, Any]] = []
    for symbol in symbols:
        logger.info("Running independent decision for %s", symbol)
        try:
            artifact, scan_result = _build_decision_artifact(
                config=config,
                db_path=db_path,
                alpaca=alpaca,
                symbols=[symbol],
                max_candidates=max_candidates,
                option_feed=option_feed,
                mock_decision=mock_decision,
            )
        except OpenAIClientError as exc:
            logger.error("Decision failed for %s: %s", symbol, exc)
            per_symbol.append({"symbol": symbol, "error": str(exc), "accepted": False})
            continue
        except Exception as exc:  # noqa: BLE001 - preserve per-symbol progress
            logger.exception("Decision failed for %s: %s", symbol, exc)
            per_symbol.append({"symbol": symbol, "error": str(exc), "accepted": False})
            continue

        decision = artifact["decision"]
        validator_errors = artifact["validator_errors"]
        logger.info(
            "Symbol %s decision action=%s accepted=%s candidate_id=%s confidence=%s candidates=%s",
            symbol,
            decision.get("action"),
            artifact["accepted"],
            decision.get("candidate_id"),
            decision.get("confidence"),
            len(scan_result.candidates),
        )
        decision_artifacts.append(artifact)
        per_symbol.append(_compact_watchlist_artifact(symbol, artifact))

    successful_decisions = [item for item in per_symbol if item.get("decision")]
    allocation = build_allocation_summary(config, decision_artifacts)
    selected_order_preview = _selected_order_preview(decision_artifacts, allocation.get("selected_open"))
    execution_attempt = None
    execution_attempt_id = None
    if submit_requested or selected_order_preview:
        state_refresh_error = None
        try:
            final_open_orders = alpaca.get_orders(status="open")
            final_positions = alpaca.get_positions()
        except Exception as exc:  # noqa: BLE001 - final execution guard must fail closed
            logger.exception("Final execution state refresh failed: %s", exc)
            state_refresh_error = str(exc)
            final_open_orders = []
            final_positions = []
        execution_attempt = maybe_submit_paper_order(
            config=config,
            alpaca=alpaca,
            kill_switch=kill_switch,
            notifier=notifier,
            submit_requested=submit_requested,
            order_preview=selected_order_preview,
            open_orders=final_open_orders,
            open_positions=final_positions,
            allocation=allocation,
            state_refresh_error=state_refresh_error,
        )
        selected_decision_id = (allocation.get("selected_open") or {}).get("decision_id")
        execution_attempt_id = record_execution_attempt(
            db_path,
            created_at=datetime.now(UTC).isoformat(),
            mode=config.mode,
            decision_id=selected_decision_id if isinstance(selected_decision_id, int) else None,
            attempt=execution_attempt,
        )
        logger.info(
            "Paper execution gate status=%s submitted=%s attempt_id=%s block_reasons=%s",
            execution_attempt.status,
            execution_attempt.submitted,
            execution_attempt_id,
            "; ".join(execution_attempt.block_reasons) if execution_attempt.block_reasons else "-",
        )
    combined_artifact = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": config.mode,
        "symbols": symbols,
        "per_symbol": per_symbol,
        "allocation": allocation,
        "selected_order_preview": selected_order_preview,
        "execution_attempt_id": execution_attempt_id,
        "execution_attempt": execution_attempt.to_dict() if execution_attempt else None,
    }

    logger.info(
        "Watchlist decision complete: symbols=%s successful=%s accepted_opens=%s selected=%s",
        len(symbols),
        len(successful_decisions),
        allocation["accepted_open_count"],
        (allocation["selected_open"] or {}).get("candidate_id"),
    )
    return combined_artifact, len(successful_decisions)


def run_position_monitor(args: argparse.Namespace) -> int:
    config, logger, _db_path, kill_switch, notifier = bootstrap(args)
    logger.info("Starting read-only position monitor")
    if kill_switch.is_active():
        logger.info("Kill switch is active; continuing because monitor-positions is read-only")

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
    if args.json_output:
        _write_json_artifact(args.json_output, artifact, logger, "position monitor")

    if args.send_discord:
        discord_ok = _send_position_monitor_summary(notifier, artifact, logger)
        if not discord_ok:
            return 1

    return 0


def _write_json_artifact(
    output_path_value: str,
    artifact: dict[str, Any],
    logger: logging.Logger,
    label: str,
) -> None:
    output_path = resolve_path(output_path_value)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("Wrote %s JSON to %s", label, output_path)


def _log_position_monitor_result(logger: logging.Logger, result) -> None:
    logger.info(
        "Position monitor complete: option_positions=%s spreads=%s unpaired_legs=%s",
        result.option_position_count,
        result.spread_count,
        len(result.unpaired_legs),
    )
    for spread in result.spreads:
        logger.info(
            "Spread %s close_recommended=%s close_debit=%s pnl=%s flags=%s",
            spread.get("spread_id"),
            spread.get("close_recommended"),
            spread.get("close_debit"),
            spread.get("estimated_unrealized_pnl"),
            ",".join(flag for flag, active in spread.get("exit_flags", {}).items() if active) or "-",
        )


def _close_recommended_spreads(monitor_artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        spread
        for spread in monitor_artifact.get("spreads", [])
        if isinstance(spread, dict) and spread.get("close_recommended") is True
    ]


def _build_run_cycle_artifact(
    *,
    config,
    phase: str,
    monitor_artifact: dict[str, Any],
    close_recommended_spreads: list[dict[str, Any]],
    watchlist_artifact: dict[str, Any] | None,
    skipped_open_reason: str | None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": config.mode,
        "command": "run-cycle",
        "phase": phase,
        "monitor": monitor_artifact,
        "close_recommended_count": len(close_recommended_spreads),
        "close_recommended_spreads": close_recommended_spreads,
        "skipped_open_decisions": bool(skipped_open_reason),
        "skip_open_reason": skipped_open_reason,
        "watchlist_decision": watchlist_artifact,
    }


def _acquire_run_cycle_lock(config, logger: logging.Logger) -> TextIO | None:
    lock_path = resolve_path(config.get("runtime", "cycle_lock_path", default="data/run_cycle.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        existing = handle.read().strip()
        logger.error(
            "Another run-cycle already holds lock %s%s",
            lock_path,
            f" ({existing})" if existing else "",
        )
        handle.close()
        return None

    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} acquired_at={datetime.now(UTC).isoformat()}\n")
    handle.flush()
    logger.info("Acquired run-cycle lock at %s", lock_path)
    return handle


def _release_run_cycle_lock(handle: TextIO, logger: logging.Logger) -> None:
    lock_path = getattr(handle, "name", "run-cycle lock")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    logger.info("Released run-cycle lock at %s", lock_path)


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


def _watchlist_symbols_from_args_or_config(args: argparse.Namespace, config) -> list[str]:
    if args.symbols:
        raw_symbols = args.symbols.split(",")
    else:
        raw_symbols = config.get("strategy", "watchlist", default=[])
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


def _get_llm_or_mock_decision(
    mock_decision: str | None,
    config,
    packet_dict: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if mock_decision:
        decision = _mock_decision(mock_decision)
        return decision, {"mock": True, "decision": decision}, "mock"

    prompt_version = str(config.get("decision_engine", "prompt_version", default="put_credit_spread_v1"))
    prompt_text = _load_prompt_text(prompt_version)
    client = OpenAIClient.from_config(config)
    decision, raw_response = client.create_trading_decision(
        prompt_text=prompt_text,
        decision_packet=packet_dict,
    )
    return decision, raw_response, client.model


def _build_decision_artifact(
    *,
    config,
    db_path: Path,
    alpaca: AlpacaClient,
    symbols: list[str],
    max_candidates: int,
    option_feed: str | None,
    mock_decision: str | None,
) -> tuple[dict[str, Any], Any]:
    account = alpaca.get_account()
    clock = alpaca.get_clock()
    positions = alpaca.get_positions()
    open_orders = alpaca.get_orders(status="open")
    market_context = build_market_context(config=config, alpaca=alpaca, symbols=symbols)
    event_context = build_event_context(config=config, symbols=symbols)
    news_context = build_news_context(config=config, alpaca=alpaca, symbols=symbols)
    scan_result = scan_put_credit_spreads(
        config=config,
        alpaca=alpaca,
        symbols=symbols,
        max_candidates=max_candidates,
        option_feed=option_feed,
    )

    record_option_scan(db_path, mode=config.mode, scan_result=scan_result)
    packet = build_decision_packet(
        config=config,
        account=account,
        clock=clock,
        positions=positions,
        open_orders=open_orders,
        scan_result=scan_result,
        market_context=market_context,
        event_context=event_context,
        news_context=news_context,
    )
    packet_dict = packet.to_dict()
    decision, raw_response, model = _get_llm_or_mock_decision(mock_decision, config, packet_dict)
    validator_errors = validate_decision_payload(
        decision,
        candidate_ids=packet_candidate_ids(scan_result),
        candidates_by_id=candidate_dicts_by_id(scan_result),
        allowed_symbols=set(symbols),
        open_position_symbols={str(position.get("symbol")) for position in positions if position.get("symbol")},
        market_context_by_symbol=packet_dict["market_context"]["symbols"],
        event_context_by_symbol=packet_dict["event_context"]["symbols"],
        max_loss_per_trade=config.get("risk", "max_loss_per_trade"),
        max_option_quote_age_seconds=int(
            config.get("market_filters", "max_option_quote_age_minutes", default=30)
        )
        * 60,
    )
    decision_id = record_llm_decision(
        db_path,
        created_at=datetime.now(UTC).isoformat(),
        mode=config.mode,
        provider="mock" if mock_decision else "openai",
        model=model,
        prompt_version=str(config.get("decision_engine", "prompt_version", default="put_credit_spread_v1")),
        packet=packet_dict,
        response=decision,
        raw_response=raw_response,
        validator_errors=validator_errors,
    )
    order_preview = None
    if not validator_errors and decision.get("action") == "open":
        selected_candidate = _selected_candidate_from_packet(packet_dict, decision.get("candidate_id"))
        symbol = str(decision.get("symbol") or symbols[0])
        order_preview = build_put_credit_spread_order_preview(
            config=config,
            decision=decision,
            candidate=selected_candidate,
            client_order_id=build_client_order_id("preview", symbol, decision_id),
        )
    return (
        {
            "decision_id": decision_id,
            "accepted": not validator_errors,
            "validator_errors": validator_errors,
            "decision": decision,
            "order_preview": order_preview,
            "packet": packet_dict,
            "raw_response": raw_response,
        },
        scan_result,
    )


def _load_prompt_text(prompt_version: str) -> str:
    prompt_path = resolve_path(f"src/trading_bot/llm/prompt_versions/{prompt_version}.md")
    if not prompt_path.exists():
        raise OpenAIClientError(f"Prompt version not found: {prompt_version}")
    return prompt_path.read_text(encoding="utf-8")


def _mock_decision(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "symbol": None,
        "candidate_id": None,
        "quantity": 0,
        "limit_price": None,
        "confidence": 1.0,
        "decision_reason": f"Mock {action} decision for local validation.",
        "news_assessment": {
            "risk_level": "unknown",
            "sentiment": "unknown",
            "summary": "Mock decision; news was not evaluated.",
        },
        "risk_checklist": {
            "defined_risk": True,
            "within_max_loss": True,
            "liquidity_ok": True,
            "earnings_ok": True,
            "no_material_negative_news": False,
            "market_trend_ok": True,
        },
        "exit_plan": {
            "profit_take_credit_pct": 50,
            "loss_trigger": "2x initial credit or short put delta above 0.45",
            "close_before_expiry_days": 3,
        },
    }


def _send_decision_summary(
    notifier: DiscordNotifier,
    decision: dict[str, Any],
    validator_errors: list[str],
    scan_result,
    order_preview: dict[str, Any] | None,
    logger: logging.Logger,
) -> bool:
    status = "accepted" if not validator_errors else "rejected"
    errors = "\n".join(f"- {error}" for error in validator_errors) if validator_errors else "- none"
    content = (
        "Read-only LLM decision complete\n"
        f"Status: {status}\n"
        f"Symbols: {', '.join(scan_result.symbols)}\n"
        f"Candidates: {len(scan_result.candidates)}\n"
        f"Action: {decision.get('action')}\n"
        f"Symbol: {decision.get('symbol')}\n"
        f"Candidate: {decision.get('candidate_id')}\n"
        f"Confidence: {decision.get('confidence')}\n"
        f"Order preview: {_preview_status(order_preview)}\n"
        f"Reason: {decision.get('decision_reason')}\n"
        f"Validator errors:\n{errors}"
    )
    result = notifier.send(content)
    if result.ok:
        logger.info("Discord decision summary sent")
        return True

    logger.error("Discord decision summary failed: %s", result.error)
    return False


def _compact_watchlist_artifact(symbol: str, artifact: dict[str, Any]) -> dict[str, Any]:
    decision = artifact["decision"]
    packet = artifact["packet"]
    selected_candidate = _selected_candidate_from_packet(packet, decision.get("candidate_id"))
    return {
        "symbol": symbol,
        "decision_id": artifact["decision_id"],
        "accepted": artifact["accepted"],
        "validator_errors": artifact["validator_errors"],
        "decision": decision,
        "order_preview": artifact.get("order_preview"),
        "selected_candidate": selected_candidate,
        "candidate_count": len(packet.get("option_scan", {}).get("candidates", [])),
        "market_context": packet.get("market_context", {}).get("symbols", {}).get(symbol),
        "event_context": packet.get("event_context", {}).get("symbols", {}).get(symbol),
    }


def _selected_candidate_from_packet(packet: dict[str, Any], candidate_id: Any) -> dict[str, Any] | None:
    if not candidate_id:
        return None
    candidates = packet.get("option_scan", {}).get("candidates", [])
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("candidate_id") == candidate_id:
            return candidate
    return None


def _selected_order_preview(
    decision_artifacts: list[dict[str, Any]],
    selected_open: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not selected_open:
        return None
    selected_decision_id = selected_open.get("decision_id")
    for artifact in decision_artifacts:
        if artifact.get("decision_id") == selected_decision_id:
            preview = artifact.get("order_preview")
            return preview if isinstance(preview, dict) else None
    return None


def _send_watchlist_decision_summary(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
) -> bool:
    allocation = artifact["allocation"]
    selected = allocation.get("selected_open")
    selected_preview = artifact.get("selected_order_preview")
    execution_attempt = artifact.get("execution_attempt")
    selected_line = (
        f"{selected['symbol']} {selected['candidate_id']} limit {selected['limit_price']} "
        f"max_loss {selected['max_loss']}"
        if selected
        else "none"
    )
    symbol_lines = []
    for item in artifact["per_symbol"]:
        decision = item.get("decision") or {}
        action = decision.get("action", "error")
        reason = item.get("error") or decision.get("decision_reason", "")
        symbol_lines.append(
            f"- {item.get('symbol')}: {action} candidate={decision.get('candidate_id')} "
            f"confidence={decision.get('confidence')} reason={str(reason)[:160]}"
        )

    content = (
        "Read-only watchlist decision complete\n"
        f"Symbols: {', '.join(artifact['symbols'])}\n"
        f"Accepted opens: {allocation['accepted_open_count']}\n"
        f"Selected open: {selected_line}\n"
        f"Order preview: {_preview_status(selected_preview)}\n"
        f"Execution: {_execution_status(execution_attempt)}\n"
        + "\n".join(symbol_lines[:12])
    )
    result = notifier.send(content)
    if result.ok:
        logger.info("Discord watchlist decision summary sent")
        return True

    logger.error("Discord watchlist decision summary failed: %s", result.error)
    return False


def _send_run_cycle_summary(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
) -> bool:
    monitor = artifact.get("monitor") or {}
    close_spreads = artifact.get("close_recommended_spreads") or []
    lines = [
        "Bot run cycle complete",
        f"Phase: {artifact.get('phase')}",
        f"Option positions: {monitor.get('option_position_count')}",
        f"Spreads: {monitor.get('spread_count')}",
        f"Close recommendations: {artifact.get('close_recommended_count')}",
    ]

    if close_spreads:
        lines.append(f"Open decisions: skipped ({artifact.get('skip_open_reason')})")
        for spread in close_spreads[:5]:
            active_flags = [
                flag for flag, active in (spread.get("exit_flags") or {}).items() if active
            ]
            lines.append(
                f"- {spread.get('spread_id')}: close_debit={spread.get('close_debit')} "
                f"pnl={spread.get('estimated_unrealized_pnl')} "
                f"preview={_preview_status(spread.get('close_order_preview'))} "
                f"flags={','.join(active_flags) or '-'}"
            )
    else:
        watchlist = artifact.get("watchlist_decision") or {}
        allocation = watchlist.get("allocation") or {}
        selected = allocation.get("selected_open")
        selected_preview = watchlist.get("selected_order_preview")
        execution_attempt = watchlist.get("execution_attempt")
        selected_line = (
            f"{selected['symbol']} {selected['candidate_id']} limit {selected['limit_price']} "
            f"max_loss {selected['max_loss']}"
            if selected
            else "none"
        )
        lines.extend(
            [
                f"Open symbols: {', '.join(watchlist.get('symbols', []))}",
                f"Accepted opens: {allocation.get('accepted_open_count')}",
                f"Selected open: {selected_line}",
                f"Order preview: {_preview_status(selected_preview)}",
                f"Execution: {_execution_status(execution_attempt)}",
            ]
        )
        for item in (watchlist.get("per_symbol") or [])[:8]:
            decision = item.get("decision") or {}
            action = decision.get("action", "error")
            reason = item.get("error") or decision.get("decision_reason", "")
            lines.append(
                f"- {item.get('symbol')}: {action} candidate={decision.get('candidate_id')} "
                f"confidence={decision.get('confidence')} reason={str(reason)[:120]}"
            )

    result = notifier.send("\n".join(lines))
    if result.ok:
        logger.info("Discord run-cycle summary sent")
        return True

    logger.error("Discord run-cycle summary failed: %s", result.error)
    return False


def _preview_status(order_preview: dict[str, Any] | None) -> str:
    if not order_preview:
        return "none"
    errors = order_preview.get("errors")
    if errors:
        return f"errors={len(errors)}"
    return "ready"


def _execution_status(execution_attempt: dict[str, Any] | None) -> str:
    if not execution_attempt:
        return "not attempted"
    status = execution_attempt.get("status")
    submitted = execution_attempt.get("submitted")
    reasons = execution_attempt.get("block_reasons") or []
    if reasons:
        return f"{status}, submitted={submitted}, first_block={reasons[0]}"
    broker_error = execution_attempt.get("broker_error")
    if broker_error:
        return f"{status}, submitted={submitted}, broker_error={str(broker_error)[:120]}"
    return f"{status}, submitted={submitted}"


def _send_position_monitor_summary(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
) -> bool:
    spread_lines = []
    for spread in artifact.get("spreads", [])[:10]:
        active_flags = [
            flag for flag, active in (spread.get("exit_flags") or {}).items() if active
        ]
        spread_lines.append(
            f"- {spread.get('spread_id')}: close={spread.get('close_recommended')} "
            f"debit={spread.get('close_debit')} pnl={spread.get('estimated_unrealized_pnl')} "
            f"flags={','.join(active_flags) or '-'}"
        )
    if not spread_lines:
        spread_lines = ["- No put credit spreads detected."]

    content = (
        "Read-only position monitor complete\n"
        f"Option positions: {artifact.get('option_position_count')}\n"
        f"Spreads: {artifact.get('spread_count')}\n"
        f"Unpaired legs: {len(artifact.get('unpaired_legs', []))}\n"
        + "\n".join(spread_lines)
    )
    result = notifier.send(content)
    if result.ok:
        logger.info("Discord position monitor summary sent")
        return True

    logger.error("Discord position monitor summary failed: %s", result.error)
    return False


if __name__ == "__main__":
    raise SystemExit(main())

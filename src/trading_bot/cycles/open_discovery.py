from __future__ import annotations

import argparse
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from trading_bot.allocation import build_allocation_summary
from trading_bot.app import bootstrap
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.data.events import build_event_context
from trading_bot.data.market_data import build_market_context
from trading_bot.data.news import NewsContext, build_news_context
from trading_bot.execution.gate import maybe_submit_paper_close_order, maybe_submit_paper_order
from trading_bot.execution.orders import build_client_order_id, build_put_credit_spread_order_preview
from trading_bot.execution.revalidation import revalidate_put_credit_spread_entry_preview
from trading_bot.llm.decision import build_decision_packet, candidate_dicts_by_id, packet_candidate_ids
from trading_bot.llm.openai_client import OpenAIClient, OpenAIClientError
from trading_bot.llm.schemas import validate_decision_payload
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.notifications.messages import _send_open_discovery_summary
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.risk.account import build_account_risk_state
from trading_bot.storage.db import (
    record_bot_run,
    record_execution_attempt,
    record_llm_decision,
    record_option_scan,
)
from trading_bot.strategy.put_credit_spread import scan_put_credit_spreads
from trading_bot.config import resolve_path
from trading_bot.utils.artifacts import write_json_artifact
from trading_bot.utils.money import decimal_or_none as _decimal_or_none
from trading_bot.utils.symbols import watchlist_symbols_from_args_or_config


_DB_WRITE_LOCK = threading.Lock()


def run_open_discovery_cycle(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC).isoformat()
    config, logger, db_path, kill_switch, notifier = bootstrap(args)
    logger.info("Starting new-open discovery cycle")

    lock_handle = _acquire_bot_cycle_lock(config, logger)
    if lock_handle is None:
        record_bot_run(
            db_path,
            started_at=started_at,
            mode=config.mode,
            status="locked",
            details={"command": "open-discovery-cycle", "reason": "another cycle is already running"},
        )
        return 1

    try:
        return _run_open_discovery_cycle_with_lock(
            args=args,
            config=config,
            logger=logger,
            db_path=db_path,
            kill_switch=kill_switch,
            notifier=notifier,
            started_at=started_at,
        )
    finally:
        _release_bot_cycle_lock(lock_handle, logger)


def _run_open_discovery_cycle_with_lock(
    *,
    args: argparse.Namespace,
    config,
    logger: logging.Logger,
    db_path: Path,
    kill_switch: KillSwitch,
    notifier: DiscordNotifier,
    started_at: str,
    alpaca: AlpacaClient | None = None,
) -> int:
    if kill_switch.is_active():
        logger.warning("Kill switch is active; open execution remains blocked")

    if alpaca is None:
        try:
            alpaca = AlpacaClient.from_config(config)
        except AlpacaCredentialsError as exc:
            logger.error("%s", exc)
            record_bot_run(
                db_path,
                started_at=started_at,
                mode=config.mode,
                status="failed",
                details={"command": "open-discovery-cycle", "error": str(exc)},
            )
            return 1

    watchlist_artifact = None
    successful_count = 0
    skipped_open_reason = None
    account_risk_state = None
    cycle_status = "ok"
    phase = "open_discovery"

    account_risk_state, account_risk_error = _build_pre_open_account_risk_state(
        config=config,
        logger=logger,
        db_path=db_path,
        alpaca=alpaca,
    )
    if account_risk_error:
        phase = "open_account_risk_error"
        skipped_open_reason = f"Account risk gate unavailable: {account_risk_error}"
        cycle_status = "failed"
        logger.error("Skipping new open decisions: %s", skipped_open_reason)
    elif account_risk_state and account_risk_state.get("blocks_new_opens"):
        phase = "open_account_risk_block"
        skipped_open_reason = (
            "Account risk gate blocked new opens: "
            + "; ".join(str(reason) for reason in account_risk_state.get("block_reasons", []))
        )
        cycle_status = "open_risk_blocked"
        logger.warning("Skipping new open decisions: %s", skipped_open_reason)
    else:
        symbols = watchlist_symbols_from_args_or_config(args, config)
        if not symbols:
            phase = "open_no_symbols"
            skipped_open_reason = "No symbols configured for open discovery"
            cycle_status = "failed"
            logger.error("Skipping new open decisions: %s", skipped_open_reason)
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

    cycle_artifact = _build_open_discovery_cycle_artifact(
        config=config,
        phase=phase,
        watchlist_artifact=watchlist_artifact,
        skipped_open_reason=skipped_open_reason,
        account_risk_state=account_risk_state,
    )

    if args.json_output:
        write_json_artifact(args.json_output, cycle_artifact, logger, "open discovery cycle")

    discord_ok = True
    if args.send_discord:
        discord_ok = _send_open_discovery_summary(
            notifier,
            cycle_artifact,
            logger,
            include_decision_details=not bool(getattr(args, "discord_summary_only", False)),
        )

    final_status = cycle_status if discord_ok else "failed"
    record_bot_run(
        db_path,
        started_at=started_at,
        mode=config.mode,
        status=final_status,
        details={
            "command": "open-discovery-cycle",
            "phase": phase,
            "watchlist_successful_decision_count": successful_count,
            "account_risk_state": account_risk_state,
            "discord_requested": args.send_discord,
            "discord_ok": discord_ok,
        },
    )
    return 0 if final_status in {"ok", "open_risk_blocked"} else 1


def _build_pre_open_account_risk_state(
    *,
    config,
    logger: logging.Logger,
    db_path: Path,
    alpaca: AlpacaClient,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        account = alpaca.get_account()
        return (
            build_account_risk_state(
                config=config,
                db_path=db_path,
                account=account,
            ).to_dict(),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed when account risk cannot be checked
        logger.exception("Pre-open account risk check failed: %s", exc)
        return None, str(exc)


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
    symbol_entries: dict[str, dict[str, Any]] = {}
    symbol_results = _run_symbol_decisions(
        config=config,
        logger=logger,
        db_path=db_path,
        alpaca=alpaca,
        symbols=symbols,
        max_candidates=max_candidates,
        option_feed=option_feed,
        mock_decision=mock_decision,
    )

    for symbol in symbols:
        result = symbol_results.get(symbol)
        if result is None:
            symbol_entries[symbol] = {"symbol": symbol, "error": "Decision result missing", "accepted": False}
            continue
        error = result.get("error")
        if error:
            symbol_entries[symbol] = {"symbol": symbol, "error": error, "accepted": False}
            continue

        artifact = result["artifact"]
        scan_result = result["scan_result"]
        decision = artifact["decision"]
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
        symbol_entries[symbol] = {"artifact": artifact}

    open_revalidation = _revalidate_open_order_previews(
        config=config,
        logger=logger,
        alpaca=alpaca,
        decision_artifacts=decision_artifacts,
    )

    for symbol in symbols:
        entry = symbol_entries.get(symbol)
        if not entry:
            per_symbol.append({"symbol": symbol, "error": "Decision result missing", "accepted": False})
            continue
        artifact = entry.get("artifact")
        if isinstance(artifact, dict):
            per_symbol.append(_compact_watchlist_artifact(symbol, artifact))
        else:
            per_symbol.append(entry)

    successful_decisions = [item for item in per_symbol if item.get("decision")]
    llm_call_count = len([item for item in per_symbol if item.get("llm_called") is True])
    allocation = build_allocation_summary(config, decision_artifacts)
    selected_order_preview = _selected_order_preview(decision_artifacts, allocation.get("selected_open"))
    execution_attempt = None
    execution_attempt_id = None
    account_risk_state = None
    if submit_requested or selected_order_preview:
        state_refresh_error = None
        try:
            final_account = alpaca.get_account()
            final_open_orders = alpaca.get_orders(status="open")
            final_positions = alpaca.get_positions()
            account_risk_state = build_account_risk_state(
                config=config,
                db_path=db_path,
                account=final_account,
            ).to_dict()
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
            account_risk_state=account_risk_state,
            state_refresh_error=state_refresh_error,
        )
        if isinstance(execution_attempt.order_preview, dict):
            selected_order_preview = execution_attempt.order_preview
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
        "llm_call_count": llm_call_count,
        "open_revalidation": open_revalidation,
        "allocation": allocation,
        "selected_order_preview": selected_order_preview,
        "account_risk_state": account_risk_state,
        "execution_attempt_id": execution_attempt_id,
        "execution_attempt": execution_attempt.to_dict() if execution_attempt else None,
    }

    logger.info(
        "Watchlist decision complete: symbols=%s successful=%s llm_calls=%s accepted_opens=%s selected=%s",
        len(symbols),
        len(successful_decisions),
        llm_call_count,
        allocation["accepted_open_count"],
        (allocation["selected_open"] or {}).get("candidate_id"),
    )
    return combined_artifact, len(successful_decisions)


def _revalidate_open_order_previews(
    *,
    config,
    logger: logging.Logger,
    alpaca: AlpacaClient,
    decision_artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = {
        "enabled": bool(config.get("execution", "pre_submit_revalidate_quotes", default=True)),
        "checked_count": 0,
        "eligible_count": 0,
        "blocked_count": 0,
        "errors": [],
    }
    if summary["enabled"] is not True:
        return summary

    for artifact in decision_artifacts:
        decision = artifact.get("decision")
        order_preview = artifact.get("order_preview")
        if not isinstance(decision, dict) or decision.get("action") != "open":
            continue
        if not artifact.get("accepted") or not isinstance(order_preview, dict):
            continue

        summary["checked_count"] += 1
        try:
            refreshed_preview = revalidate_put_credit_spread_entry_preview(
                config=config,
                alpaca=alpaca,
                order_preview=order_preview,
                adjustment_index=0,
            )
        except Exception as exc:  # noqa: BLE001 - mark candidate execution-ineligible and keep going
            refreshed_preview = dict(order_preview)
            refreshed_preview.setdefault("errors", []).append(f"Revalidation failed: {exc}")
            refreshed_preview["revalidation"] = {
                "kind": "put_credit_spread_entry_revalidation",
                "ok": False,
                "errors": [str(exc)],
                "warnings": [],
            }
            summary["errors"].append(str(exc))

        artifact["order_preview"] = refreshed_preview
        revalidation = refreshed_preview.get("revalidation") if isinstance(refreshed_preview, dict) else None
        if isinstance(revalidation, dict) and revalidation.get("ok") is True and not refreshed_preview.get("errors"):
            summary["eligible_count"] += 1
        else:
            summary["blocked_count"] += 1

    if summary["checked_count"]:
        logger.info(
            "Fresh quote revalidation complete: checked=%s eligible=%s blocked=%s",
            summary["checked_count"],
            summary["eligible_count"],
            summary["blocked_count"],
        )
    return summary


def _run_symbol_decisions(
    *,
    config,
    logger: logging.Logger,
    db_path: Path,
    alpaca: AlpacaClient,
    symbols: list[str],
    max_candidates: int,
    option_feed: str | None,
    mock_decision: str | None,
) -> dict[str, dict[str, Any]]:
    max_workers = _max_concurrent_symbols(config, len(symbols))
    logger.info("Running independent decisions for %s symbols with concurrency=%s", len(symbols), max_workers)
    if max_workers <= 1 or len(symbols) <= 1:
        return {
            symbol: _run_single_symbol_decision(
                config=config,
                logger=logger,
                db_path=db_path,
                alpaca=alpaca,
                symbol=symbol,
                max_candidates=max_candidates,
                option_feed=option_feed,
                mock_decision=mock_decision,
            )
            for symbol in symbols
        }

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="symbol-decision") as executor:
        futures = {
            executor.submit(
                _run_single_symbol_decision,
                config=config,
                logger=logger,
                db_path=db_path,
                alpaca=alpaca,
                symbol=symbol,
                max_candidates=max_candidates,
                option_feed=option_feed,
                mock_decision=mock_decision,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            results[symbol] = future.result()
    return results


def _run_single_symbol_decision(
    *,
    config,
    logger: logging.Logger,
    db_path: Path,
    alpaca: AlpacaClient,
    symbol: str,
    max_candidates: int,
    option_feed: str | None,
    mock_decision: str | None,
) -> dict[str, Any]:
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
        return {"artifact": artifact, "scan_result": scan_result}
    except OpenAIClientError as exc:
        logger.error("Decision failed for %s: %s", symbol, exc)
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - preserve per-symbol progress
        logger.exception("Decision failed for %s: %s", symbol, exc)
        return {"error": str(exc)}


def _max_concurrent_symbols(config, symbol_count: int) -> int:
    configured = int(config.get("decision_engine", "max_concurrent_symbols", default=1))
    if configured < 1:
        configured = 1
    return min(configured, max(1, symbol_count))


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


def _maybe_execute_recommended_closes(
    *,
    config,
    logger: logging.Logger,
    db_path: Path,
    alpaca: AlpacaClient,
    kill_switch: KillSwitch,
    notifier: DiscordNotifier,
    monitor_artifact: dict[str, Any],
    submit_requested: bool,
) -> list[dict[str, Any]]:
    close_spreads = _close_recommended_spreads(monitor_artifact)
    if not close_spreads:
        return []

    state_refresh_error = None
    try:
        final_open_orders = alpaca.get_orders(status="open")
        final_positions = alpaca.get_positions()
    except Exception as exc:  # noqa: BLE001 - final execution guard must fail closed
        logger.exception("Final close execution state refresh failed: %s", exc)
        state_refresh_error = str(exc)
        final_open_orders = []
        final_positions = []

    attempts: list[dict[str, Any]] = []
    for spread in close_spreads:
        attempt = maybe_submit_paper_close_order(
            config=config,
            alpaca=alpaca,
            kill_switch=kill_switch,
            notifier=notifier,
            submit_requested=submit_requested,
            spread=spread,
            order_preview=spread.get("close_order_preview"),
            open_orders=final_open_orders,
            open_positions=final_positions,
            state_refresh_error=state_refresh_error,
        )
        attempt_id = record_execution_attempt(
            db_path,
            created_at=datetime.now(UTC).isoformat(),
            mode=config.mode,
            decision_id=None,
            attempt=attempt,
        )
        logger.info(
            "Paper close execution gate spread=%s status=%s submitted=%s attempt_id=%s block_reasons=%s",
            spread.get("spread_id"),
            attempt.status,
            attempt.submitted,
            attempt_id,
            "; ".join(attempt.block_reasons) if attempt.block_reasons else "-",
        )
        attempts.append(
            {
                "spread_id": spread.get("spread_id"),
                "execution_attempt_id": attempt_id,
                "execution_attempt": attempt.to_dict(),
            }
        )
    return attempts


def _build_open_discovery_cycle_artifact(
    *,
    config,
    phase: str,
    watchlist_artifact: dict[str, Any] | None,
    skipped_open_reason: str | None,
    account_risk_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": config.mode,
        "command": "open-discovery-cycle",
        "phase": phase,
        "account_risk_state": account_risk_state,
        "skipped_open_decisions": bool(skipped_open_reason),
        "skip_open_reason": skipped_open_reason,
        "watchlist_decision": watchlist_artifact,
    }


def _acquire_bot_cycle_lock(config, logger: logging.Logger) -> TextIO | None:
    lock_path = resolve_path(config.get("runtime", "cycle_lock_path", default="data/bot_cycle.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        existing = handle.read().strip()
        logger.error(
            "Another bot cycle already holds lock %s%s",
            lock_path,
            f" ({existing})" if existing else "",
        )
        handle.close()
        return None

    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} acquired_at={datetime.now(UTC).isoformat()}\n")
    handle.flush()
    logger.info("Acquired bot cycle lock at %s", lock_path)
    return handle


def _release_bot_cycle_lock(handle: TextIO, logger: logging.Logger) -> None:
    lock_path = getattr(handle, "name", "bot cycle lock")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    logger.info("Released bot cycle lock at %s", lock_path)


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
    scan_result = scan_put_credit_spreads(
        config=config,
        alpaca=alpaca,
        symbols=symbols,
        max_candidates=max_candidates,
        option_feed=option_feed,
    )

    with _DB_WRITE_LOCK:
        record_option_scan(db_path, mode=config.mode, scan_result=scan_result)
    filtered_scan_result, pre_llm_filter = _apply_pre_llm_filters(
        config=config,
        symbols=symbols,
        scan_result=scan_result,
        market_context=market_context.to_dict(),
        event_context=event_context.to_dict(),
    )
    if pre_llm_filter["eligible_candidate_count"] == 0:
        reason = _pre_llm_skip_reason(pre_llm_filter)
        news_context = _empty_news_context(
            config=config,
            symbols=symbols,
            warning="News was not fetched because hard pre-LLM filters blocked this symbol.",
        )
        packet = build_decision_packet(
            config=config,
            account=account,
            clock=clock,
            positions=positions,
            open_orders=open_orders,
            scan_result=filtered_scan_result,
            market_context=market_context,
            event_context=event_context,
            news_context=news_context,
        )
        packet_dict = packet.to_dict()
        decision = _pre_llm_skip_decision(symbols[0], reason)
        return (
            {
                "decision_id": None,
                "accepted": True,
                "validator_errors": [],
                "decision": decision,
                "decision_source": "pre_llm_filter",
                "llm_called": False,
                "pre_llm_filter": pre_llm_filter,
                "order_preview": None,
                "packet": packet_dict,
                "raw_response": None,
            },
            filtered_scan_result,
        )

    news_context = build_news_context(config=config, alpaca=alpaca, symbols=symbols)
    packet = build_decision_packet(
        config=config,
        account=account,
        clock=clock,
        positions=positions,
        open_orders=open_orders,
        scan_result=filtered_scan_result,
        market_context=market_context,
        event_context=event_context,
        news_context=news_context,
    )
    packet_dict = packet.to_dict()
    decision, raw_response, model = _get_llm_or_mock_decision(mock_decision, config, packet_dict)
    validator_errors = validate_decision_payload(
        decision,
        candidate_ids=packet_candidate_ids(filtered_scan_result),
        candidates_by_id=candidate_dicts_by_id(filtered_scan_result),
        allowed_symbols=set(symbols),
        open_position_symbols={str(position.get("symbol")) for position in positions if position.get("symbol")},
        market_context=packet_dict["market_context"],
        market_context_by_symbol=packet_dict["market_context"]["symbols"],
        event_context_by_symbol=packet_dict["event_context"]["symbols"],
        broad_market_symbol=config.get("market_filters", "broad_market_symbol"),
        require_broad_market_above_ma=bool(
            config.get("market_filters", "require_broad_market_above_30m_ma", default=True)
        ),
        max_loss_per_trade=config.get("risk", "max_loss_per_trade"),
        max_option_quote_age_seconds=int(
            config.get("market_filters", "max_option_quote_age_minutes", default=30)
        )
        * 60,
        min_short_put_distance_pct=config.get("strategy", "min_short_put_distance_pct"),
        min_open_confidence=config.get("decision_engine", "min_open_confidence"),
    )
    with _DB_WRITE_LOCK:
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
            "decision_source": "mock" if mock_decision else "openai",
            "llm_called": not bool(mock_decision),
            "pre_llm_filter": pre_llm_filter,
            "order_preview": order_preview,
            "packet": packet_dict,
            "raw_response": raw_response,
        },
        filtered_scan_result,
    )


def _apply_pre_llm_filters(
    *,
    config,
    symbols: list[str],
    scan_result,
    market_context: dict[str, Any],
    event_context: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    symbol = symbols[0] if symbols else None
    raw_candidates = list(scan_result.candidates)
    context_reasons = _pre_llm_context_block_reasons(
        config=config,
        symbol=symbol,
        market_context=market_context,
        event_context=event_context,
    )
    eligible_candidates = []
    blocked_candidates = []

    for candidate in raw_candidates:
        candidate_reasons = list(context_reasons)
        candidate_reasons.extend(
            _pre_llm_candidate_block_reasons(
                config=config,
                candidate=candidate.to_dict(),
            )
        )
        if candidate_reasons:
            blocked_candidates.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "reasons": candidate_reasons,
                }
            )
            continue
        eligible_candidates.append(candidate)

    block_reasons = _unique_reasons(
        list(context_reasons)
        + [
            reason
            for item in blocked_candidates
            for reason in item.get("reasons", [])
        ]
    )
    if not raw_candidates:
        block_reasons.append("No candidates passed scanner filters: DTE, delta, distance, liquidity, and credit.")

    filtered_warnings = list(scan_result.warnings)
    if block_reasons:
        filtered_warnings.append("Pre-LLM hard filters: " + "; ".join(block_reasons))

    filtered_scan_result = replace(
        scan_result,
        candidates=eligible_candidates,
        warnings=filtered_warnings,
    )
    return filtered_scan_result, {
        "enabled": True,
        "raw_candidate_count": len(raw_candidates),
        "eligible_candidate_count": len(eligible_candidates),
        "blocked_candidate_count": len(blocked_candidates),
        "block_reasons": block_reasons,
        "blocked_candidates": blocked_candidates[:20],
    }


def _pre_llm_context_block_reasons(
    *,
    config,
    symbol: str | None,
    market_context: dict[str, Any],
    event_context: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    symbols_context = market_context.get("symbols")
    if not isinstance(symbols_context, dict) or not symbol:
        reasons.append("Market context is unavailable before LLM decision.")
    else:
        symbol_context = symbols_context.get(symbol)
        if not isinstance(symbol_context, dict):
            reasons.append(f"Market context is unavailable for {symbol}.")
        else:
            if symbol_context.get("latest_bar_fresh") is not True:
                reasons.append(f"Latest market bar is not fresh for {symbol}.")
            if symbol_context.get("market_trend_ok") is not True:
                reasons.append(f"Market trend filter is not passing for {symbol}.")

    broad_symbol = str(config.get("market_filters", "broad_market_symbol", default="") or "").strip().upper()
    if broad_symbol:
        broad_context = symbols_context.get(broad_symbol) if isinstance(symbols_context, dict) else None
        if not isinstance(broad_context, dict):
            reasons.append(f"Broad market context is unavailable for {broad_symbol}.")
        else:
            if broad_context.get("latest_bar_fresh") is not True:
                reasons.append(f"Broad market latest bar is not fresh for {broad_symbol}.")
            if broad_context.get("block_intraday_down") is not False:
                reasons.append(f"Broad market intraday down filter is not passing for {broad_symbol}.")
            if bool(config.get("market_filters", "require_broad_market_above_30m_ma", default=True)):
                if broad_context.get("above_trend_ma") is not True:
                    reasons.append(f"Broad market moving-average filter is not passing for {broad_symbol}.")

    event_symbols = event_context.get("symbols")
    if not isinstance(event_symbols, dict) or not symbol:
        reasons.append("Event context is unavailable before LLM decision.")
    else:
        symbol_event = event_symbols.get(symbol)
        if not isinstance(symbol_event, dict):
            reasons.append(f"Event context is unavailable for {symbol}.")
        elif symbol_event.get("earnings_ok") is not True:
            reasons.append(f"Earnings/event filter is not passing for {symbol}.")

    return _unique_reasons(reasons)


def _pre_llm_candidate_block_reasons(*, config, candidate: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if candidate.get("liquidity_ok") is not True:
        reasons.append("Candidate liquidity filter is not passing.")

    max_loss_per_trade = _decimal_or_none(config.get("risk", "max_loss_per_trade"))
    if max_loss_per_trade is not None:
        candidate_max_loss = _decimal_or_none(candidate.get("max_loss"))
        if candidate_max_loss is None:
            reasons.append("Candidate max loss is unavailable.")
        elif candidate_max_loss > max_loss_per_trade:
            reasons.append(f"Candidate max loss {candidate_max_loss} exceeds limit {max_loss_per_trade}.")

    max_quote_age_seconds = int(config.get("market_filters", "max_option_quote_age_minutes", default=30)) * 60
    quote_age = candidate.get("max_quote_age_seconds")
    if not isinstance(quote_age, int):
        reasons.append("Candidate option quote age is unavailable.")
    elif quote_age < -60:
        reasons.append(f"Candidate option quote timestamp is from the future: age={quote_age}.")
    elif quote_age > max_quote_age_seconds:
        reasons.append(f"Candidate option quote age {quote_age}s exceeds limit {max_quote_age_seconds}s.")

    min_distance = _decimal_or_none(config.get("strategy", "min_short_put_distance_pct"))
    if min_distance is not None and min_distance > 0:
        candidate_distance = _decimal_or_none(candidate.get("short_put_distance_pct"))
        if candidate_distance is None:
            reasons.append("Candidate short put distance is unavailable.")
        elif candidate_distance < min_distance:
            reasons.append(f"Candidate short put distance {candidate_distance} is below minimum {min_distance}%.")

    return reasons


def _pre_llm_skip_reason(pre_llm_filter: dict[str, Any]) -> str:
    reasons = pre_llm_filter.get("block_reasons") or []
    if reasons:
        return "Hard filters blocked LLM call: " + "; ".join(str(reason) for reason in reasons[:3])
    return "Hard filters blocked LLM call."


def _pre_llm_skip_decision(symbol: str | None, reason: str) -> dict[str, Any]:
    return {
        "action": "skip",
        "symbol": symbol,
        "candidate_id": None,
        "quantity": 0,
        "limit_price": None,
        "confidence": 1.0,
        "decision_reason": reason,
        "news_assessment": {
            "risk_level": "unknown",
            "sentiment": "unknown",
            "summary": "News was not evaluated because hard numeric filters failed first.",
        },
        "risk_checklist": _default_true_risk_checklist(no_material_negative_news=False),
        "exit_plan": {
            "profit_take_credit_pct": 50,
            "loss_trigger": "2x initial credit or short put delta above 0.45",
            "close_before_expiry_days": 3,
        },
    }


def _empty_news_context(*, config, symbols: list[str], warning: str) -> NewsContext:
    return NewsContext(
        provider=str(config.get("news", "provider", default="alpaca")),
        generated_at=datetime.now(UTC).isoformat(),
        lookback_hours=int(config.get("news", "lookback_hours", default=24)),
        symbols=symbols,
        item_count=0,
        items=[],
        warnings=[warning],
    )


def _default_true_risk_checklist(*, no_material_negative_news: bool = True) -> dict[str, bool]:
    return {
        "defined_risk": True,
        "within_max_loss": True,
        "liquidity_ok": True,
        "earnings_ok": True,
        "no_material_negative_news": no_material_negative_news,
        "market_trend_ok": True,
        "broad_market_ok": True,
        "short_put_distance_ok": True,
    }


def _unique_reasons(reasons: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for reason in reasons:
        text = str(reason)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


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
        "risk_checklist": _default_true_risk_checklist(no_material_negative_news=False),
        "exit_plan": {
            "profit_take_credit_pct": 50,
            "loss_trigger": "2x initial credit or short put delta above 0.45",
            "close_before_expiry_days": 3,
        },
    }


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
        "decision_source": artifact.get("decision_source"),
        "llm_called": artifact.get("llm_called"),
        "pre_llm_filter": artifact.get("pre_llm_filter"),
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

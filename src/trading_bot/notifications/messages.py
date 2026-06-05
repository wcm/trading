from __future__ import annotations

import logging
from typing import Any

from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.utils.money import format_counts


DISCORD_CONTENT_LIMIT = 1900


def _send_scheduler_heartbeat(
    notifier: DiscordNotifier,
    logger: logging.Logger,
    *,
    status: str,
    interval_minutes: float,
    heartbeat_minutes: float,
    details: str,
) -> bool:
    content = (
        "Local scheduler heartbeat\n"
        f"Status: {status}\n"
        f"Interval minutes: {interval_minutes:g}\n"
        f"Heartbeat minutes: {heartbeat_minutes:g}\n"
        f"Details: {details}"
    )
    result = notifier.send(content)
    if result.ok:
        logger.info("Discord scheduler heartbeat sent: %s", status)
        return True

    logger.error("Discord scheduler heartbeat failed: %s", result.error)
    return False


def _send_scheduler_error(
    notifier: DiscordNotifier,
    logger: logging.Logger,
    *,
    phase: str,
    error: str,
) -> bool:
    content = (
        "Local scheduler error\n"
        f"Phase: {phase}\n"
        f"Error: {error}"
    )
    result = notifier.send(content)
    if result.ok:
        logger.info("Discord scheduler error sent: phase=%s", phase)
        return True

    logger.error("Discord scheduler error failed: %s", result.error)
    return False


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

    content = (
        "Read-only watchlist decision complete\n"
        f"Symbols: {', '.join(artifact['symbols'])}\n"
        f"Accepted opens: {allocation['accepted_open_count']}\n"
        f"Executable opens: {allocation.get('execution_eligible_open_count')}\n"
        f"Selected open: {selected_line}\n"
        f"Order preview: {_preview_status(selected_preview)}\n"
        f"Execution: {_execution_status(execution_attempt)}\n"
        f"Decision detail messages: {len(artifact.get('per_symbol') or [])}"
    )
    messages = [content]
    messages.extend(_watchlist_decision_detail_messages(artifact, heading="Watchlist decision"))
    if _send_discord_messages(notifier, messages, logger, "watchlist decision summary"):
        return True

    return False


def _send_run_cycle_summary(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
    *,
    include_decision_details: bool = True,
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
        lines.extend(_close_execution_status_lines(artifact.get("close_execution_attempts") or []))
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
    elif artifact.get("skipped_open_decisions"):
        risk = artifact.get("account_risk_state") or {}
        lines.append(f"Open decisions: skipped ({artifact.get('skip_open_reason')})")
        if risk:
            lines.append(
                "Account risk: "
                f"daily_pnl={risk.get('daily_pnl')} weekly_pnl={risk.get('weekly_pnl')} "
                f"new_trades_today={risk.get('new_trades_today')}/"
                f"{risk.get('max_new_trades_per_day')} "
                f"blocks={len(risk.get('block_reasons') or [])}"
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
                f"Executable opens: {allocation.get('execution_eligible_open_count')}",
                f"Selected open: {selected_line}",
                f"Order preview: {_preview_status(selected_preview)}",
                f"Execution: {_execution_status(execution_attempt)}",
                f"Decision detail messages: {len(watchlist.get('per_symbol') or [])}",
            ]
        )

    messages = ["\n".join(lines)]
    if include_decision_details and not close_spreads and not artifact.get("skipped_open_decisions"):
        messages.extend(_watchlist_decision_detail_messages(watchlist, heading="Run-cycle decision"))
    if _send_discord_messages(notifier, messages, logger, "run-cycle summary"):
        return True

    return False


def _send_order_poll_summary(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
) -> bool:
    changes = artifact.get("changes") or []
    lines = [
        "Order lifecycle update",
        f"Status filter: {artifact.get('status_filter')}",
        f"Orders polled: {artifact.get('order_count')}",
        f"Changes: {artifact.get('change_count')}",
    ]
    if not changes:
        lines.append("- No order status changes detected.")
    for change in changes:
        lines.append(
            f"- {change.get('broker_order_id')} client={change.get('client_order_id')} "
            f"symbol={change.get('symbol')} status={change.get('previous_status')}->{change.get('status')} "
            f"filled={change.get('previous_filled_qty')}->{change.get('filled_qty')} "
            f"qty={change.get('qty')} class={change.get('order_class')}"
        )

    messages = _split_discord_content("\n".join(lines))
    return _send_discord_messages(notifier, messages, logger, "order lifecycle update")


def _send_daily_trading_summary(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
) -> bool:
    account = artifact.get("account") or {}
    positions = artifact.get("positions") or {}
    orders = artifact.get("orders") or {}
    lifecycle = orders.get("lifecycle_events") or {}
    attempts = artifact.get("execution_attempts") or {}
    lines = [
        "Daily trading summary",
        f"Date: {artifact.get('summary_date')} ET",
        f"Mode: {artifact.get('mode')}",
        (
            f"Equity: {account.get('equity')} "
            f"daily P&L: {account.get('daily_pnl')} "
            f"buying power: {account.get('buying_power')}"
        ),
        (
            f"Open positions: broker={positions.get('broker_position_count')} "
            f"option={positions.get('option_position_count')} spreads={positions.get('spread_count')}"
        ),
        f"Estimated open spread P&L: {positions.get('estimated_open_spread_pnl')}",
        f"Close recommendations: {positions.get('close_recommended_count')}",
        (
            f"Orders: open_now={orders.get('open_order_count')} "
            f"recent_polled={orders.get('recent_order_count')} "
            f"events_today={lifecycle.get('total')}"
        ),
        f"Order event statuses: {format_counts(lifecycle.get('by_status') or {})}",
        (
            f"Execution attempts: total={attempts.get('total')} "
            f"requested={attempts.get('requested')} submitted={attempts.get('submitted')}"
        ),
        f"Execution statuses: {format_counts(attempts.get('by_status') or {})}",
    ]
    spreads = positions.get("spreads") or []
    if spreads:
        lines.append("Open spreads:")
        for spread in spreads[:10]:
            lines.append(
                f"- {spread.get('spread_id')} qty={spread.get('quantity')} "
                f"dte={spread.get('dte')} pnl={spread.get('estimated_unrealized_pnl')} "
                f"close={spread.get('close_recommended')}"
            )
    else:
        lines.append("Open spreads: none")

    messages = _split_discord_content("\n".join(lines))
    return _send_discord_messages(notifier, messages, logger, "daily trading summary")


def _close_execution_status_lines(close_execution_attempts: list[dict[str, Any]]) -> list[str]:
    if not close_execution_attempts:
        return ["Close execution: not attempted"]
    lines = [f"Close execution attempts: {len(close_execution_attempts)}"]
    for item in close_execution_attempts[:5]:
        if not isinstance(item, dict):
            continue
        attempt = item.get("execution_attempt") if isinstance(item, dict) else None
        lines.append(
            f"- {item.get('spread_id')}: {_execution_status(attempt if isinstance(attempt, dict) else None)}"
        )
    return lines


def _watchlist_decision_detail_messages(artifact: dict[str, Any], *, heading: str) -> list[str]:
    items = artifact.get("per_symbol") or []
    total = len(items)
    messages = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        decision = item.get("decision") or {}
        action = decision.get("action", "error")
        reason = item.get("error") or decision.get("decision_reason") or "-"
        candidate = item.get("selected_candidate") or {}
        validator_errors = item.get("validator_errors") or []
        news_assessment = decision.get("news_assessment") or {}
        risk_checklist = decision.get("risk_checklist") or {}
        lines = [
            f"{heading} {index}/{total}",
            f"Symbol: {item.get('symbol')}",
            f"Action: {action}",
            f"Accepted: {item.get('accepted')}",
            f"Candidate: {decision.get('candidate_id')}",
            f"Confidence: {decision.get('confidence')}",
            f"Order preview: {_preview_status(item.get('order_preview'))}",
        ]
        if candidate:
            lines.extend(
                [
                    "Candidate details:",
                    (
                        f"{candidate.get('underlying_symbol')} "
                        f"{candidate.get('short_strike')}/{candidate.get('long_strike')}P "
                        f"exp={candidate.get('expiration_date')} "
                        f"credit={candidate.get('net_credit')} "
                        f"max_loss={candidate.get('max_loss')}"
                    ),
                ]
            )
        if validator_errors:
            lines.append("Validator errors:")
            lines.extend(f"- {error}" for error in validator_errors)
        if isinstance(news_assessment, dict) and news_assessment:
            lines.extend(
                [
                    "News assessment:",
                    f"risk={news_assessment.get('risk_level')} sentiment={news_assessment.get('sentiment')}",
                    str(news_assessment.get("summary") or "-"),
                ]
            )
        if isinstance(risk_checklist, dict) and risk_checklist:
            checklist = ", ".join(f"{key}={value}" for key, value in sorted(risk_checklist.items()))
            lines.extend(["Risk checklist:", checklist])
        lines.extend(["Reason:", str(reason)])
        messages.append("\n".join(lines))
    return messages


def _send_discord_messages(
    notifier: DiscordNotifier,
    messages: list[str],
    logger: logging.Logger,
    label: str,
) -> bool:
    sent_count = 0
    for message in messages:
        for part in _split_discord_content(message):
            result = notifier.send(part)
            if not result.ok:
                logger.error("Discord %s failed: %s", label, result.error)
                return False
            sent_count += 1

    logger.info("Discord %s sent: messages=%s", label, sent_count)
    return True


def _split_discord_content(content: str, limit: int = DISCORD_CONTENT_LIMIT) -> list[str]:
    if len(content) <= limit:
        return [content]

    parts: list[str] = []
    current = ""
    for line in content.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current.rstrip("\n"))
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            if current:
                parts.append(current.rstrip("\n"))
            current = ""
        current += line

    if current:
        parts.append(current.rstrip("\n"))
    return parts or [""]


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
    order_management = execution_attempt.get("order_management")
    if isinstance(order_management, dict) and order_management.get("final_status"):
        return (
            f"{status}, submitted={submitted}, "
            f"entry={order_management.get('final_status')}"
        )
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
        "Position monitor complete\n"
        f"Option positions: {artifact.get('option_position_count')}\n"
        f"Spreads: {artifact.get('spread_count')}\n"
        f"Unpaired legs: {len(artifact.get('unpaired_legs', []))}\n"
        + "\n".join(spread_lines)
        + "\n"
        + "\n".join(_close_execution_status_lines(artifact.get("close_execution_attempts") or []))
    )
    result = notifier.send(content)
    if result.ok:
        logger.info("Discord position monitor summary sent")
        return True

    logger.error("Discord position monitor summary failed: %s", result.error)
    return False

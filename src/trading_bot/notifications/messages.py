from __future__ import annotations

import argparse
import logging
from decimal import Decimal
from typing import Any

from trading_bot.monitoring.positions import parse_occ_option_symbol
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.utils.money import decimal_or_none, format_counts, format_optional_decimal


DISCORD_CONTENT_LIMIT = 1900


def _send_scheduler_error(
    notifier: DiscordNotifier,
    logger: logging.Logger,
    *,
    phase: str,
    error: str,
) -> bool:
    content = (
        "# Scheduler Error\n\n"
        f"**Where:** {phase}\n"
        f"**Error:** {error}"
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
        "# Bot Test\n\n"
        "**Status:** connected\n"
        "**Mode:** paper\n"
        "**Broker:** Alpaca"
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
        "# Option Scan\n\n"
        f"**Symbols:** {', '.join(scan_result.symbols)}\n"
        f"**Feed:** {scan_result.feed}\n"
        f"**Candidates:** {len(scan_result.candidates)}\n\n"
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
        "# AI Decision\n\n"
        f"**Status:** {status}\n"
        f"**Symbol:** {decision.get('symbol')}\n"
        f"**Action:** {decision.get('action')}\n"
        f"**Confidence:** {decision.get('confidence')}\n"
        f"**Candidate:** {decision.get('candidate_id')}\n"
        f"**Order preview:** {_preview_status(order_preview)}\n\n"
        f"**Reason:** {decision.get('decision_reason')}\n\n"
        f"**Validator errors:**\n{errors}"
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
    execution_attempt = artifact.get("execution_attempt")
    selected_line = (
        f"{selected['symbol']} {selected['candidate_id']} limit {selected['limit_price']} "
        f"max_loss {selected['max_loss']}"
        if selected
        else "none"
    )

    content = (
        "# Watchlist Decision\n\n"
        f"**Selected:** {selected_line}\n"
        f"**Accepted:** {allocation['accepted_open_count']}\n"
        f"**Executable:** {allocation.get('execution_eligible_open_count')}\n"
        f"**Execution:** {_execution_status(execution_attempt)}\n\n"
        f"**Symbols:** {', '.join(artifact['symbols'])}"
    )
    messages = [content]
    messages.extend(_watchlist_decision_detail_messages(artifact, heading="Watchlist decision"))
    if _send_discord_messages(notifier, messages, logger, "watchlist decision summary"):
        return True

    return False


def _send_open_discovery_summary(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
    *,
    include_decision_details: bool = True,
) -> bool:
    lines = [
        "# New Trade Skipped" if artifact.get("skipped_open_decisions") else "# New Trade Search",
    ]

    if artifact.get("skipped_open_decisions"):
        risk = artifact.get("account_risk_state") or {}
        lines.append("")
        lines.append(f"**Reason:** {_simple_skip_reason(artifact.get('skip_open_reason'))}")
        if risk:
            lines.extend(
                [
                    "",
                    f"**Daily P&L:** {risk.get('daily_pnl')}",
                    f"**Weekly P&L:** {risk.get('weekly_pnl')}",
                    (
                        f"**New trades today:** {risk.get('new_trades_today')} / "
                        f"{risk.get('max_new_trades_per_day')}"
                    ),
                ]
            )
        messages = ["\n".join(lines)]
        return _send_discord_messages(notifier, messages, logger, "open-discovery summary")

    watchlist = artifact.get("watchlist_decision") or {}
    allocation = watchlist.get("allocation") or {}
    selected = allocation.get("selected_open")
    execution_attempt = watchlist.get("execution_attempt")
    lines.append("")
    if selected:
        lines.extend(
            [
                f"**Selected:** {_selected_open_label(selected)}",
                f"**Price:** {selected.get('limit_price')}",
                f"**Max loss:** {selected.get('max_loss')}",
                f"**Execution:** {_execution_status(execution_attempt)}",
            ]
        )
    else:
        hard_filter_summary = _hard_filter_summary(watchlist)
        lines.extend(
            [
                "**Selected:** none",
                f"**Accepted:** {allocation.get('accepted_open_count')}",
                f"**Executable:** {allocation.get('execution_eligible_open_count')}",
            ]
        )
        if hard_filter_summary:
            lines.extend(["", *hard_filter_summary])

    messages = ["\n".join(lines)]
    if include_decision_details:
        messages.extend(_watchlist_decision_detail_messages(watchlist, heading="New-trade decision"))
    return _send_discord_messages(notifier, messages, logger, "open-discovery summary")


def _send_order_poll_summary(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
) -> bool:
    messages = _order_event_messages(artifact.get("changes") or [])
    if not messages:
        messages = ["# No Order Changes\n\nNo order changes detected."]
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
        "# Daily Summary",
        "",
        f"**Date:** {artifact.get('summary_date')} ET",
        f"**Mode:** {artifact.get('mode')}",
        "",
        f"**Daily P&L:** {account.get('daily_pnl')}",
        f"**Equity:** {account.get('equity')}",
        f"**Buying power:** {account.get('buying_power')}",
        "",
        "## Open Positions",
        "",
        f"**Spreads:** {positions.get('spread_count')}",
        f"**Estimated open P&L:** {positions.get('estimated_open_spread_pnl')}",
        f"**Close recommendations:** {positions.get('close_recommended_count')}",
        "",
        "## Orders",
        "",
        f"**Open orders now:** {orders.get('open_order_count')}",
        f"**Events today:** {lifecycle.get('total')}",
        f"**Statuses:** {format_counts(lifecycle.get('by_status') or {})}",
        "",
        "## Execution",
        "",
        f"**Attempts:** {attempts.get('total')}",
        f"**Submitted:** {attempts.get('submitted')}",
        f"**Statuses:** {format_counts(attempts.get('by_status') or {})}",
    ]
    spreads = positions.get("spreads") or []
    if spreads:
        lines.extend(["", "## Open Spreads", ""])
        for spread in spreads[:10]:
            lines.append(f"**{_spread_label(spread)}**")
            lines.append(f"P&L: {spread.get('estimated_unrealized_pnl')}")
            lines.append("")
    else:
        lines.extend(["", "## Open Spreads", "", "none"])

    messages = _split_discord_content("\n".join(lines))
    return _send_discord_messages(notifier, messages, logger, "daily trading summary")


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
            f"## {heading} {index}/{total}",
            "",
            f"**Symbol:** {item.get('symbol')}",
            f"**Action:** {action}",
            f"**Accepted:** {item.get('accepted')}",
            f"**Source:** {item.get('decision_source') or 'unknown'}",
            f"**Candidate:** {decision.get('candidate_id')}",
            f"**Confidence:** {decision.get('confidence')}",
            f"**Order preview:** {_preview_status(item.get('order_preview'))}",
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
            lines.append("**Validator errors:**")
            lines.extend(f"- {error}" for error in validator_errors)
        if isinstance(news_assessment, dict) and news_assessment:
            lines.extend(
                [
                    "**News:**",
                    f"Risk: {news_assessment.get('risk_level')} | Sentiment: {news_assessment.get('sentiment')}",
                    str(news_assessment.get("summary") or "-"),
                ]
            )
        if isinstance(risk_checklist, dict) and risk_checklist:
            checklist = ", ".join(f"{key}={value}" for key, value in sorted(risk_checklist.items()))
            lines.extend(["**Risk checklist:**", checklist])
        lines.extend(["**Reason:**", str(reason)])
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
    close_spreads = [
        spread
        for spread in artifact.get("spreads", [])
        if isinstance(spread, dict) and spread.get("close_recommended") is True
    ]
    if not close_spreads:
        logger.info("No close recommendations to send to Discord")
        return True

    spread_lines = []
    for spread in close_spreads[:10]:
        spread_lines.extend(
            [
                f"**{_spread_label(spread)}**",
                f"**Price:** {spread.get('close_debit')}",
                f"**Reason:** {_close_reason(spread)}",
                "",
            ]
        )

    content = (
        "# Order Close Recommendation\n\n"
        + "\n".join(spread_lines).strip()
    )
    result = notifier.send(content)
    if result.ok:
        logger.info("Discord position monitor summary sent")
        return True

    logger.error("Discord position monitor summary failed: %s", result.error)
    return False


def _order_event_messages(changes: list[dict[str, Any]]) -> list[str]:
    groups = {
        "filled": [],
        "closed": [],
        "rejected": [],
        "canceled": [],
    }
    for change in changes:
        kind = _order_event_kind(change)
        if kind in groups:
            groups[kind].append(change)

    messages = []
    if groups["filled"]:
        messages.append(_simple_order_message("# Orders Filled", groups["filled"]))
    if groups["closed"]:
        messages.append(_simple_order_message("# Orders Closed", groups["closed"], include_pnl=True))
    if groups["rejected"]:
        messages.append(_simple_order_message("# Orders Rejected", groups["rejected"], include_reason=True))
    if groups["canceled"]:
        messages.append(_simple_order_message("# Orders Canceled", groups["canceled"]))
    return messages


def _order_event_kind(change: dict[str, Any]) -> str | None:
    status = str(change.get("status") or "").lower()
    raw_order = change.get("raw_order") if isinstance(change.get("raw_order"), dict) else {}
    intents = _order_position_intents(raw_order)
    if status in {"rejected"}:
        return "rejected"
    if status in {"canceled", "expired"}:
        return "canceled"
    if status in {"filled", "partially_filled"}:
        if {"buy_to_close", "sell_to_close"} <= intents:
            return "closed"
        return "filled"
    return None


def _simple_order_message(
    title: str,
    changes: list[dict[str, Any]],
    *,
    include_reason: bool = False,
    include_pnl: bool = False,
) -> str:
    lines = [title]
    for change in changes:
        lines.extend(["", f"**{_order_label(change)}**", f"**Price:** {_order_price(change)}"])
        if include_pnl:
            realized_pnl = _order_realized_pnl(change)
            if realized_pnl is not None:
                lines.append(f"**P&L:** {_format_signed_decimal(realized_pnl)}")
        if include_reason:
            lines.append(f"**Reason:** {_order_reject_reason(change)}")
    return "\n".join(lines)


def _order_position_intents(order: dict[str, Any]) -> set[str]:
    legs = order.get("legs")
    if not isinstance(legs, list):
        return set()
    return {
        str(leg.get("position_intent") or "")
        for leg in legs
        if isinstance(leg, dict) and leg.get("position_intent")
    }


def _order_label(change: dict[str, Any]) -> str:
    raw_order = change.get("raw_order") if isinstance(change.get("raw_order"), dict) else {}
    spread_label = _spread_label_from_order(raw_order)
    if spread_label:
        return spread_label
    symbol = change.get("symbol") or raw_order.get("symbol")
    if symbol:
        return str(symbol)
    return str(change.get("client_order_id") or change.get("broker_order_id") or "unknown order")


def _spread_label_from_order(order: dict[str, Any]) -> str | None:
    legs = order.get("legs")
    if not isinstance(legs, list):
        return None
    parsed_legs = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        parsed = parse_occ_option_symbol(str(leg.get("symbol") or ""))
        if parsed is not None and parsed.option_type == "put":
            parsed_legs.append((leg, parsed))

    short_leg = _find_order_leg(parsed_legs, {"sell_to_open", "buy_to_close"})
    long_leg = _find_order_leg(parsed_legs, {"buy_to_open", "sell_to_close"})
    if not short_leg or not long_leg:
        return None
    _short_raw, short_parsed = short_leg
    _long_raw, long_parsed = long_leg
    if short_parsed.underlying_symbol != long_parsed.underlying_symbol:
        return None
    if short_parsed.expiration_date != long_parsed.expiration_date:
        return None
    if short_parsed.strike <= long_parsed.strike:
        return None
    return (
        f"{short_parsed.underlying_symbol} "
        f"{_format_strike(short_parsed.strike)}/{_format_strike(long_parsed.strike)}P"
    )


def _find_order_leg(
    parsed_legs: list[tuple[dict[str, Any], Any]],
    position_intents: set[str],
) -> tuple[dict[str, Any], Any] | None:
    for leg, parsed in parsed_legs:
        if str(leg.get("position_intent") or "") in position_intents:
            return leg, parsed
    return None


def _order_price(change: dict[str, Any]) -> str:
    raw_order = change.get("raw_order") if isinstance(change.get("raw_order"), dict) else {}
    price = (
        decimal_or_none(raw_order.get("filled_avg_price"))
        or _order_leg_price_difference(raw_order)
        or decimal_or_none(raw_order.get("limit_price"))
        or decimal_or_none(change.get("filled_avg_price"))
    )
    return format_optional_decimal(abs(price)) if price is not None else "-"


def _order_leg_price_difference(order: dict[str, Any]):
    legs = order.get("legs")
    if not isinstance(legs, list):
        return None
    prices = [
        decimal_or_none(leg.get("filled_avg_price"))
        for leg in legs
        if isinstance(leg, dict)
    ]
    prices = [price for price in prices if price is not None]
    if len(prices) < 2:
        return None
    return abs(prices[0] - prices[1])


def _order_reject_reason(change: dict[str, Any]) -> str:
    raw_order = change.get("raw_order") if isinstance(change.get("raw_order"), dict) else {}
    return str(
        raw_order.get("reject_reason")
        or raw_order.get("rejected_reason")
        or raw_order.get("reason")
        or "not provided"
    )


def _order_realized_pnl(change: dict[str, Any]) -> Decimal | None:
    spread_trade = change.get("spread_trade")
    if not isinstance(spread_trade, dict):
        return None
    realized = decimal_or_none(spread_trade.get("realized_pnl"))
    if realized is not None:
        return realized
    entry = decimal_or_none(spread_trade.get("entry_credit"))
    close = decimal_or_none(spread_trade.get("close_debit"))
    quantity = decimal_or_none(spread_trade.get("quantity"))
    if entry is None or close is None or quantity is None:
        return None
    return (entry - close) * Decimal("100") * quantity


def _format_signed_decimal(value: Decimal) -> str:
    formatted = format_optional_decimal(value)
    if formatted is None:
        return "-"
    if value > 0:
        return f"+{formatted}"
    return formatted


def _spread_label(spread: dict[str, Any]) -> str:
    symbol = spread.get("underlying_symbol")
    short_strike = spread.get("short_strike")
    long_strike = spread.get("long_strike")
    if symbol and short_strike and long_strike:
        return f"{symbol} {short_strike}/{long_strike}P"
    spread_id = spread.get("spread_id")
    if spread_id:
        return str(spread_id)
    return "unknown spread"


def _format_strike(value: object) -> str:
    text = format_optional_decimal(decimal_or_none(value))
    if text is None:
        return str(value)
    if text.endswith(".00"):
        return text[:-3]
    if text.endswith("0"):
        return text.rstrip("0").rstrip(".")
    return text


def _selected_open_label(selected: dict[str, Any]) -> str:
    candidate_id = str(selected.get("candidate_id") or "")
    parts = candidate_id.split("-")
    if len(parts) >= 5:
        return f"{parts[0]} {parts[-2].removesuffix('P')}/{parts[-1].removesuffix('P')}P"
    return str(selected.get("symbol") or candidate_id or "selected trade")


def _close_reason(spread: dict[str, Any]) -> str:
    labels = {
        "profit_target_hit": "profit target reached",
        "loss_trigger_hit": "loss trigger reached",
        "close_before_expiry": "close before expiry",
        "short_strike_threatened": "short strike threatened",
    }
    active = [
        labels.get(flag, str(flag).replace("_", " "))
        for flag, enabled in (spread.get("exit_flags") or {}).items()
        if enabled
    ]
    return ", ".join(active) if active else "close rule triggered"


def _simple_skip_reason(reason: object) -> str:
    text = str(reason or "not provided")
    prefixes = [
        "Account risk gate blocked new opens: ",
        "Account risk gate unavailable: ",
    ]
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _hard_filter_summary(watchlist: dict[str, Any]) -> list[str]:
    items = [item for item in watchlist.get("per_symbol", []) if isinstance(item, dict)]
    if not items:
        return []
    filters = [item.get("pre_llm_filter") for item in items if isinstance(item.get("pre_llm_filter"), dict)]
    if not filters:
        return []
    passed = len([item for item in filters if item.get("eligible_candidate_count", 0) > 0])
    lines = [f"**Hard filters passed:** {passed} / {len(items)}"]
    if passed == 0:
        first_reason = _first_hard_filter_reason(filters)
        if first_reason:
            lines.append(f"**Reason:** {first_reason}")
    return lines


def _first_hard_filter_reason(filters: list[dict[str, Any]]) -> str | None:
    for item in filters:
        reasons = item.get("block_reasons") or []
        if reasons:
            return str(reasons[0])
    return None

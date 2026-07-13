from __future__ import annotations

import logging
from typing import Any

from trading_bot.notifications.discord import DiscordNotifier


def send_grid_event_notifications(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
) -> bool:
    messages = grid_event_messages(artifact)
    if not messages:
        logger.info("No grid events to send to Discord")
        return True
    if not notifier.is_configured:
        logger.warning("Discord webhook is not configured")
        return False

    for message in messages:
        result = notifier.send(message)
        if not result.ok:
            logger.error("Discord grid event failed: %s", result.error)
            return False
    logger.info("Discord grid events sent: messages=%s", len(messages))
    return True


def send_grid_error(
    notifier: DiscordNotifier,
    logger: logging.Logger,
    *,
    error: str,
) -> bool:
    if not notifier.is_configured:
        logger.warning("Discord webhook is not configured")
        return False
    result = notifier.send(f"# Grid Bot Error\n\n**Error:** {error}")
    if not result.ok:
        logger.error("Discord grid error failed: %s", result.error)
    return result.ok


def grid_event_messages(artifact: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    symbol = str(artifact.get("symbol") or "TQQQ")

    safety_errors = artifact.get("safety_errors") or []
    if safety_errors:
        details = "\n".join(f"- {error}" for error in safety_errors)
        messages.append(f"# Grid Safety Block\n\n**{symbol} orders paused**\n\n{details}")

    for event in artifact.get("reconciliation_events") or []:
        message = _reconciliation_event_message(symbol, event)
        if message:
            messages.append(message)

    for submission in artifact.get("submitted_orders") or []:
        payload = submission.get("payload") or {}
        action = str(submission.get("action") or "").lower()
        if action not in {"buy", "sell"}:
            continue
        title = "Grid Buy Submitted" if action == "buy" else "Grid Sell Submitted"
        messages.append(
            f"# {title}\n\n"
            f"**{symbol}:** {payload.get('qty')} shares at ${payload.get('limit_price')}\n"
            f"**Order duration:** {str(payload.get('time_in_force') or '-').upper()}\n"
            f"**Level:** {submission.get('level_index')}"
        )
    return messages


def _reconciliation_event_message(symbol: str, event: dict[str, Any]) -> str | None:
    side = str(event.get("side") or "").lower()
    status = str(event.get("order_status") or "").lower()
    if side not in {"buy", "sell"}:
        return None

    side_title = "Buy" if side == "buy" else "Sell"
    if status == "filled":
        lines = [
            f"# Grid {side_title} Filled",
            "",
            f"**{symbol}:** {event.get('filled_qty')} shares at ${event.get('fill_price')}",
            f"**Level:** {event.get('level_index')}",
        ]
        if side == "buy":
            lines.append(f"**Sell target:** ${event.get('sell_target')}")
        else:
            lines.append(f"**P&L:** ${event.get('realized_pnl')}")
        return "\n".join(lines)

    if status == "partially_filled":
        return (
            f"# Grid {side_title} Partially Filled\n\n"
            f"**{symbol}:** {event.get('filled_qty')} shares at ${event.get('fill_price')}\n"
            f"**Level:** {event.get('level_index')}"
        )

    if status in {"canceled", "cancelled", "expired", "rejected"}:
        readable_status = "Canceled" if status in {"canceled", "cancelled"} else status.title()
        return (
            f"# Grid {side_title} {readable_status}\n\n"
            f"**{symbol} level:** {event.get('level_index')}\n"
            f"**Filled before closing:** {event.get('filled_qty') or '0'} shares"
        )
    return None

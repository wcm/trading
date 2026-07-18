from __future__ import annotations

import logging
from typing import Any

from trading_bot.notifications.discord import DiscordNotifier


def send_dca_notifications(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
) -> bool:
    messages = dca_event_messages(artifact)
    if not messages:
        logger.info("No DCA events to send to Discord")
        return True
    if not notifier.is_configured:
        logger.warning("Discord webhook is not configured")
        return False
    for message in messages:
        result = notifier.send(message)
        if not result.ok:
            logger.error("Discord DCA notification failed: %s", result.error)
            return False
    return True


def send_dca_error(
    notifier: DiscordNotifier,
    logger: logging.Logger,
    *,
    error: str,
) -> bool:
    if not notifier.is_configured:
        logger.warning("Discord webhook is not configured")
        return False
    result = notifier.send(f"# DCA Bot Error\n\n**Error:** {error}")
    if not result.ok:
        logger.error("Discord DCA error failed: %s", result.error)
    return result.ok


def dca_event_messages(artifact: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    symbol = str(artifact.get("symbol") or "-")
    for event in artifact.get("reconciliation_events") or []:
        status = str(event.get("order_status") or "").lower()
        if status == "filled":
            messages.append(
                "# DCA Buy Filled\n\n"
                f"**{symbol}:** {event.get('filled_qty')} shares at "
                f"${event.get('filled_avg_price')}\n"
                f"**Invested:** ${event.get('contribution_amount')}"
            )
        elif status == "partially_filled":
            messages.append(
                "# DCA Buy Partially Filled\n\n"
                f"**{symbol}:** {event.get('filled_qty')} shares at "
                f"${event.get('filled_avg_price')}"
            )
        elif status in {"canceled", "cancelled", "expired", "rejected"}:
            title = "Canceled" if status in {"canceled", "cancelled"} else status.title()
            filled_qty = event.get("filled_qty")
            if filled_qty not in {None, "0", "0.00"}:
                messages.append(
                    f"# DCA Buy Partially Filled Then {title}\n\n"
                    f"**{symbol}:** {filled_qty} shares at "
                    f"${event.get('filled_avg_price')}"
                )
            else:
                messages.append(
                    f"# DCA Buy {title}\n\n"
                    f"**{symbol} scheduled investment:** "
                    f"${event.get('contribution_amount')}"
                )

    submitted = artifact.get("submitted_order")
    if submitted:
        payload = submitted.get("payload") or {}
        messages.append(
            "# DCA Buy Submitted\n\n"
            f"**{symbol}:** ${payload.get('notional') or submitted.get('contribution_amount')}\n"
            f"**Schedule:** {submitted.get('period_key')}"
        )

    plan = artifact.get("plan") or {}
    if plan.get("blocked_reason"):
        messages.append(
            "# DCA Purchase Blocked\n\n"
            f"**{symbol}:** {plan.get('blocked_reason')}"
        )
    return messages

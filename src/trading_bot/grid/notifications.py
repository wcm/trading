from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.utils.money import decimal_or_none


def send_grid_event_notifications(
    notifier: DiscordNotifier,
    artifact: dict[str, Any],
    logger: logging.Logger,
    *,
    include_status: bool = False,
) -> bool:
    messages = grid_event_messages(artifact, include_status=include_status)
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


def grid_event_messages(
    artifact: dict[str, Any],
    *,
    include_status: bool = False,
) -> list[str]:
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
    if include_status:
        messages.append(_grid_status_message(artifact, symbol))
    return messages


def _grid_status_message(artifact: dict[str, Any], symbol: str) -> str:
    bar = artifact.get("bar") or {}
    state = artifact.get("state") or {}
    broker = artifact.get("broker_snapshot") or {}
    next_buy = artifact.get("next_buy_level") or {}

    price = decimal_or_none(bar.get("close"))
    bar_open = decimal_or_none(bar.get("open"))
    anchor = decimal_or_none(state.get("anchor_price"))
    bar_change = (
        price - bar_open if price is not None and bar_open is not None else None
    )
    anchor_change = price - anchor if price is not None and anchor is not None else None

    next_buy_text = "No unused buy levels"
    if next_buy.get("price") is not None:
        next_buy_text = (
            f"Level {next_buy.get('level_index')} at {_money(next_buy.get('price'))}"
        )

    open_orders = broker.get("open_orders") or []
    status = _grid_status_text(artifact, state, open_orders)
    return (
        "# Grid Status\n\n"
        f"**{symbol} price:** {_money(price)}\n"
        f"**Latest 5-minute change:** {_signed_money(bar_change)} "
        f"({_signed_percent(bar_change, bar_open)})\n"
        f"**From grid anchor:** {_signed_money(anchor_change)} "
        f"({_signed_percent(anchor_change, anchor)})\n"
        f"**Grid anchor:** {_money(anchor)}\n"
        f"**Next buy:** {next_buy_text}\n"
        f"**Shares held:** {broker.get('position_qty') or '0'}\n"
        f"**Working orders:** {len(open_orders)}\n"
        f"**Unrealized P&L:** {_signed_money(state.get('unrealized_pnl'))}\n"
        f"**Status:** {status}"
    )


def _grid_status_text(
    artifact: dict[str, Any],
    state: dict[str, Any],
    open_orders: list[dict[str, Any]],
) -> str:
    if artifact.get("safety_errors"):
        return "Orders paused for a safety check"
    if artifact.get("submitted_orders"):
        return "New order submitted"
    if int(state.get("open_inventory_lot_count") or 0) > 0:
        return "Holding shares and waiting to sell"
    if open_orders:
        return "Waiting for an order to fill"
    if not artifact.get("market_open", True):
        return "Market is closed"
    return "Waiting for the next buy level"


def _money(value: Any) -> str:
    number = decimal_or_none(value)
    if number is None:
        return "-"
    return f"${number.quantize(Decimal('0.01'))}"


def _signed_money(value: Any) -> str:
    number = decimal_or_none(value)
    if number is None:
        return "-"
    if number > 0:
        return f"+${number:.2f}"
    if number < 0:
        return f"-${abs(number):.2f}"
    return "$0.00"


def _signed_percent(change: Any, base: Any) -> str:
    change_number = decimal_or_none(change)
    base_number = decimal_or_none(base)
    if change_number is None or base_number in {None, 0}:
        return "-"
    percent = (change_number / base_number) * 100
    return f"{percent:+.2f}%" if percent else "0.00%"


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

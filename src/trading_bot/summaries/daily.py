from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_bot.app import bootstrap
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.cycles.open_discovery import _close_recommended_spreads
from trading_bot.monitoring.positions import monitor_put_credit_spreads
from trading_bot.notifications.messages import _send_daily_trading_summary
from trading_bot.orders.lifecycle import _poll_order_status_changes
from trading_bot.storage.db import (
    record_bot_run,
    summarize_execution_attempts,
    summarize_order_status_events,
)
from trading_bot.utils.artifacts import write_json_artifact
from trading_bot.utils.market_time import daily_summary_date_from_arg, eastern_date_window_utc
from trading_bot.utils.money import decimal_or_none, format_optional_decimal, sum_decimal_strings


def run_daily_summary(args: argparse.Namespace) -> int:
    config, logger, db_path, _kill_switch, notifier = bootstrap(args)
    summary_date = daily_summary_date_from_arg(args.summary_date)
    logger.info("Starting daily trading summary date=%s", summary_date.isoformat())

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    try:
        order_poll = _poll_order_status_changes(
            config=config,
            logger=logger,
            db_path=db_path,
            alpaca=alpaca,
            status="all",
            limit=int(config.get("runtime", "scheduler_order_poll_limit", default=50)),
        )
        artifact = _build_daily_trading_summary(
            config=config,
            db_path=db_path,
            alpaca=alpaca,
            option_feed=args.option_feed,
            summary_date=summary_date,
            order_poll=order_poll,
        )
    except Exception as exc:  # noqa: BLE001 - report broker/API errors cleanly in CLI
        logger.exception("Daily trading summary failed: %s", exc)
        return 1

    if args.json_output:
        write_json_artifact(args.json_output, artifact, logger, "daily trading summary")

    discord_ok = True
    if args.send_discord:
        discord_ok = _send_daily_trading_summary(notifier, artifact, logger)

    record_bot_run(
        db_path,
        started_at=datetime.now(UTC).isoformat(),
        mode=config.mode,
        status=_daily_summary_bot_run_status(args.send_discord, discord_ok),
        details={
            "command": "daily-summary",
            "summary_date": summary_date.isoformat(),
            "discord_requested": args.send_discord,
            "discord_ok": discord_ok,
        },
    )
    return 0 if discord_ok else 1


def _build_daily_trading_summary(
    *,
    config,
    db_path: Path,
    alpaca: AlpacaClient,
    option_feed: str | None,
    summary_date: date,
    order_poll: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start_at, end_at = eastern_date_window_utc(summary_date)
    account = alpaca.get_account()
    positions = alpaca.get_positions()
    monitor_result = monitor_put_credit_spreads(
        config=config,
        alpaca=alpaca,
        positions=positions,
        option_feed=option_feed,
    )
    open_orders = alpaca.get_orders(status="open", limit=50)
    recent_orders = (
        order_poll.get("orders")
        if isinstance(order_poll, dict) and isinstance(order_poll.get("orders"), list)
        else alpaca.get_orders(status="all", limit=50)
    )
    monitor_artifact = monitor_result.to_dict()
    order_events = summarize_order_status_events(
        db_path,
        mode=config.mode,
        start_at=start_at,
        end_at=end_at,
    )
    execution_attempts = summarize_execution_attempts(
        db_path,
        mode=config.mode,
        start_at=start_at,
        end_at=end_at,
    )
    open_spread_pnl = sum_decimal_strings(
        [spread.get("estimated_unrealized_pnl") for spread in monitor_artifact.get("spreads", [])]
    )
    close_recommended = _close_recommended_spreads(monitor_artifact)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": config.mode,
        "summary_date": summary_date.isoformat(),
        "timezone": "America/New_York",
        "window": {
            "start_at": start_at,
            "end_at": end_at,
        },
        "account": _daily_account_summary(account),
        "positions": {
            "broker_position_count": len(positions),
            "option_position_count": monitor_artifact.get("option_position_count"),
            "spread_count": monitor_artifact.get("spread_count"),
            "unpaired_leg_count": len(monitor_artifact.get("unpaired_legs", [])),
            "estimated_open_spread_pnl": format_optional_decimal(open_spread_pnl),
            "close_recommended_count": len(close_recommended),
            "spreads": monitor_artifact.get("spreads", []),
            "unpaired_legs": monitor_artifact.get("unpaired_legs", []),
        },
        "orders": {
            "open_order_count": len(open_orders),
            "recent_order_count": len(recent_orders),
            "lifecycle_events": order_events,
            "latest_poll": {
                "change_count": order_poll.get("change_count") if isinstance(order_poll, dict) else None,
                "order_count": order_poll.get("order_count") if isinstance(order_poll, dict) else None,
            },
        },
        "execution_attempts": execution_attempts,
    }


def _daily_account_summary(account: dict[str, Any]) -> dict[str, Any]:
    equity = decimal_or_none(account.get("equity"))
    last_equity = decimal_or_none(account.get("last_equity"))
    daily_pnl = equity - last_equity if equity is not None and last_equity is not None else None
    return {
        "status": account.get("status"),
        "equity": format_optional_decimal(equity),
        "last_equity": format_optional_decimal(last_equity),
        "daily_pnl": format_optional_decimal(daily_pnl),
        "buying_power": format_optional_decimal(decimal_or_none(account.get("buying_power"))),
        "cash": format_optional_decimal(decimal_or_none(account.get("cash"))),
        "portfolio_value": format_optional_decimal(decimal_or_none(account.get("portfolio_value"))),
    }


def _daily_summary_bot_run_status(discord_requested: bool, discord_ok: bool) -> str:
    if not discord_ok:
        return "daily_summary_failed"
    return "daily_summary_sent" if discord_requested else "daily_summary_built"

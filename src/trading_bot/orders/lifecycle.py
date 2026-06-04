from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.app import bootstrap
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.notifications.messages import _send_order_poll_summary
from trading_bot.storage.db import record_order_status_changes
from trading_bot.utils.artifacts import write_json_artifact


def run_order_poll(args: argparse.Namespace) -> int:
    config, logger, db_path, _kill_switch, notifier = bootstrap(args)
    logger.info("Starting order lifecycle poll status=%s limit=%s", args.status, args.limit)

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    try:
        artifact = _poll_order_status_changes(
            config=config,
            logger=logger,
            db_path=db_path,
            alpaca=alpaca,
            status=args.status,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001 - report broker/API errors cleanly in CLI
        logger.exception("Order lifecycle poll failed: %s", exc)
        return 1

    if args.json_output:
        write_json_artifact(args.json_output, artifact, logger, "order poll")

    if args.send_discord and (artifact["changes"] or args.notify_no_changes):
        discord_ok = _send_order_poll_summary(notifier, artifact, logger)
        if not discord_ok:
            return 1

    return 0


def _poll_order_status_changes(
    *,
    config,
    logger: logging.Logger,
    db_path: Path,
    alpaca: AlpacaClient,
    status: str,
    limit: int,
) -> dict[str, Any]:
    observed_at = datetime.now(UTC).isoformat()
    orders = alpaca.get_orders(status=status, limit=limit)
    changes = record_order_status_changes(
        db_path,
        observed_at=observed_at,
        mode=config.mode,
        orders=orders,
    )
    logger.info(
        "Order lifecycle poll complete: status=%s orders=%s changes=%s",
        status,
        len(orders),
        len(changes),
    )
    for change in changes:
        logger.info(
            "Order change id=%s client_order_id=%s status=%s previous_status=%s filled=%s previous_filled=%s",
            change.get("broker_order_id"),
            change.get("client_order_id"),
            change.get("status"),
            change.get("previous_status"),
            change.get("filled_qty"),
            change.get("previous_filled_qty"),
        )
    return {
        "generated_at": observed_at,
        "mode": config.mode,
        "status_filter": status,
        "limit": limit,
        "order_count": len(orders),
        "change_count": len(changes),
        "changes": changes,
        "orders": orders,
    }

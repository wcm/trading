from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any
from uuid import uuid4

from trading_bot.app import bootstrap
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.config import AppConfig, resolve_path
from trading_bot.dca.notifications import send_dca_error, send_dca_notifications
from trading_bot.dca.state import (
    DcaPurchaseState,
    DcaState,
    load_dca_state,
    save_dca_state,
)
from trading_bot.dca.strategy import (
    DcaPlan,
    DcaStrategyConfig,
    build_dca_plan,
    dca_config_from_app_config,
    validate_dca_config,
)
from trading_bot.utils.artifacts import write_json_artifact
from trading_bot.utils.market_time import EASTERN
from trading_bot.utils.money import decimal_or_none, format_decimal


INACTIVE_ORDER_STATUSES = {"canceled", "cancelled", "expired", "rejected"}


def run_dca_cycle_command(args: argparse.Namespace) -> int:
    config, logger, _db_path, kill_switch, notifier = bootstrap(args)
    strategy_config = dca_config_from_app_config(config)
    try:
        validate_dca_config(strategy_config)
    except ValueError as exc:
        logger.error("Invalid DCA configuration: %s", exc)
        return 1
    state_path = _dca_state_path(args, config)
    state = load_dca_state(
        state_path,
        strategy_name=strategy_config.name,
        symbol=strategy_config.symbol,
    )
    logger.info("Starting DCA cycle for %s state=%s", strategy_config.symbol, state_path)

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        if args.send_discord:
            send_dca_error(notifier, logger, error=str(exc))
        return 1

    try:
        reconciliation_events = _reconcile_dca_orders(alpaca, state, logger)
    except Exception as exc:  # noqa: BLE001 - fail closed when broker state is uncertain
        logger.exception("DCA order reconciliation failed: %s", exc)
        if args.send_discord:
            send_dca_error(notifier, logger, error=f"Could not check existing orders: {exc}")
        return 1

    as_of = datetime.now(UTC).astimezone(EASTERN)
    drawdown_pct = Decimal("0")
    market_context: dict[str, Any] | None = None
    preliminary_plan = build_dca_plan(
        as_of=as_of.date(),
        config=strategy_config,
        completed_period_keys=state.completed_period_keys(),
        annual_contributed=state.annual_contributed(as_of.year),
    )
    if preliminary_plan.due_period is not None:
        try:
            market_context = _load_dca_market_context(
                alpaca,
                strategy_config,
                config,
            )
            drawdown_pct = Decimal(str(market_context["drawdown_pct"]))
        except Exception as exc:  # noqa: BLE001 - no purchase without reliable market data
            logger.exception("DCA market data failed: %s", exc)
            if args.send_discord:
                send_dca_error(notifier, logger, error=f"Could not load market data: {exc}")
            return 1

    plan = build_dca_plan(
        as_of=as_of.date(),
        config=strategy_config,
        completed_period_keys=state.completed_period_keys(),
        annual_contributed=state.annual_contributed(as_of.year),
        drawdown_pct=drawdown_pct,
    )
    market_open = _market_is_open(args, alpaca, logger)
    execution_allowed, execution_reason = _dca_execution_allowed(
        args=args,
        config=config,
        kill_switch_active=kill_switch.is_active(),
        market_open=market_open,
    )

    submitted_order = None
    if plan.should_buy and args.submit_paper and execution_allowed:
        try:
            submitted_order = _submit_dca_purchase(
                alpaca=alpaca,
                state=state,
                strategy_config=strategy_config,
                plan=plan,
                market_context=market_context or {},
                logger=logger,
            )
        except Exception as exc:  # noqa: BLE001 - submission errors require operator attention
            save_dca_state(state_path, state)
            logger.exception("DCA order submission failed: %s", exc)
            if args.send_discord:
                send_dca_error(notifier, logger, error=f"Order submission failed: {exc}")
            return 1
    elif plan.should_buy and args.submit_paper:
        logger.warning("DCA paper submission skipped: %s", execution_reason)

    save_dca_state(state_path, state)
    artifact = {
        "generated_at": datetime.now(UTC).isoformat(),
        "command": "dca-cycle",
        "mode": config.mode,
        "symbol": strategy_config.symbol,
        "as_of_date_et": as_of.date().isoformat(),
        "market_open": market_open,
        "market_context": market_context,
        "submit_requested": bool(args.submit_paper),
        "execution_allowed": execution_allowed,
        "execution_reason": execution_reason,
        "plan": plan.to_dict(),
        "reconciliation_events": reconciliation_events,
        "submitted_order": submitted_order,
        "state": state.summary(),
        "state_path": str(state_path),
    }
    logger.info(
        "DCA cycle complete: symbol=%s due=%s submitted=%s filled_purchases=%s",
        strategy_config.symbol,
        plan.due_period.period_key if plan.due_period else "none",
        bool(submitted_order),
        artifact["state"]["filled_purchase_count"],
    )
    if args.json_output:
        write_json_artifact(args.json_output, artifact, logger, "DCA cycle")
    if args.send_discord:
        send_dca_notifications(notifier, artifact, logger)
    return 0


def run_dca_scheduler_command(args: argparse.Namespace) -> int:
    config, logger, _db_path, _kill_switch, _notifier = bootstrap(args)
    interval_minutes = float(
        args.interval_minutes
        if args.interval_minutes is not None
        else config.get("runtime", "dca_scheduler_interval_minutes", default=60)
    )
    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Starting DCA scheduler interval_minutes=%s submit_paper=%s",
        interval_minutes,
        bool(args.submit_paper),
    )
    while True:
        if not args.ignore_market_hours:
            clock = alpaca.get_clock()
            if not bool(clock.get("is_open")):
                if args.once:
                    logger.info("Market is closed; DCA scheduler once check exiting")
                    return 0
                sleep_seconds = _seconds_until_next_open(
                    clock,
                    fallback_minutes=interval_minutes,
                )
                logger.info(
                    "Market is closed; sleeping %.0f seconds until next open",
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
                continue

        cycle_args = argparse.Namespace(**vars(args))
        cycle_args.command = "dca-cycle"
        if args.json_output_dir:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            output_dir = resolve_path(args.json_output_dir)
            cycle_args.json_output = str(output_dir / f"dca_cycle_{timestamp}.json")
        result = run_dca_cycle_command(cycle_args)
        if args.once:
            return result
        time.sleep(max(60.0, interval_minutes * 60))


def _load_dca_market_context(
    alpaca: AlpacaClient,
    strategy_config: DcaStrategyConfig,
    config: AppConfig,
) -> dict[str, Any]:
    feed = str(config.get("alpaca", "stock_data_feed", default="iex"))
    latest = alpaca.get_latest_stock_bars([strategy_config.symbol], feed=feed)
    row = latest.get(strategy_config.symbol)
    if not isinstance(row, dict):
        raise ValueError(f"No latest {strategy_config.symbol} bar returned by Alpaca")
    price = decimal_or_none(row.get("c"))
    if price is None or price <= 0:
        raise ValueError(f"Latest {strategy_config.symbol} bar has no valid close")

    drawdown_pct = Decimal("0")
    if strategy_config.sizing_mode == "drawdown_scaled":
        end = datetime.now(UTC)
        start = end - timedelta(days=strategy_config.drawdown_lookback_days)
        rows = alpaca.get_stock_bars(
            [strategy_config.symbol],
            timeframe="1Day",
            start=start.isoformat(),
            end=end.isoformat(),
            feed=feed,
            adjustment="split",
            limit=1000,
            sort="asc",
        ).get(strategy_config.symbol, [])
        closes = [
            value
            for item in rows
            if (value := decimal_or_none(item.get("c"))) is not None
        ]
        if not closes:
            raise ValueError("No daily bars returned for DCA drawdown sizing")
        peak = max(closes)
        if peak > 0:
            drawdown_pct = max(Decimal("0"), (peak - price) / peak * Decimal("100"))

    return {
        "timestamp": row.get("t"),
        "price": format(price.quantize(Decimal("0.01")), "f"),
        "drawdown_pct": format(drawdown_pct.quantize(Decimal("0.01")), "f"),
        "feed": feed,
    }


def _submit_dca_purchase(
    *,
    alpaca: AlpacaClient,
    state: DcaState,
    strategy_config: DcaStrategyConfig,
    plan: DcaPlan,
    market_context: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, Any]:
    if plan.due_period is None or plan.contribution_amount is None:
        raise ValueError("DCA purchase submission requires a due contribution")
    client_order_id = _dca_client_order_id(
        strategy_config.symbol,
        plan.due_period.period_key,
    )
    price = decimal_or_none(market_context.get("price"))
    payload = _dca_market_order_payload(
        symbol=strategy_config.symbol,
        contribution_amount=plan.contribution_amount,
        price=price,
        allow_fractional_shares=strategy_config.allow_fractional_shares,
        client_order_id=client_order_id,
    )
    order = alpaca.submit_order(payload)
    now = datetime.now(UTC).isoformat()
    purchase = DcaPurchaseState(
        purchase_id=f"dca-{uuid4().hex[:12]}",
        period_key=plan.due_period.period_key,
        scheduled_date=plan.due_period.scheduled_date.isoformat(),
        contribution_amount=plan.contribution_amount,
        status="submitted",
        client_order_id=client_order_id,
        broker_order_id=str(order.get("id") or ""),
        submitted_at=now,
        last_order_status=str(order.get("status") or "") or None,
    )
    state.purchases.append(purchase)
    logger.info("Submitted DCA purchase period=%s payload=%s", purchase.period_key, payload)
    return {
        "purchase_id": purchase.purchase_id,
        "period_key": purchase.period_key,
        "contribution_amount": format_decimal(purchase.contribution_amount),
        "payload": payload,
        "broker_order_id": purchase.broker_order_id,
        "broker_status": order.get("status"),
    }


def _dca_market_order_payload(
    *,
    symbol: str,
    contribution_amount: Decimal,
    price: Decimal | None,
    allow_fractional_shares: bool,
    client_order_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": symbol,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }
    if allow_fractional_shares:
        payload["notional"] = format(contribution_amount.quantize(Decimal("0.01")), "f")
        return payload
    if price is None or price <= 0:
        raise ValueError("Whole-share DCA orders require a valid current price")
    qty = (contribution_amount / price).to_integral_value(rounding=ROUND_DOWN)
    if qty <= 0:
        raise ValueError("DCA contribution is too small to buy one whole share")
    payload["qty"] = format_decimal(qty)
    return payload


def _reconcile_dca_orders(
    alpaca: AlpacaClient,
    state: DcaState,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for purchase in state.purchases:
        if purchase.status not in {"submitted", "partially_filled"}:
            continue
        order = alpaca.get_order(purchase.broker_order_id)
        status = str(order.get("status") or "").lower()
        filled_qty = decimal_or_none(order.get("filled_qty")) or Decimal("0")
        fill_price = decimal_or_none(order.get("filled_avg_price"))
        if status == purchase.last_order_status and filled_qty == (
            purchase.filled_qty or Decimal("0")
        ):
            continue
        purchase.last_order_status = status
        purchase.filled_qty = filled_qty or purchase.filled_qty
        purchase.filled_avg_price = fill_price or purchase.filled_avg_price
        if status == "filled":
            purchase.status = "filled"
            purchase.filled_at = str(order.get("filled_at") or datetime.now(UTC).isoformat())
        elif status == "partially_filled":
            purchase.status = "partially_filled"
        elif status in INACTIVE_ORDER_STATUSES:
            purchase.status = "partially_filled_closed" if filled_qty > 0 else status
            if filled_qty > 0:
                purchase.filled_at = str(
                    order.get("filled_at") or datetime.now(UTC).isoformat()
                )
        elif not status:
            continue
        event = {
            "purchase_id": purchase.purchase_id,
            "period_key": purchase.period_key,
            "order_status": status,
            "contribution_amount": format_decimal(purchase.contribution_amount),
            "filled_qty": format_decimal(filled_qty),
            "filled_avg_price": (
                format_decimal(fill_price) if fill_price is not None else None
            ),
        }
        events.append(event)
        logger.info("DCA reconciliation: %s", event)
    return events


def _dca_execution_allowed(
    *,
    args: argparse.Namespace,
    config: AppConfig,
    kill_switch_active: bool,
    market_open: bool,
) -> tuple[bool, str]:
    if not args.submit_paper:
        return False, "preview_only"
    if config.mode != "paper":
        return False, "config mode is not paper"
    if not bool(config.get("execution", "enable_paper_orders", default=False)):
        return False, "execution.enable_paper_orders is false"
    if str(config.get("execution", "order_type", default="market")) != "market":
        return False, "DCA v1 requires execution.order_type=market"
    if kill_switch_active:
        return False, "kill switch is active"
    if not market_open:
        return False, "market is closed"
    return True, "paper DCA submission enabled"


def _market_is_open(
    args: argparse.Namespace,
    alpaca: AlpacaClient,
    logger: logging.Logger,
) -> bool:
    if args.ignore_market_hours:
        return True
    try:
        return bool(alpaca.get_clock().get("is_open"))
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("Could not read Alpaca clock; treating market as closed: %s", exc)
        return False


def _dca_state_path(args: argparse.Namespace, config: AppConfig):
    return resolve_path(
        args.state_path
        or config.get("storage", "dca_state_path", default="data/dca/dca_state.json")
    )


def _dca_client_order_id(symbol: str, period_key: str) -> str:
    clean_period = "".join(character for character in period_key if character.isalnum())
    clean_symbol = "".join(character for character in symbol.lower() if character.isalnum())
    return f"dca-{clean_symbol}-{clean_period}"[:48]


def _seconds_until_next_open(clock: dict[str, Any], *, fallback_minutes: float) -> float:
    raw_next_open = clock.get("next_open")
    if raw_next_open:
        next_open = datetime.fromisoformat(str(raw_next_open).replace("Z", "+00:00"))
        if next_open.tzinfo is None:
            next_open = next_open.replace(tzinfo=UTC)
        seconds = (next_open.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        if seconds > 0:
            return max(60.0, seconds)
    return max(60.0, fallback_minutes * 60)

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading_bot.app import bootstrap
from trading_bot.backtesting.bars import PriceBar
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.config import AppConfig, resolve_path
from trading_bot.execution.orders import build_client_order_id
from trading_bot.grid.state import GridLotState, GridState, load_grid_state, save_grid_state
from trading_bot.grid.reconciliation import (
    grid_reconciliation_errors,
    load_grid_broker_snapshot,
)
from trading_bot.grid.notifications import send_grid_error, send_grid_event_notifications
from trading_bot.grid.strategy import (
    GridIntent,
    GridPlan,
    GridStrategyConfig,
    build_grid_plan,
    buy_price_for_level,
    grid_config_from_app_config,
    lot_from_buy_intent,
)
from trading_bot.utils.artifacts import write_json_artifact
from trading_bot.utils.money import decimal_or_none, format_decimal


FILLED_ORDER_STATUS = "filled"
INACTIVE_ORDER_STATUSES = {"canceled", "cancelled", "expired", "rejected"}


def run_grid_cycle_command(args: argparse.Namespace) -> int:
    config, logger, _db_path, kill_switch, notifier = bootstrap(args)
    strategy_config = grid_config_from_app_config(config)
    state_path = _grid_state_path(args, config)
    logger.info("Starting grid cycle for %s state=%s", strategy_config.symbol, state_path)

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        if args.send_discord:
            send_grid_error(notifier, logger, error=str(exc))
        return 1

    try:
        bar = _fetch_latest_grid_bar(args, config, alpaca, strategy_config.symbol)
    except Exception as exc:  # noqa: BLE001 - report API/data failures cleanly in CLI
        logger.exception("Grid cycle failed while loading bars: %s", exc)
        if args.send_discord:
            send_grid_error(notifier, logger, error=f"Could not load market data: {exc}")
        return 1

    state = load_grid_state(
        state_path,
        strategy_name=strategy_config.name,
        symbol=strategy_config.symbol,
    )

    reconciliation_events: list[dict[str, Any]] = []
    try:
        reconciliation_events = _reconcile_grid_orders(
            alpaca=alpaca,
            state=state,
            strategy_config=strategy_config,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001 - keep the state safe if Alpaca has a transient issue
        logger.exception("Grid order reconciliation failed: %s", exc)
        if args.send_discord:
            send_grid_error(notifier, logger, error=f"Could not check existing orders: {exc}")
        return 1

    try:
        broker_snapshot = load_grid_broker_snapshot(alpaca, symbol=strategy_config.symbol)
        safety_errors = grid_reconciliation_errors(state, broker_snapshot)
        if safety_errors:
            retry_events = _reconcile_grid_orders(
                alpaca=alpaca,
                state=state,
                strategy_config=strategy_config,
                logger=logger,
            )
            reconciliation_events.extend(retry_events)
            broker_snapshot = load_grid_broker_snapshot(alpaca, symbol=strategy_config.symbol)
            safety_errors = grid_reconciliation_errors(state, broker_snapshot)
    except Exception as exc:  # noqa: BLE001 - fail closed if broker state is uncertain
        logger.exception("Grid broker-state check failed: %s", exc)
        if args.send_discord:
            send_grid_error(notifier, logger, error=f"Could not verify Alpaca account state: {exc}")
        return 1

    if safety_errors:
        for error in safety_errors:
            logger.error("Grid safety block: %s", error)
        plan = GridPlan(
            intents=[],
            blocked=[{"reason": "broker_state_mismatch", "details": safety_errors}],
            events=["Broker and local grid state do not match"],
        )
    else:
        plan = build_grid_plan(state, strategy_config, bar)
    market_open = _market_is_open(args, alpaca, logger)
    execution_allowed, execution_reason = _execution_allowed(
        args=args,
        config=config,
        kill_switch_active=kill_switch.is_active(),
        market_open=market_open,
        safety_errors=safety_errors,
    )

    submitted_orders: list[dict[str, Any]] = []
    if args.submit_paper and execution_allowed:
        try:
            submitted_orders = _submit_grid_intents(
                alpaca=alpaca,
                state=state,
                strategy_config=strategy_config,
                intents=plan.intents,
                logger=logger,
            )
        except Exception as exc:  # noqa: BLE001 - persist any successfully submitted earlier orders
            save_grid_state(state_path, state)
            logger.exception("Grid order submission failed: %s", exc)
            if args.send_discord:
                send_grid_error(notifier, logger, error=f"Order submission failed: {exc}")
            return 1
    elif args.submit_paper:
        logger.warning("Grid paper submission skipped: %s", execution_reason)

    save_grid_state(state_path, state)

    artifact = {
        "generated_at": datetime.now(UTC).isoformat(),
        "command": "grid-cycle",
        "mode": config.mode,
        "symbol": strategy_config.symbol,
        "timeframe": _grid_timeframe(args, config),
        "state_path": str(state_path),
        "bar": bar.to_dict(),
        "submit_requested": bool(args.submit_paper),
        "execution_allowed": execution_allowed,
        "execution_reason": execution_reason,
        "market_open": market_open,
        "reconciliation_events": reconciliation_events,
        "broker_snapshot": broker_snapshot.to_dict(),
        "safety_errors": safety_errors,
        "plan": plan.to_dict(),
        "submitted_orders": submitted_orders,
        "state": state.summary(mark_price=bar.close),
        "next_buy_level": _next_grid_buy_level(state, strategy_config),
    }

    _log_grid_cycle_summary(logger, artifact)
    if args.json_output:
        write_json_artifact(args.json_output, artifact, logger, "grid cycle")
    if args.send_discord:
        send_grid_event_notifications(
            notifier,
            artifact,
            logger,
            include_status=bool(
                config.get("notifications", "grid_status_every_cycle", default=False)
            ),
        )
    return 0


def run_grid_scheduler_command(args: argparse.Namespace) -> int:
    config, logger, _db_path, _kill_switch, _notifier = bootstrap(args)
    interval_minutes = _grid_scheduler_interval_minutes(args, config)
    logger.info(
        "Starting grid scheduler interval_minutes=%s submit_paper=%s",
        interval_minutes,
        bool(args.submit_paper),
    )

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    while True:
        if not args.ignore_market_hours:
            clock = alpaca.get_clock()
            if not bool(clock.get("is_open")):
                if args.once:
                    logger.info("Market is closed; grid scheduler once check exiting")
                    return 0
                sleep_seconds = _seconds_until_next_open(clock, fallback_minutes=interval_minutes)
                logger.info("Market is closed; sleeping %.0f seconds until next open", sleep_seconds)
                time.sleep(sleep_seconds)
                continue

        cycle_args = _grid_scheduler_cycle_args(args)
        result = run_grid_cycle_command(cycle_args)
        if args.once:
            return result
        time.sleep(max(1.0, interval_minutes * 60))


def _fetch_latest_grid_bar(
    args: argparse.Namespace,
    config: AppConfig,
    alpaca: AlpacaClient,
    symbol: str,
) -> PriceBar:
    end = datetime.now(UTC)
    start = end - timedelta(days=int(args.lookback_days or 5))
    feed = args.feed or str(config.get("alpaca", "stock_data_feed", default="iex"))
    bars_by_symbol = alpaca.get_stock_bars(
        [symbol],
        timeframe=_grid_timeframe(args, config),
        start=start.isoformat(),
        end=end.isoformat(),
        feed=feed,
        limit=1000,
        sort="asc",
    )
    rows = bars_by_symbol.get(symbol.upper(), [])
    if not rows:
        raise ValueError(f"No recent {symbol} bars returned by Alpaca")
    return _price_bar_from_alpaca_row(rows[-1])


def _price_bar_from_alpaca_row(row: dict[str, Any]) -> PriceBar:
    open_price = decimal_or_none(row.get("o"))
    high = decimal_or_none(row.get("h"))
    low = decimal_or_none(row.get("l"))
    close = decimal_or_none(row.get("c"))
    timestamp = row.get("t")
    if None in (open_price, high, low, close) or not timestamp:
        raise ValueError(f"Alpaca bar is missing required fields: {row}")
    return PriceBar(
        timestamp=str(timestamp),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=int(row["v"]) if row.get("v") is not None else None,
    )


def _reconcile_grid_orders(
    *,
    alpaca: AlpacaClient,
    state,
    strategy_config: GridStrategyConfig,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for lot in state.lots:
        if lot.status == "buy_submitted" and lot.buy_order_id:
            order = alpaca.get_order(lot.buy_order_id)
            event = _reconcile_buy_order(lot, order, strategy_config)
            if event:
                events.append(event)
        elif lot.status == "sell_submitted" and lot.sell_order_id:
            order = alpaca.get_order(lot.sell_order_id)
            event = _reconcile_sell_order(lot, order)
            if event:
                events.append(event)

    for event in events:
        logger.info("Grid reconciliation: %s", event)
    return events


def _reconcile_buy_order(
    lot: GridLotState,
    order: dict[str, Any],
    strategy_config: GridStrategyConfig,
) -> dict[str, Any] | None:
    status = str(order.get("status") or "").lower()
    filled_qty = decimal_or_none(order.get("filled_qty")) or Decimal("0")
    if not status:
        return None
    if status == lot.last_order_status and (
        status != "partially_filled" or filled_qty == (lot.qty or Decimal("0"))
    ):
        return None
    lot.last_order_status = status
    fill_price = decimal_or_none(order.get("filled_avg_price")) or lot.buy_price
    if status in {FILLED_ORDER_STATUS, "partially_filled"}:
        if status == FILLED_ORDER_STATUS and filled_qty <= 0:
            filled_qty = decimal_or_none(order.get("qty")) or Decimal("0")
        _apply_buy_fill(lot, strategy_config, qty=filled_qty, fill_price=fill_price)
    if status == FILLED_ORDER_STATUS:
        lot.buy_filled_at = str(order.get("filled_at") or datetime.now(UTC).isoformat())
        lot.status = "open"
    elif status in INACTIVE_ORDER_STATUSES:
        if filled_qty > 0:
            _apply_buy_fill(lot, strategy_config, qty=filled_qty, fill_price=fill_price)
            lot.status = "open"
            lot.notes.append(f"Buy order became {status} after a partial fill")
        else:
            lot.status = f"buy_{status}"
    else:
        return {
            "lot_id": lot.lot_id,
            "level_index": lot.level_index,
            "side": "buy",
            "order_status": status,
            "filled_qty": format_decimal(filled_qty),
            "fill_price": format_decimal(fill_price),
        }
    return {
        "lot_id": lot.lot_id,
        "level_index": lot.level_index,
        "side": "buy",
        "order_status": status,
        "lot_status": lot.status,
        "filled_qty": format_decimal(filled_qty),
        "fill_price": format_decimal(fill_price),
        "sell_target": format_decimal(lot.sell_target),
    }


def _apply_buy_fill(
    lot: GridLotState,
    strategy_config: GridStrategyConfig,
    *,
    qty: Decimal,
    fill_price: Decimal,
) -> None:
    lot.qty = qty
    lot.buy_fill_price = fill_price
    spacing = strategy_config.grid_spacing_pct / Decimal("100")
    lot.sell_target = (fill_price * (Decimal("1") + spacing)).quantize(Decimal("0.01"))


def _reconcile_sell_order(lot: GridLotState, order: dict[str, Any]) -> dict[str, Any] | None:
    status = str(order.get("status") or "").lower()
    filled_qty = decimal_or_none(order.get("filled_qty")) or Decimal("0")
    if not status:
        return None
    if status == lot.last_order_status and (
        status != "partially_filled"
        or filled_qty == (lot.sell_filled_qty or Decimal("0"))
    ):
        return None
    lot.last_order_status = status
    fill_price = decimal_or_none(order.get("filled_avg_price")) or lot.sell_target
    buy_price = lot.buy_fill_price or lot.buy_price
    if status == "partially_filled":
        lot.sell_filled_qty = filled_qty
        lot.sell_fill_price = fill_price
        return {
            "lot_id": lot.lot_id,
            "level_index": lot.level_index,
            "side": "sell",
            "order_status": status,
            "filled_qty": format_decimal(filled_qty),
            "remaining_qty": format_decimal(lot.remaining_qty()),
            "fill_price": format_decimal(fill_price),
        }
    if status == FILLED_ORDER_STATUS:
        qty = filled_qty or lot.qty or Decimal("0")
        lot.sell_fill_price = fill_price
        lot.sell_filled_qty = qty
        lot.sell_filled_at = str(order.get("filled_at") or datetime.now(UTC).isoformat())
        lot.realized_pnl = (lot.realized_pnl or Decimal("0")) + (fill_price - buy_price) * qty
        lot.status = "closed"
    elif status in INACTIVE_ORDER_STATUSES:
        if filled_qty > 0:
            lot.sell_fill_price = fill_price
            lot.realized_pnl = (lot.realized_pnl or Decimal("0")) + (
                fill_price - buy_price
            ) * filled_qty
            lot.qty = max(Decimal("0"), (lot.qty or Decimal("0")) - filled_qty)
        lot.sell_filled_qty = None
        lot.status = "open" if (lot.qty or Decimal("0")) > 0 else "closed"
        lot.sell_order_id = None
        lot.sell_client_order_id = None
        lot.sell_submitted_at = None
        lot.notes.append(f"Sell order became {status}; lot returned to open")
    else:
        return {
            "lot_id": lot.lot_id,
            "level_index": lot.level_index,
            "side": "sell",
            "order_status": status,
            "filled_qty": format_decimal(filled_qty),
        }
    return {
        "lot_id": lot.lot_id,
        "level_index": lot.level_index,
        "side": "sell",
        "order_status": status,
        "lot_status": lot.status,
        "filled_qty": format_decimal(filled_qty),
        "fill_price": format_decimal(fill_price),
        "realized_pnl": format_decimal(lot.realized_pnl or Decimal("0")),
    }


def _submit_grid_intents(
    *,
    alpaca: AlpacaClient,
    state,
    strategy_config: GridStrategyConfig,
    intents: list[GridIntent],
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    submitted: list[dict[str, Any]] = []
    for sequence, intent in enumerate(intents, start=1):
        if intent.action == "buy":
            client_order_id = build_client_order_id("grid-buy", strategy_config.symbol, sequence)
            payload = _equity_limit_order_payload(
                symbol=strategy_config.symbol,
                side="buy",
                qty=intent.qty,
                limit_price=intent.price,
                client_order_id=client_order_id,
                time_in_force="day",
            )
            order = alpaca.submit_order(payload)
            now = datetime.now(UTC).isoformat()
            lot = lot_from_buy_intent(
                intent,
                lot_id=f"grid-{uuid4().hex[:12]}",
                created_at=now,
                status="buy_submitted",
                buy_order_id=str(order.get("id") or ""),
                buy_client_order_id=client_order_id,
            )
            state.lots.append(lot)
            submitted.append(_submitted_order_record(intent, payload, order, lot.lot_id))
            logger.info("Submitted grid buy order lot=%s payload=%s", lot.lot_id, payload)
        elif intent.action == "sell" and intent.lot_id:
            lot = _find_lot(state.lots, intent.lot_id)
            if lot is None:
                logger.warning("Skipping sell intent for missing lot_id=%s", intent.lot_id)
                continue
            client_order_id = build_client_order_id("grid-sell", strategy_config.symbol, sequence)
            payload = _equity_limit_order_payload(
                symbol=strategy_config.symbol,
                side="sell",
                qty=intent.qty,
                limit_price=intent.price,
                client_order_id=client_order_id,
                time_in_force="gtc",
            )
            order = alpaca.submit_order(payload)
            lot.status = "sell_submitted"
            lot.sell_order_id = str(order.get("id") or "")
            lot.sell_client_order_id = client_order_id
            lot.sell_submitted_at = datetime.now(UTC).isoformat()
            submitted.append(_submitted_order_record(intent, payload, order, lot.lot_id))
            logger.info("Submitted grid sell order lot=%s payload=%s", lot.lot_id, payload)
    return submitted


def _equity_limit_order_payload(
    *,
    symbol: str,
    side: str,
    qty: Decimal,
    limit_price: Decimal,
    client_order_id: str,
    time_in_force: str,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "qty": format_decimal(qty),
        "side": side,
        "type": "limit",
        "time_in_force": time_in_force,
        "limit_price": format(limit_price.quantize(Decimal("0.01")), "f"),
        "client_order_id": client_order_id,
    }


def _submitted_order_record(
    intent: GridIntent,
    payload: dict[str, Any],
    order: dict[str, Any],
    lot_id: str,
) -> dict[str, Any]:
    return {
        "lot_id": lot_id,
        "action": intent.action,
        "level_index": intent.level_index,
        "payload": payload,
        "broker_order_id": order.get("id"),
        "broker_status": order.get("status"),
    }


def _find_lot(lots: list[GridLotState], lot_id: str) -> GridLotState | None:
    for lot in lots:
        if lot.lot_id == lot_id:
            return lot
    return None


def _market_is_open(args: argparse.Namespace, alpaca: AlpacaClient, logger: logging.Logger) -> bool:
    if args.ignore_market_hours:
        return True
    try:
        clock = alpaca.get_clock()
    except Exception as exc:  # noqa: BLE001 - fail closed for order submission
        logger.warning("Could not read Alpaca clock; treating market as closed: %s", exc)
        return False
    return bool(clock.get("is_open"))


def _execution_allowed(
    *,
    args: argparse.Namespace,
    config: AppConfig,
    kill_switch_active: bool,
    market_open: bool,
    safety_errors: list[str],
) -> tuple[bool, str]:
    if not args.submit_paper:
        return False, "preview_only"
    if safety_errors:
        return False, "broker and local grid state do not match"
    if config.mode != "paper":
        return False, "config mode is not paper"
    if not bool(config.get("execution", "enable_paper_orders", default=False)):
        return False, "execution.enable_paper_orders is false"
    if bool(config.get("execution", "no_market_orders", default=True)) is False:
        return False, "execution.no_market_orders must stay true for grid v1"
    if kill_switch_active:
        return False, "kill switch is active"
    if not market_open:
        return False, "market is closed"
    return True, "paper order submission enabled"


def _grid_state_path(args: argparse.Namespace, config: AppConfig):
    return resolve_path(
        args.state_path
        or config.get("storage", "grid_state_path", default="data/grid/grid_state.json")
    )


def _grid_timeframe(args: argparse.Namespace, config: AppConfig) -> str:
    return str(args.timeframe or config.get("runtime", "grid_timeframe", default="5Min"))


def _grid_scheduler_interval_minutes(args: argparse.Namespace, config: AppConfig) -> float:
    if args.interval_minutes is not None:
        return float(args.interval_minutes)
    return float(config.get("runtime", "grid_scheduler_interval_minutes", default=1))


def _grid_scheduler_cycle_args(args: argparse.Namespace) -> argparse.Namespace:
    cycle_args = argparse.Namespace(**vars(args))
    cycle_args.command = "grid-cycle"
    if args.json_output_dir:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_dir = resolve_path(args.json_output_dir)
        cycle_args.json_output = str(output_dir / f"grid_cycle_{timestamp}.json")
    return cycle_args


def _next_grid_buy_level(
    state: GridState, strategy_config: GridStrategyConfig
) -> dict[str, Any] | None:
    if state.anchor_price is None:
        return None
    active_levels = state.active_level_indexes()
    spacing = strategy_config.grid_spacing_pct / Decimal("100")
    for level_index in range(1, strategy_config.max_buy_levels_below_anchor + 1):
        if level_index in active_levels:
            continue
        return {
            "level_index": level_index,
            "price": format_decimal(
                buy_price_for_level(state.anchor_price, spacing, level_index)
            ),
        }
    return None


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


def _log_grid_cycle_summary(logger: logging.Logger, artifact: dict[str, Any]) -> None:
    plan = artifact["plan"]
    submitted = artifact["submitted_orders"]
    state = artifact["state"]
    logger.info(
        "Grid cycle complete: symbol=%s close=%s intents=%s submitted=%s active_lots=%s blocked=%s",
        artifact["symbol"],
        artifact["bar"]["close"],
        len(plan["intents"]),
        len(submitted),
        state["active_lot_count"],
        len(plan["blocked"]),
    )

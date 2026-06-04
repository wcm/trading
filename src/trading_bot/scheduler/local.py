from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, date, datetime, time as dt_time
from pathlib import Path

from trading_bot.app import bootstrap
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.cycles.run_cycle import (
    _close_recommended_spreads,
    _log_position_monitor_result,
    _maybe_execute_recommended_closes,
    run_cycle,
)
from trading_bot.monitoring.positions import monitor_put_credit_spreads
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.notifications.messages import (
    _send_daily_trading_summary,
    _send_order_poll_summary,
    _send_position_monitor_summary,
    _send_scheduler_error,
    _send_scheduler_heartbeat,
)
from trading_bot.orders.lifecycle import _poll_order_status_changes
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.storage.db import record_bot_run
from trading_bot.summaries.daily import _build_daily_trading_summary, _daily_summary_bot_run_status
from trading_bot.utils.artifacts import write_json_artifact
from trading_bot.utils.market_time import EASTERN
from trading_bot.config import resolve_path


def run_local_scheduler(args: argparse.Namespace) -> int:
    config, logger, db_path, kill_switch, notifier = bootstrap(args)
    interval_minutes = _scheduler_interval_minutes(args, config)
    open_interval_minutes = _scheduler_open_interval_minutes(args, config)
    heartbeat_minutes = _scheduler_heartbeat_minutes(args, config)
    try:
        daily_summary_time = _scheduler_daily_summary_time_et(args, config)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    if interval_minutes <= 0:
        logger.error("Scheduler interval must be positive: %s", interval_minutes)
        return 1
    if open_interval_minutes <= 0:
        logger.error("Scheduler open interval must be positive: %s", open_interval_minutes)
        return 1
    if heartbeat_minutes < 0:
        logger.error("Scheduler heartbeat interval cannot be negative: %s", heartbeat_minutes)
        return 1

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    started_at = datetime.now(UTC).isoformat()
    logger.info(
        (
            "Starting local scheduler interval_minutes=%s open_interval_minutes=%s "
            "heartbeat_minutes=%s once=%s ignore_market_hours=%s"
        ),
        interval_minutes,
        open_interval_minutes,
        heartbeat_minutes,
        args.once,
        args.ignore_market_hours,
    )
    record_bot_run(
        db_path,
        started_at=started_at,
        mode=config.mode,
        status="started",
        details={
            "command": "schedule-local",
            "interval_minutes": interval_minutes,
            "open_interval_minutes": open_interval_minutes,
            "heartbeat_minutes": heartbeat_minutes,
            "daily_summary_time_et": daily_summary_time.strftime("%H:%M"),
            "once": args.once,
            "ignore_market_hours": args.ignore_market_hours,
            "send_cycle_discord": args.send_cycle_discord,
            "skip_order_poll": args.skip_order_poll,
            "skip_daily_summary": args.skip_daily_summary,
        },
    )

    if args.send_discord:
        _send_scheduler_heartbeat(
            notifier,
            logger,
            status="started",
            interval_minutes=interval_minutes,
            heartbeat_minutes=heartbeat_minutes,
            details=(
                f"open_interval={open_interval_minutes:g}m cycle_discord={args.send_cycle_discord} "
                f"daily_summary={not args.skip_daily_summary} once={args.once}"
            ),
        )

    cycle_count = 0
    monitor_count = 0
    skipped_count = 0
    order_change_count = 0
    daily_summary_count = 0
    error_count = 0
    last_status = "started"
    next_heartbeat_at = time.monotonic() + heartbeat_minutes * 60 if heartbeat_minutes > 0 else None
    next_open_decision_at = 0.0
    daily_summary_sent_dates: set[str] = set()
    close_alerted_spread_ids: set[str] = set()

    try:
        while True:
            now = datetime.now(UTC)
            tick_started = time.monotonic()
            try:
                clock = alpaca.get_clock()
            except Exception as exc:  # noqa: BLE001 - scheduler should keep supervising after transient API errors
                error_count += 1
                last_status = f"clock_error: {exc}"
                logger.exception("Scheduler Alpaca clock check failed: %s", exc)
                if args.send_discord:
                    _send_scheduler_error(notifier, logger, phase="clock", error=str(exc))
                if args.once:
                    return 1
            else:
                market_open = args.ignore_market_hours or bool(clock.get("is_open"))
                if market_open:
                    positions: list[dict[str, Any]] = []
                    position_error = None
                    try:
                        positions = alpaca.get_positions()
                    except Exception as exc:  # noqa: BLE001 - scheduler should fail closed on state uncertainty
                        position_error = str(exc)
                        error_count += 1
                        last_status = f"position_check_error: {exc}"
                        logger.exception("Scheduler position check failed: %s", exc)
                        if args.send_discord:
                            _send_scheduler_error(notifier, logger, phase="positions", error=str(exc))

                    open_due = tick_started >= next_open_decision_at
                    if positions and not open_due:
                        try:
                            monitor_artifact = _run_scheduler_position_monitor(
                                args=args,
                                config=config,
                                logger=logger,
                                db_path=db_path,
                                alpaca=alpaca,
                                kill_switch=kill_switch,
                                notifier=notifier,
                                positions=positions,
                            )
                        except Exception as exc:  # noqa: BLE001 - scheduler should keep supervising
                            error_count += 1
                            last_status = f"monitor_error: {exc}"
                            logger.exception("Scheduler position monitor failed: %s", exc)
                            if args.send_discord:
                                _send_scheduler_error(notifier, logger, phase="monitor-positions", error=str(exc))
                        else:
                            monitor_count += 1
                            close_spreads = _close_recommended_spreads(monitor_artifact)
                            close_ids = {str(spread.get("spread_id")) for spread in close_spreads}
                            new_close_ids = close_ids - close_alerted_spread_ids
                            if args.send_discord and new_close_ids:
                                _send_position_monitor_summary(notifier, monitor_artifact, logger)
                            close_alerted_spread_ids.update(close_ids)
                            if not close_ids:
                                close_alerted_spread_ids.clear()
                            last_status = (
                                f"monitor_ok spreads={monitor_artifact.get('spread_count')} "
                                f"close_recommended={len(close_spreads)}"
                            )
                    elif not positions and not position_error:
                        close_alerted_spread_ids.clear()

                    if open_due:
                        next_open_decision_at = tick_started + open_interval_minutes * 60
                        if position_error:
                            logger.warning("Skipping new open decision because position check failed")
                        else:
                            cycle_count += 1
                            if positions:
                                monitor_count += 1
                            cycle_json_output = _scheduler_cycle_json_output(args, now)
                            cycle_args = _scheduler_cycle_args(args, cycle_json_output)
                            logger.info(
                                "Scheduler running open decision cycle %s json_output=%s cycle_discord=%s",
                                cycle_count,
                                cycle_json_output or "-",
                                args.send_cycle_discord,
                            )
                            cycle_code = run_cycle(cycle_args)
                            if cycle_code == 0:
                                last_status = f"open_cycle_ok count={cycle_count}"
                            else:
                                error_count += 1
                                last_status = f"open_cycle_failed code={cycle_code}"
                                if args.send_discord:
                                    _send_scheduler_error(
                                        notifier,
                                        logger,
                                        phase="run-cycle",
                                        error=f"run-cycle exited with code {cycle_code}",
                                    )
                else:
                    skipped_count += 1
                    last_status = (
                        f"market_closed next_open={clock.get('next_open')} next_close={clock.get('next_close')}"
                    )
                    logger.info("Scheduler skipped cycle because Alpaca market is closed: %s", last_status)

                order_poll = None
                if not args.skip_order_poll:
                    try:
                        order_poll = _poll_order_status_changes(
                            config=config,
                            logger=logger,
                            db_path=db_path,
                            alpaca=alpaca,
                            status="all",
                            limit=_scheduler_order_poll_limit(args, config),
                        )
                    except Exception as exc:  # noqa: BLE001 - scheduler should keep supervising
                        error_count += 1
                        last_status = f"order_poll_error: {exc}"
                        logger.exception("Scheduler order lifecycle poll failed: %s", exc)
                        if args.send_discord:
                            _send_scheduler_error(notifier, logger, phase="poll-orders", error=str(exc))
                    else:
                        order_change_count += int(order_poll.get("change_count") or 0)
                        if args.send_discord and order_poll.get("changes"):
                            _send_order_poll_summary(notifier, order_poll, logger)

                summary_date = _scheduler_daily_summary_due_date(
                    now,
                    daily_summary_time=daily_summary_time,
                    sent_dates=daily_summary_sent_dates,
                )
                if (
                    summary_date is not None
                    and not args.skip_daily_summary
                    and not market_open
                    and (args.send_discord or args.json_output_dir)
                ):
                    try:
                        daily_summary = _build_daily_trading_summary(
                            config=config,
                            db_path=db_path,
                            alpaca=alpaca,
                            option_feed=args.option_feed,
                            summary_date=summary_date,
                            order_poll=order_poll if not args.skip_order_poll else None,
                        )
                    except Exception as exc:  # noqa: BLE001 - scheduler should keep supervising
                        error_count += 1
                        last_status = f"daily_summary_error: {exc}"
                        logger.exception("Scheduler daily summary failed: %s", exc)
                        if args.send_discord:
                            _send_scheduler_error(notifier, logger, phase="daily-summary", error=str(exc))
                    else:
                        summary_path = _scheduler_daily_summary_json_output(args, summary_date)
                        if summary_path:
                            write_json_artifact(summary_path, daily_summary, logger, "daily trading summary")
                        discord_ok = True
                        if args.send_discord:
                            discord_ok = _send_daily_trading_summary(notifier, daily_summary, logger)
                        daily_summary_sent_dates.add(summary_date.isoformat())
                        daily_summary_count += 1
                        summary_status = _daily_summary_bot_run_status(args.send_discord, discord_ok)
                        last_status = f"{summary_status} date={summary_date.isoformat()}"
                        record_bot_run(
                            db_path,
                            started_at=datetime.now(UTC).isoformat(),
                            mode=config.mode,
                            status=summary_status,
                            details={
                                "command": "daily-summary",
                                "summary_date": summary_date.isoformat(),
                                "source": "schedule-local",
                                "discord_requested": args.send_discord,
                                "discord_ok": discord_ok,
                            },
                        )
                        if not discord_ok:
                            error_count += 1

            if args.once:
                break

            if (
                args.send_discord
                and next_heartbeat_at is not None
                and time.monotonic() >= next_heartbeat_at
            ):
                _send_scheduler_heartbeat(
                    notifier,
                    logger,
                    status="running",
                    interval_minutes=interval_minutes,
                    heartbeat_minutes=heartbeat_minutes,
                    details=(
                        f"{last_status}; open_cycles={cycle_count}; monitors={monitor_count}; "
                        f"skipped={skipped_count}; order_changes={order_change_count}; "
                        f"daily_summaries={daily_summary_count}; errors={error_count}"
                    ),
                )
                next_heartbeat_at = time.monotonic() + heartbeat_minutes * 60

            time.sleep(interval_minutes * 60)
    except KeyboardInterrupt:
        last_status = "stopped_by_keyboard_interrupt"
        logger.info("Local scheduler stopped by keyboard interrupt")

    final_status = "ok" if error_count == 0 else "completed_with_errors"
    if args.once:
        final_status = last_status if error_count else "ok"
    record_bot_run(
        db_path,
        started_at=datetime.now(UTC).isoformat(),
        mode=config.mode,
        status=final_status,
        details={
            "command": "schedule-local",
            "open_cycles": cycle_count,
            "monitors": monitor_count,
            "skipped": skipped_count,
            "order_changes": order_change_count,
            "daily_summaries": daily_summary_count,
            "errors": error_count,
            "last_status": last_status,
        },
    )
    if args.send_discord:
        _send_scheduler_heartbeat(
            notifier,
            logger,
            status="stopped" if not args.once else "completed once",
            interval_minutes=interval_minutes,
            heartbeat_minutes=heartbeat_minutes,
            details=(
                f"{last_status}; open_cycles={cycle_count}; monitors={monitor_count}; "
                f"skipped={skipped_count}; order_changes={order_change_count}; "
                f"daily_summaries={daily_summary_count}; errors={error_count}"
            ),
        )
    return 0 if error_count == 0 else 1


def _run_scheduler_position_monitor(
    *,
    args: argparse.Namespace,
    config,
    logger: logging.Logger,
    db_path: Path,
    alpaca: AlpacaClient,
    kill_switch: KillSwitch,
    notifier: DiscordNotifier,
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    result = monitor_put_credit_spreads(
        config=config,
        alpaca=alpaca,
        positions=positions,
        option_feed=args.option_feed,
    )
    _log_position_monitor_result(logger, result)
    artifact = result.to_dict()
    close_execution_attempts = []
    if args.submit_paper_close and _close_recommended_spreads(artifact):
        close_execution_attempts = _maybe_execute_recommended_closes(
            config=config,
            logger=logger,
            db_path=db_path,
            alpaca=alpaca,
            kill_switch=kill_switch,
            notifier=notifier,
            monitor_artifact=artifact,
            submit_requested=True,
        )
    artifact["close_execution_attempts"] = close_execution_attempts
    return artifact


def _scheduler_interval_minutes(args: argparse.Namespace, config) -> float:
    if args.interval_minutes is not None:
        return float(args.interval_minutes)
    return float(config.get("runtime", "scheduler_interval_minutes", default=1))


def _scheduler_open_interval_minutes(args: argparse.Namespace, config) -> float:
    if args.open_interval_minutes is not None:
        return float(args.open_interval_minutes)
    return float(config.get("runtime", "scheduler_open_interval_minutes", default=5))


def _scheduler_heartbeat_minutes(args: argparse.Namespace, config) -> float:
    if args.heartbeat_minutes is not None:
        return float(args.heartbeat_minutes)
    return float(config.get("runtime", "scheduler_heartbeat_minutes", default=60))


def _scheduler_order_poll_limit(args: argparse.Namespace, config) -> int:
    if args.order_poll_limit is not None:
        return int(args.order_poll_limit)
    return int(config.get("runtime", "scheduler_order_poll_limit", default=50))


def _scheduler_daily_summary_time_et(args: argparse.Namespace, config) -> dt_time:
    value = args.daily_summary_time_et or str(
        config.get("runtime", "scheduler_daily_summary_time_et", default="16:05")
    )
    return _parse_hhmm_time(value)


def _parse_hhmm_time(value: str) -> dt_time:
    try:
        hour_raw, minute_raw = value.split(":", maxsplit=1)
        return dt_time(hour=int(hour_raw), minute=int(minute_raw))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Expected HH:MM time, got {value!r}") from exc


def _scheduler_daily_summary_due_date(
    now: datetime,
    *,
    daily_summary_time: dt_time,
    sent_dates: set[str],
) -> date | None:
    now_et = now.astimezone(EASTERN)
    summary_date = now_et.date()
    if summary_date.weekday() >= 5:
        return None
    if summary_date.isoformat() in sent_dates:
        return None
    if now_et.time() < daily_summary_time:
        return None
    return summary_date


def _scheduler_cycle_json_output(args: argparse.Namespace, now: datetime) -> str | None:
    if not args.json_output_dir:
        return None
    output_dir = resolve_path(args.json_output_dir)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    return str(output_dir / f"run_cycle_{timestamp}.json")


def _scheduler_daily_summary_json_output(args: argparse.Namespace, summary_date: date) -> str | None:
    if not args.json_output_dir:
        return None
    output_dir = resolve_path(args.json_output_dir)
    return str(output_dir / f"daily_summary_{summary_date.isoformat()}.json")


def _scheduler_cycle_args(args: argparse.Namespace, json_output: str | None) -> argparse.Namespace:
    return argparse.Namespace(
        command="run-cycle",
        settings=args.settings,
        env=args.env,
        symbols=args.symbols,
        max_candidates=args.max_candidates,
        option_feed=args.option_feed,
        send_discord=args.send_cycle_discord,
        json_output=json_output,
        mock_decision=args.mock_decision,
        submit_paper=args.submit_paper,
        submit_paper_close=args.submit_paper_close,
    )

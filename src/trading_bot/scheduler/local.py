from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, date, datetime, time as dt_time
from pathlib import Path
from typing import Any

from trading_bot.app import bootstrap
from trading_bot.brokers.alpaca import AlpacaClient, AlpacaCredentialsError
from trading_bot.config import resolve_path
from trading_bot.cycles.open_discovery import (
    _close_recommended_spreads,
    _log_position_monitor_result,
    _maybe_execute_recommended_closes,
    run_open_discovery_cycle,
)
from trading_bot.monitoring.positions import monitor_put_credit_spreads
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.notifications.messages import (
    _send_daily_trading_summary,
    _send_order_poll_summary,
    _send_position_monitor_summary,
    _send_scheduler_error,
)
from trading_bot.orders.lifecycle import _poll_order_status_changes
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.storage.db import record_bot_run
from trading_bot.summaries.daily import _build_daily_trading_summary, _daily_summary_bot_run_status
from trading_bot.utils.artifacts import write_json_artifact
from trading_bot.utils.market_time import EASTERN


def run_local_scheduler(args: argparse.Namespace) -> int:
    config, logger, db_path, kill_switch, notifier = bootstrap(args)
    interval_minutes = _scheduler_interval_minutes(args, config)
    open_interval_minutes = _scheduler_open_interval_minutes(args, config)
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

    try:
        alpaca = AlpacaClient.from_config(config)
    except AlpacaCredentialsError as exc:
        logger.error("%s", exc)
        return 1

    started_at = datetime.now(UTC).isoformat()
    logger.info(
        (
            "Starting local scheduler interval_minutes=%s open_interval_minutes=%s "
            "once=%s ignore_market_hours=%s"
        ),
        interval_minutes,
        open_interval_minutes,
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
            "daily_summary_time_et": daily_summary_time.strftime("%H:%M"),
            "once": args.once,
            "ignore_market_hours": args.ignore_market_hours,
            "send_cycle_discord": args.send_cycle_discord,
            "skip_order_poll": args.skip_order_poll,
            "skip_daily_summary": args.skip_daily_summary,
        },
    )

    cycle_count = 0
    monitor_count = 0
    skipped_count = 0
    order_change_count = 0
    daily_summary_count = 0
    error_count = 0
    last_status = "started"
    next_open_decision_at = 0.0
    daily_summary_sent_dates: set[str] = set()
    close_alerted_spread_ids: set[str] = set()

    try:
        while True:
            now = datetime.now(UTC)
            tick_started = time.monotonic()
            sleep_seconds = interval_minutes * 60
            closed_market_sleep_target = None
            closed_market_sleep_reason = None
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
                    monitor_error = None
                    monitor_status = None
                    if positions:
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
                            monitor_error = str(exc)
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
                            monitor_status = (
                                f"monitor_ok spreads={monitor_artifact.get('spread_count')} "
                                f"close_recommended={len(close_spreads)}"
                            )
                            last_status = monitor_status
                    elif not positions and not position_error:
                        close_alerted_spread_ids.clear()

                    if open_due:
                        next_open_decision_at = tick_started + open_interval_minutes * 60
                        if position_error or monitor_error:
                            reason = (
                                "position check failed"
                                if position_error
                                else "position monitor failed"
                            )
                            logger.warning("Skipping new open decision because %s", reason)
                        else:
                            cycle_count += 1
                            cycle_json_output = _scheduler_cycle_json_output(args, now)
                            cycle_args = _scheduler_cycle_args(args, cycle_json_output)
                            logger.info(
                                "Scheduler running new-open discovery cycle %s json_output=%s cycle_discord=%s",
                                cycle_count,
                                cycle_json_output or "-",
                                args.send_cycle_discord,
                            )
                            cycle_code = run_open_discovery_cycle(cycle_args)
                            if cycle_code == 0:
                                open_status = f"open_discovery_ok count={cycle_count}"
                                last_status = f"{monitor_status}; {open_status}" if monitor_status else open_status
                            else:
                                error_count += 1
                                last_status = f"open_discovery_failed code={cycle_code}"
                                if args.send_discord:
                                    _send_scheduler_error(
                                        notifier,
                                        logger,
                                        phase="open-discovery",
                                        error=f"open discovery exited with code {cycle_code}",
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

                if not args.once and not market_open and not args.ignore_market_hours:
                    closed_market_sleep_target, closed_market_sleep_reason = _scheduler_closed_market_sleep_target(
                        now=now,
                        clock=clock,
                        daily_summary_time=daily_summary_time,
                        sent_dates=daily_summary_sent_dates,
                        daily_summary_enabled=(
                            not args.skip_daily_summary and (args.send_discord or bool(args.json_output_dir))
                        ),
                    )
                    if closed_market_sleep_target is not None:
                        sleep_seconds = max(
                            1.0,
                            (closed_market_sleep_target - datetime.now(UTC)).total_seconds(),
                        )
                        last_status = (
                            f"{last_status}; sleeping_until={closed_market_sleep_target.isoformat()} "
                            f"reason={closed_market_sleep_reason}"
                        )
                        logger.info(
                            "Scheduler sleeping while market is closed until %s reason=%s sleep_seconds=%.1f",
                            closed_market_sleep_target.isoformat(),
                            closed_market_sleep_reason,
                            sleep_seconds,
                        )

            if args.once:
                break

            time.sleep(sleep_seconds)
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


def _scheduler_closed_market_sleep_target(
    *,
    now: datetime,
    clock: dict,
    daily_summary_time: dt_time,
    sent_dates: set[str],
    daily_summary_enabled: bool,
) -> tuple[datetime | None, str | None]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    next_open = _parse_clock_datetime(clock.get("next_open"))
    pending_summary_at = (
        _scheduler_pending_daily_summary_at(
            now,
            daily_summary_time=daily_summary_time,
            sent_dates=sent_dates,
        )
        if daily_summary_enabled
        else None
    )
    if pending_summary_at is not None:
        if pending_summary_at <= now:
            return None, "daily_summary_due"
        if next_open is None or pending_summary_at < next_open:
            return pending_summary_at, "daily_summary"

    if next_open is None or next_open <= now:
        return None, "interval"
    return next_open, "next_open"


def _scheduler_pending_daily_summary_at(
    now: datetime,
    *,
    daily_summary_time: dt_time,
    sent_dates: set[str],
) -> datetime | None:
    now_et = now.astimezone(EASTERN)
    summary_date = now_et.date()
    if summary_date.weekday() >= 5:
        return None
    if summary_date.isoformat() in sent_dates:
        return None
    return datetime.combine(summary_date, daily_summary_time, tzinfo=EASTERN).astimezone(UTC)


def _parse_clock_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(UTC)


def _scheduler_cycle_json_output(args: argparse.Namespace, now: datetime) -> str | None:
    if not args.json_output_dir:
        return None
    output_dir = resolve_path(args.json_output_dir)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    return str(output_dir / f"open_discovery_{timestamp}.json")


def _scheduler_daily_summary_json_output(args: argparse.Namespace, summary_date: date) -> str | None:
    if not args.json_output_dir:
        return None
    output_dir = resolve_path(args.json_output_dir)
    return str(output_dir / f"daily_summary_{summary_date.isoformat()}.json")


def _scheduler_cycle_args(args: argparse.Namespace, json_output: str | None) -> argparse.Namespace:
    return argparse.Namespace(
        command="open-discovery-cycle",
        settings=args.settings,
        env=args.env,
        symbols=args.symbols,
        max_candidates=args.max_candidates,
        option_feed=args.option_feed,
        send_discord=args.send_cycle_discord,
        discord_summary_only=args.cycle_summary_only,
        json_output=json_output,
        mock_decision=args.mock_decision,
        submit_paper=args.submit_paper,
        submit_paper_close=args.submit_paper_close,
    )

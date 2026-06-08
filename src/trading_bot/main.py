from __future__ import annotations

from trading_bot.app import bootstrap
from trading_bot.cli.parser import build_parser
from trading_bot.commands.basic import (
    _maybe_check_alpaca,
    run_decision,
    run_option_scan,
    run_position_monitor,
    run_smoke,
    run_watchlist_decisions,
)
from trading_bot.cycles.open_discovery import (
    _build_decision_artifact,
    _build_watchlist_decision_run,
    _close_recommended_spreads,
    _compact_watchlist_artifact,
    _get_llm_or_mock_decision,
    _load_prompt_text,
    _maybe_execute_recommended_closes,
    _mock_decision,
    _selected_candidate_from_packet,
    _selected_order_preview,
)
from trading_bot.notifications.messages import (
    _close_execution_status_lines,
    _execution_status,
    _maybe_send_discord,
    _preview_status,
    _send_daily_trading_summary,
    _send_decision_summary,
    _send_discord_messages,
    _send_order_poll_summary,
    _send_position_monitor_summary,
    _send_scan_summary,
    _send_scheduler_error,
    _send_scheduler_heartbeat,
    _send_watchlist_decision_summary,
    _split_discord_content,
    _watchlist_decision_detail_messages,
)
from trading_bot.orders.lifecycle import _poll_order_status_changes, run_order_poll
from trading_bot.scheduler.local import (
    _parse_hhmm_time,
    _run_scheduler_position_monitor,
    _scheduler_cycle_args,
    _scheduler_cycle_json_output,
    _scheduler_daily_summary_due_date,
    _scheduler_daily_summary_json_output,
    _scheduler_daily_summary_time_et,
    _scheduler_heartbeat_minutes,
    _scheduler_interval_minutes,
    _scheduler_open_interval_minutes,
    _scheduler_order_poll_limit,
    run_local_scheduler,
)
from trading_bot.summaries.daily import (
    _build_daily_trading_summary,
    _daily_account_summary,
    _daily_summary_bot_run_status,
    run_daily_summary,
)
from trading_bot.utils.artifacts import write_json_artifact as _write_json_artifact
from trading_bot.utils.market_time import daily_summary_date_from_arg as _daily_summary_date_from_arg
from trading_bot.utils.market_time import eastern_date_window_utc as _eastern_date_window_utc
from trading_bot.utils.money import decimal_or_none as _decimal_or_none
from trading_bot.utils.money import format_counts as _format_counts
from trading_bot.utils.money import format_optional_decimal as _format_optional_decimal
from trading_bot.utils.money import sum_decimal_strings as _sum_decimal_strings
from trading_bot.utils.symbols import symbols_from_args_or_config as _symbols_from_args_or_config
from trading_bot.utils.symbols import watchlist_symbols_from_args_or_config as _watchlist_symbols_from_args_or_config


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "smoke"
        args.send_discord = False
        args.check_alpaca = False

    if args.command == "smoke":
        return run_smoke(args)
    if args.command == "scan-options":
        return run_option_scan(args)
    if args.command == "decide":
        return run_decision(args)
    if args.command == "decide-watchlist":
        return run_watchlist_decisions(args)
    if args.command == "monitor-positions":
        return run_position_monitor(args)
    if args.command == "poll-orders":
        return run_order_poll(args)
    if args.command == "daily-summary":
        return run_daily_summary(args)
    if args.command == "schedule-local":
        return run_local_scheduler(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

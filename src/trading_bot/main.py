from __future__ import annotations

from trading_bot.cli.parser import build_parser
from trading_bot.commands.basic import (
    run_decision,
    run_option_scan,
    run_position_monitor,
    run_smoke,
    run_watchlist_decisions,
)
from trading_bot.commands.backtesting import run_grid_backtest_command, run_grid_sweep_command
from trading_bot.commands.grid import run_grid_cycle_command, run_grid_scheduler_command
from trading_bot.orders.lifecycle import run_order_poll
from trading_bot.scheduler.local import run_local_scheduler
from trading_bot.summaries.daily import run_daily_summary


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
    if args.command == "backtest-grid":
        return run_grid_backtest_command(args)
    if args.command == "sweep-grid":
        return run_grid_sweep_command(args)
    if args.command == "grid-cycle":
        return run_grid_cycle_command(args)
    if args.command == "grid-schedule-local":
        return run_grid_scheduler_command(args)
    if args.command == "schedule-local":
        return run_local_scheduler(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

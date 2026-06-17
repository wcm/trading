from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-bot")
    parser.add_argument(
        "--settings",
        default="config/settings.yaml",
        help="Path to settings YAML file.",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to local environment file.",
    )

    subparsers = parser.add_subparsers(dest="command")
    smoke = subparsers.add_parser("smoke", help="Run local paper-mode smoke checks.")
    smoke.add_argument(
        "--send-discord",
        action="store_true",
        help="Send a Discord startup test message.",
    )
    smoke.add_argument(
        "--check-alpaca",
        action="store_true",
        help="Read Alpaca paper account, clock, and positions.",
    )

    scan = subparsers.add_parser("scan-options", help="Run a read-only put-spread candidate scan.")
    scan.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to strategy.preferred_symbols.",
    )
    scan.add_argument(
        "--max-candidates",
        type=int,
        default=20,
        help="Maximum candidates to log and store.",
    )
    scan.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this scan.",
    )
    scan.add_argument(
        "--send-discord",
        action="store_true",
        help="Send a Discord scan summary.",
    )
    scan.add_argument(
        "--json-output",
        help="Optional path to write the full scan result JSON.",
    )

    decide = subparsers.add_parser("decide", help="Run read-only scan plus LLM decision.")
    decide.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to strategy.preferred_symbols.",
    )
    decide.add_argument(
        "--max-candidates",
        type=int,
        default=5,
        help="Maximum candidates to pass to the LLM.",
    )
    decide.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this decision.",
    )
    decide.add_argument(
        "--send-discord",
        action="store_true",
        help="Send a Discord LLM decision summary.",
    )
    decide.add_argument(
        "--json-output",
        help="Optional path to write the full decision artifact JSON.",
    )
    decide.add_argument(
        "--mock-decision",
        choices=["skip", "disable_trading"],
        help="Use a local mock decision instead of calling OpenAI.",
    )

    decide_watchlist = subparsers.add_parser(
        "decide-watchlist",
        help="Run independent read-only LLM decisions for each watchlist symbol.",
    )
    decide_watchlist.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to strategy.watchlist.",
    )
    decide_watchlist.add_argument(
        "--max-candidates",
        type=int,
        default=20,
        help="Maximum candidates to pass to each per-symbol LLM decision.",
    )
    decide_watchlist.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this decision run.",
    )
    decide_watchlist.add_argument(
        "--send-discord",
        action="store_true",
        help="Send one Discord summary for the watchlist decision run.",
    )
    decide_watchlist.add_argument(
        "--json-output",
        help="Optional path to write the combined watchlist decision artifact JSON.",
    )
    decide_watchlist.add_argument(
        "--mock-decision",
        choices=["skip", "disable_trading"],
        help="Use local mock decisions instead of calling OpenAI.",
    )
    decide_watchlist.add_argument(
        "--submit-paper",
        action="store_true",
        help="Request paper order submission for the allocator-selected open. Requires execution.enable_paper_orders=true.",
    )

    monitor = subparsers.add_parser(
        "monitor-positions",
        help="Run a monitor for existing put credit spread positions.",
    )
    monitor.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this monitor run.",
    )
    monitor.add_argument(
        "--send-discord",
        action="store_true",
        help="Send a Discord position monitor summary.",
    )
    monitor.add_argument(
        "--json-output",
        help="Optional path to write the position monitor artifact JSON.",
    )
    monitor.add_argument(
        "--submit-paper-close",
        action="store_true",
        help="Request paper close order submission for recommended closes. Requires execution.enable_paper_close_orders=true.",
    )

    poll_orders = subparsers.add_parser(
        "poll-orders",
        help="Poll Alpaca order statuses and notify on lifecycle changes.",
    )
    poll_orders.add_argument(
        "--status",
        choices=["open", "closed", "all"],
        default="all",
        help="Alpaca order status filter.",
    )
    poll_orders.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum Alpaca orders to poll.",
    )
    poll_orders.add_argument(
        "--send-discord",
        action="store_true",
        help="Send Discord notifications for detected order changes.",
    )
    poll_orders.add_argument(
        "--notify-no-changes",
        action="store_true",
        help="Send a Discord message even when no order changes were detected.",
    )
    poll_orders.add_argument(
        "--json-output",
        help="Optional path to write the order poll artifact JSON.",
    )

    daily_summary = subparsers.add_parser(
        "daily-summary",
        help="Build a trading-focused daily summary.",
    )
    daily_summary.add_argument(
        "--summary-date",
        help="US/Eastern summary date in YYYY-MM-DD format. Defaults to today in US/Eastern time.",
    )
    daily_summary.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for open-position marking.",
    )
    daily_summary.add_argument(
        "--send-discord",
        action="store_true",
        help="Send the daily summary to Discord.",
    )
    daily_summary.add_argument(
        "--json-output",
        help="Optional path to write the daily summary artifact JSON.",
    )

    backtest_grid = subparsers.add_parser(
        "backtest-grid",
        help="Run a deterministic TQQQ-style grid backtest.",
    )
    _add_grid_backtest_common_args(backtest_grid)
    backtest_grid.add_argument(
        "--json-output",
        help="Optional path to write the full backtest JSON artifact.",
    )
    backtest_grid.add_argument(
        "--trades-csv",
        help="Optional path to write all simulated trades as CSV.",
    )

    sweep_grid = subparsers.add_parser(
        "sweep-grid",
        help="Run a grid parameter sweep over spacing, order size, and inventory limits.",
    )
    _add_grid_backtest_common_args(sweep_grid)
    sweep_grid.add_argument(
        "--grid-spacing-pcts",
        default="0.80,2.00,3.00,5.00,8.00",
        help="Comma-separated grid spacing percentages to test.",
    )
    sweep_grid.add_argument(
        "--base-order-notionals",
        default="500",
        help="Comma-separated per-buy dollar notionals to test.",
    )
    sweep_grid.add_argument(
        "--max-inventory-values",
        default="8000",
        help="Comma-separated max inventory values to test.",
    )
    sweep_grid.add_argument(
        "--adaptive-scale-factors",
        help="Comma-separated adaptive sizing scale factors to test. Use 0 for fixed sizing.",
    )
    sweep_grid.add_argument(
        "--csv-output",
        help="Optional path to write sweep summary CSV.",
    )
    sweep_grid.add_argument(
        "--markdown-output",
        help="Optional path to write sweep summary Markdown.",
    )
    sweep_grid.add_argument(
        "--json-output",
        help="Optional path to write full sweep JSON artifact.",
    )

    grid_cycle = subparsers.add_parser(
        "grid-cycle",
        help="Run one TQQQ grid bot cycle with optional paper order submission.",
    )
    _add_grid_cycle_common_args(grid_cycle)

    grid_scheduler = subparsers.add_parser(
        "grid-schedule-local",
        help="Run the TQQQ grid bot loop locally during market hours.",
    )
    _add_grid_cycle_common_args(grid_scheduler)
    grid_scheduler.add_argument(
        "--interval-minutes",
        type=float,
        help="Minutes between grid cycles. Defaults to runtime.grid_scheduler_interval_minutes.",
    )
    grid_scheduler.add_argument(
        "--json-output-dir",
        help="Optional directory for timestamped grid cycle JSON artifacts.",
    )
    grid_scheduler.add_argument(
        "--once",
        action="store_true",
        help="Run one scheduler check and exit. Useful for validation.",
    )

    scheduler = subparsers.add_parser(
        "schedule-local",
        help="Run position monitoring every tick and new-open discovery on a slower cadence.",
    )
    scheduler.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to strategy.watchlist.",
    )
    scheduler.add_argument(
        "--max-candidates",
        type=int,
        default=20,
        help="Maximum candidates to pass to each per-symbol LLM decision.",
    )
    scheduler.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for scheduled cycles.",
    )
    scheduler.add_argument(
        "--interval-minutes",
        type=float,
        help="Minutes to wait after each scheduler check. Defaults to runtime.scheduler_interval_minutes.",
    )
    scheduler.add_argument(
        "--open-interval-minutes",
        type=float,
        help="Minutes between new-open discovery runs. Defaults to runtime.scheduler_open_interval_minutes.",
    )
    scheduler.add_argument(
        "--send-discord",
        action="store_true",
        help="Send scheduler error, order, daily-summary, and close-recommendation messages.",
    )
    scheduler.add_argument(
        "--send-cycle-discord",
        action="store_true",
        help="Also send each new-open discovery Discord summary.",
    )
    scheduler.add_argument(
        "--cycle-summary-only",
        action="store_true",
        help="With --send-cycle-discord, send only compact cycle summaries without per-symbol details.",
    )
    scheduler.add_argument(
        "--json-output-dir",
        help="Optional directory for timestamped scheduler JSON artifacts.",
    )
    scheduler.add_argument(
        "--skip-order-poll",
        action="store_true",
        help="Do not poll recent Alpaca order statuses after each scheduler check.",
    )
    scheduler.add_argument(
        "--order-poll-limit",
        type=int,
        help="Maximum Alpaca orders to poll after each scheduler check. Defaults to runtime.scheduler_order_poll_limit.",
    )
    scheduler.add_argument(
        "--skip-daily-summary",
        action="store_true",
        help="Do not send the after-market daily trading summary.",
    )
    scheduler.add_argument(
        "--daily-summary-time-et",
        help="US/Eastern time for the daily summary in HH:MM format. Defaults to runtime.scheduler_daily_summary_time_et.",
    )
    scheduler.add_argument(
        "--mock-decision",
        choices=["skip", "disable_trading"],
        help="Use local mock decisions instead of calling OpenAI when cycles run.",
    )
    scheduler.add_argument(
        "--submit-paper",
        action="store_true",
        help="Request paper order submission for selected opens. Requires execution.enable_paper_orders=true.",
    )
    scheduler.add_argument(
        "--submit-paper-close",
        action="store_true",
        help="Request paper close order submission for recommended closes. Requires execution.enable_paper_close_orders=true.",
    )
    scheduler.add_argument(
        "--once",
        action="store_true",
        help="Run one scheduler check and exit. Useful for local validation.",
    )
    scheduler.add_argument(
        "--ignore-market-hours",
        action="store_true",
        help="Run cycles even when Alpaca reports the market is closed. Useful for mock validation only.",
    )

    return parser


def _add_grid_backtest_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--symbol",
        help="Symbol to backtest. Defaults to grid_strategy.symbol.",
    )
    parser.add_argument(
        "--timeframe",
        default="1Day",
        help="Historical bar timeframe. Use 1Day for the first daily backtest.",
    )
    parser.add_argument(
        "--start",
        default="2010-02-11",
        help="Backtest start date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end",
        default="2026-06-17",
        help="Backtest end date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--data-source",
        choices=["yahoo", "alpaca"],
        default="yahoo",
        help="Historical data source. Yahoo supports 1Day; Alpaca supports configured stock bars.",
    )
    parser.add_argument(
        "--feed",
        help="Alpaca stock data feed override when --data-source=alpaca.",
    )
    parser.add_argument(
        "--cache-dir",
        default="data/backtests/cache",
        help="Directory for cached historical bars.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Download bars again instead of using/writing the local cache.",
    )
    parser.add_argument(
        "--strategy-capital",
        help="Starting cash for the simulated strategy.",
    )
    parser.add_argument(
        "--grid-spacing-pct",
        help="Grid spacing percentage for a single backtest.",
    )
    parser.add_argument(
        "--base-order-notional",
        help="Dollar notional per simulated buy.",
    )
    parser.add_argument(
        "--max-buy-levels-below-anchor",
        type=int,
        help="Maximum buy levels below the active anchor.",
    )
    parser.add_argument(
        "--max-inventory-value",
        help="Maximum simulated inventory cost.",
    )
    parser.add_argument(
        "--cash-reserve",
        help="Cash reserve that simulated buys must preserve.",
    )
    parser.add_argument(
        "--max-unrealized-loss",
        help="Pause new buys once open unrealized loss reaches this amount. Use 'off' to disable.",
    )
    parser.add_argument(
        "--pause-new-buys-after-consecutive-down-levels",
        help="Pause new buys after this many consecutive buy levels without a sell. Use 'off' to disable.",
    )
    parser.add_argument(
        "--recenter-up-pct",
        help="When flat, move the grid anchor up after this percentage rise. Use 'off' to disable.",
    )
    parser.add_argument(
        "--adaptive-sizing",
        action="store_true",
        help="Increase buy size as the buy level is farther below the anchor.",
    )
    parser.add_argument(
        "--adaptive-scale-factor",
        help="Adaptive sizing factor. Example: 5 means a 10%% drop increases size by 50%%.",
    )
    parser.add_argument(
        "--adaptive-max-order-multiplier",
        help="Hard cap for adaptive order size as a multiple of base order notional.",
    )
    parser.add_argument(
        "--max-single-order-notional",
        help="Hard dollar cap for one adaptive buy order.",
    )
    parser.add_argument(
        "--allow-fractional-shares",
        action="store_true",
        help="Allow fractional simulated share quantities.",
    )


def _add_grid_cycle_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeframe",
        help="Alpaca stock bar timeframe. Defaults to runtime.grid_timeframe.",
    )
    parser.add_argument(
        "--feed",
        help="Alpaca stock data feed override. Defaults to alpaca.stock_data_feed.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=5,
        help="Days of recent bars to request before selecting the latest bar.",
    )
    parser.add_argument(
        "--state-path",
        help="Path to the persistent grid state JSON. Defaults to storage.grid_state_path.",
    )
    parser.add_argument(
        "--submit-paper",
        action="store_true",
        help="Submit paper limit orders for generated grid intents. Requires execution.enable_paper_orders=true.",
    )
    parser.add_argument(
        "--send-discord",
        action="store_true",
        help="Send a compact Discord grid cycle summary.",
    )
    parser.add_argument(
        "--json-output",
        help="Optional path to write the grid cycle artifact JSON.",
    )
    parser.add_argument(
        "--ignore-market-hours",
        action="store_true",
        help="Allow paper submission even when Alpaca reports the market is closed. Use only for testing.",
    )

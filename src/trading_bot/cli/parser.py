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

    cycle = subparsers.add_parser(
        "run-cycle",
        help="Run one monitor-before-open bot cycle.",
    )
    cycle.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to strategy.watchlist.",
    )
    cycle.add_argument(
        "--max-candidates",
        type=int,
        default=20,
        help="Maximum candidates to pass to each per-symbol LLM decision when opens are allowed.",
    )
    cycle.add_argument(
        "--option-feed",
        choices=["indicative", "opra"],
        help="Override Alpaca option data feed for this cycle.",
    )
    cycle.add_argument(
        "--send-discord",
        action="store_true",
        help="Send one Discord summary for the full cycle.",
    )
    cycle.add_argument(
        "--discord-summary-only",
        action="store_true",
        help="Send only the compact cycle Discord summary, without per-symbol detail messages.",
    )
    cycle.add_argument(
        "--json-output",
        help="Optional path to write the combined cycle artifact JSON.",
    )
    cycle.add_argument(
        "--mock-decision",
        choices=["skip", "disable_trading"],
        help="Use local mock decisions instead of calling OpenAI when opens are allowed.",
    )
    cycle.add_argument(
        "--submit-paper",
        action="store_true",
        help="Request paper order submission for an allocator-selected open. Requires execution.enable_paper_orders=true.",
    )
    cycle.add_argument(
        "--submit-paper-close",
        action="store_true",
        help="Request paper close order submission for recommended closes. Requires execution.enable_paper_close_orders=true.",
    )

    scheduler = subparsers.add_parser(
        "schedule-local",
        help="Run run-cycle repeatedly during US market hours.",
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
        help="Minutes between new-open run-cycle decisions. Defaults to runtime.scheduler_open_interval_minutes.",
    )
    scheduler.add_argument(
        "--heartbeat-minutes",
        type=float,
        help="Minutes between scheduler heartbeat messages. Defaults to runtime.scheduler_heartbeat_minutes.",
    )
    scheduler.add_argument(
        "--send-discord",
        action="store_true",
        help="Send scheduler heartbeat and error messages.",
    )
    scheduler.add_argument(
        "--send-cycle-discord",
        action="store_true",
        help="Also send each run-cycle's detailed Discord summary.",
    )
    scheduler.add_argument(
        "--cycle-summary-only",
        action="store_true",
        help="With --send-cycle-discord, send only compact cycle summaries without per-symbol details.",
    )
    scheduler.add_argument(
        "--json-output-dir",
        help="Optional directory for timestamped run-cycle JSON artifacts.",
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

from __future__ import annotations

import contextlib
import io
import json
import logging
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from trading_bot.config import load_config
from trading_bot.cli.parser import build_parser
from trading_bot.cycles.open_discovery import (
    _build_open_discovery_cycle_artifact,
    _max_concurrent_symbols,
    _run_open_discovery_cycle_with_lock,
)
from trading_bot.notifications.messages import (
    _send_daily_trading_summary,
    _send_open_discovery_summary,
    _send_order_poll_summary,
    _send_watchlist_decision_summary,
)
from trading_bot.scheduler.local import (
    _scheduler_closed_market_sleep_target,
    _scheduler_cycle_args,
    _scheduler_cycle_json_output,
    _scheduler_daily_summary_due_date,
    _scheduler_daily_summary_json_output,
    _scheduler_daily_summary_time_et,
    _scheduler_heartbeat_minutes,
    _scheduler_interval_minutes,
    _scheduler_open_interval_minutes,
    _scheduler_order_poll_limit,
)
from trading_bot.notifications.discord import NotificationResult
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.storage.db import init_db


class RiskBlockedAlpaca:
    def get_positions(self) -> list[dict]:
        return []

    def get_account(self) -> dict:
        return {
            "status": "ACTIVE",
            "equity": "99400",
            "last_equity": "100000",
            "buying_power": "200000",
            "portfolio_value": "99400",
        }


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, content: str) -> NotificationResult:
        self.messages.append(content)
        return NotificationResult(ok=True)


def watchlist_artifact_with_reason(reason: str) -> dict:
    return {
        "generated_at": "2026-06-04T00:00:00+00:00",
        "mode": "paper",
        "symbols": ["AAPL"],
        "per_symbol": [
            {
                "symbol": "AAPL",
                "decision_id": 1,
                "accepted": True,
                "validator_errors": [],
                "decision": {
                    "action": "skip",
                    "symbol": "AAPL",
                    "candidate_id": None,
                    "confidence": 0.91,
                    "decision_reason": reason,
                    "news_assessment": {
                        "risk_level": "medium",
                        "sentiment": "mixed",
                        "summary": "Full detail test news summary.",
                    },
                    "risk_checklist": {
                        "market_trend_ok": True,
                        "liquidity_ok": True,
                    },
                },
                "order_preview": None,
                "selected_candidate": None,
                "candidate_count": 0,
            }
        ],
        "allocation": {
            "accepted_open_count": 0,
            "selected_open": None,
            "ranked_opens": [],
        },
        "selected_order_preview": None,
        "execution_attempt": None,
    }


class OpenDiscoveryTests(unittest.TestCase):
    def test_parser_rejects_removed_run_cycle_command(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["run-cycle"])

    def test_parser_accepts_schedule_local_args(self) -> None:
        args = build_parser().parse_args(
            [
                "schedule-local",
                "--symbols",
                "AAPL,MSFT",
                "--interval-minutes",
                "1",
                "--open-interval-minutes",
                "5",
                "--heartbeat-minutes",
                "30",
                "--send-discord",
                "--send-cycle-discord",
                "--cycle-summary-only",
                "--submit-paper-close",
                "--skip-order-poll",
                "--order-poll-limit",
                "25",
                "--skip-daily-summary",
                "--daily-summary-time-et",
                "16:10",
                "--once",
                "--ignore-market-hours",
                "--mock-decision",
                "skip",
            ]
        )

        self.assertEqual(args.command, "schedule-local")
        self.assertEqual(args.symbols, "AAPL,MSFT")
        self.assertEqual(args.interval_minutes, 1)
        self.assertEqual(args.open_interval_minutes, 5)
        self.assertEqual(args.heartbeat_minutes, 30)
        self.assertTrue(args.send_discord)
        self.assertTrue(args.send_cycle_discord)
        self.assertTrue(args.cycle_summary_only)
        self.assertTrue(args.submit_paper_close)
        self.assertTrue(args.skip_order_poll)
        self.assertEqual(args.order_poll_limit, 25)
        self.assertTrue(args.skip_daily_summary)
        self.assertEqual(args.daily_summary_time_et, "16:10")
        self.assertTrue(args.once)
        self.assertTrue(args.ignore_market_hours)

    def test_parser_accepts_poll_orders_args(self) -> None:
        args = build_parser().parse_args(
            [
                "poll-orders",
                "--status",
                "all",
                "--limit",
                "25",
                "--send-discord",
                "--notify-no-changes",
                "--json-output",
                "data/order_poll.json",
            ]
        )

        self.assertEqual(args.command, "poll-orders")
        self.assertEqual(args.status, "all")
        self.assertEqual(args.limit, 25)
        self.assertTrue(args.send_discord)
        self.assertTrue(args.notify_no_changes)
        self.assertEqual(args.json_output, "data/order_poll.json")

    def test_parser_accepts_daily_summary_args(self) -> None:
        args = build_parser().parse_args(
            [
                "daily-summary",
                "--summary-date",
                "2026-06-04",
                "--option-feed",
                "indicative",
                "--send-discord",
                "--json-output",
                "data/daily_summary.json",
            ]
        )

        self.assertEqual(args.command, "daily-summary")
        self.assertEqual(args.summary_date, "2026-06-04")
        self.assertEqual(args.option_feed, "indicative")
        self.assertTrue(args.send_discord)
        self.assertEqual(args.json_output, "data/daily_summary.json")

    def test_scheduler_defaults_and_cycle_args(self) -> None:
        config = load_config("config/settings.yaml")
        args = build_parser().parse_args(
            [
                "schedule-local",
                "--symbols",
                "AAPL",
                "--max-candidates",
                "2",
                "--mock-decision",
                "skip",
            ]
        )

        self.assertEqual(_scheduler_interval_minutes(args, config), 1)
        self.assertEqual(_scheduler_open_interval_minutes(args, config), 5)
        self.assertEqual(_scheduler_heartbeat_minutes(args, config), 60)
        self.assertEqual(_scheduler_order_poll_limit(args, config), 50)
        self.assertEqual(_scheduler_daily_summary_time_et(args, config).strftime("%H:%M"), "16:05")

        cycle_args = _scheduler_cycle_args(args, "data/test_cycle.json")
        self.assertEqual(cycle_args.command, "open-discovery-cycle")
        self.assertEqual(cycle_args.symbols, "AAPL")
        self.assertEqual(cycle_args.max_candidates, 2)
        self.assertFalse(cycle_args.send_discord)
        self.assertFalse(cycle_args.discord_summary_only)
        self.assertEqual(cycle_args.json_output, "data/test_cycle.json")
        self.assertFalse(cycle_args.submit_paper_close)

    def test_scheduler_cycle_args_preserve_summary_only(self) -> None:
        args = build_parser().parse_args(
            [
                "schedule-local",
                "--send-cycle-discord",
                "--cycle-summary-only",
            ]
        )

        cycle_args = _scheduler_cycle_args(args, "data/test_cycle.json")

        self.assertTrue(cycle_args.send_discord)
        self.assertTrue(cycle_args.discord_summary_only)

    def test_watchlist_decision_concurrency_default_is_eight(self) -> None:
        config = load_config("config/settings.yaml")

        self.assertEqual(_max_concurrent_symbols(config, 8), 8)
        self.assertEqual(_max_concurrent_symbols(config, 3), 3)

    def test_scheduler_cycle_json_output_is_timestamped(self) -> None:
        args = build_parser().parse_args(
            ["schedule-local", "--json-output-dir", "data/scheduler_cycles"]
        )

        path = _scheduler_cycle_json_output(
            args,
            datetime(2026, 6, 4, 15, 30, tzinfo=UTC),
        )

        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.endswith("data/scheduler_cycles/open_discovery_20260604T153000Z.json"))

    def test_scheduler_daily_summary_helpers(self) -> None:
        args = build_parser().parse_args(
            ["schedule-local", "--json-output-dir", "data/scheduler_cycles"]
        )

        path = _scheduler_daily_summary_json_output(
            args,
            datetime(2026, 6, 4, tzinfo=UTC).date(),
        )
        due = _scheduler_daily_summary_due_date(
            datetime(2026, 6, 4, 20, 6, tzinfo=UTC),
            daily_summary_time=_scheduler_daily_summary_time_et(args, load_config("config/settings.yaml")),
            sent_dates=set(),
        )
        not_due = _scheduler_daily_summary_due_date(
            datetime(2026, 6, 4, 19, 59, tzinfo=UTC),
            daily_summary_time=_scheduler_daily_summary_time_et(args, load_config("config/settings.yaml")),
            sent_dates=set(),
        )

        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.endswith("data/scheduler_cycles/daily_summary_2026-06-04.json"))
        self.assertEqual(due, datetime(2026, 6, 4, tzinfo=UTC).date())
        self.assertIsNone(not_due)

    def test_closed_market_sleep_wakes_for_pending_daily_summary_first(self) -> None:
        target, reason = _scheduler_closed_market_sleep_target(
            now=datetime(2026, 6, 4, 20, 1, tzinfo=UTC),
            clock={"next_open": "2026-06-05T09:30:00-04:00"},
            daily_summary_time=_scheduler_daily_summary_time_et(
                build_parser().parse_args(["schedule-local"]),
                load_config("config/settings.yaml"),
            ),
            sent_dates=set(),
            daily_summary_enabled=True,
        )

        self.assertEqual(target, datetime(2026, 6, 4, 20, 5, tzinfo=UTC))
        self.assertEqual(reason, "daily_summary")

    def test_closed_market_sleep_wakes_at_next_open_after_summary_sent(self) -> None:
        target, reason = _scheduler_closed_market_sleep_target(
            now=datetime(2026, 6, 4, 20, 6, tzinfo=UTC),
            clock={"next_open": "2026-06-05T09:30:00-04:00"},
            daily_summary_time=_scheduler_daily_summary_time_et(
                build_parser().parse_args(["schedule-local"]),
                load_config("config/settings.yaml"),
            ),
            sent_dates={"2026-06-04"},
            daily_summary_enabled=True,
        )

        self.assertEqual(target, datetime(2026, 6, 5, 13, 30, tzinfo=UTC))
        self.assertEqual(reason, "next_open")

    def test_closed_market_sleep_does_not_skip_due_summary_retry(self) -> None:
        target, reason = _scheduler_closed_market_sleep_target(
            now=datetime(2026, 6, 4, 20, 6, tzinfo=UTC),
            clock={"next_open": "2026-06-05T09:30:00-04:00"},
            daily_summary_time=_scheduler_daily_summary_time_et(
                build_parser().parse_args(["schedule-local"]),
                load_config("config/settings.yaml"),
            ),
            sent_dates=set(),
            daily_summary_enabled=True,
        )

        self.assertIsNone(target)
        self.assertEqual(reason, "daily_summary_due")

    def test_open_discovery_cycle_skips_watchlist_when_account_risk_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)
            output_path = Path(tmpdir) / "open_discovery.json"
            scheduler_args = build_parser().parse_args(
                ["schedule-local", "--symbols", "AAPL", "--mock-decision", "skip"]
            )
            args = _scheduler_cycle_args(scheduler_args, str(output_path))
            logger = logging.getLogger("test.open_discovery_risk_block")
            logger.disabled = True

            with patch("trading_bot.cycles.open_discovery._build_watchlist_decision_run") as watchlist:
                code = _run_open_discovery_cycle_with_lock(
                    args=args,
                    config=load_config("config/settings.yaml"),
                    logger=logger,
                    db_path=db_path,
                    kill_switch=KillSwitch(Path(tmpdir) / "KILL_SWITCH"),
                    notifier=FakeNotifier(),
                    started_at=datetime.now(UTC).isoformat(),
                    alpaca=RiskBlockedAlpaca(),
                )

            artifact = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        watchlist.assert_not_called()
        self.assertEqual(artifact["command"], "open-discovery-cycle")
        self.assertEqual(artifact["phase"], "open_account_risk_block")
        self.assertTrue(artifact["skipped_open_decisions"])
        self.assertNotIn("monitor", artifact)
        self.assertIsNone(artifact["watchlist_decision"])
        self.assertTrue(artifact["account_risk_state"]["blocks_new_opens"])

    def test_open_discovery_discord_summary_reports_account_risk_skip(self) -> None:
        artifact = _build_open_discovery_cycle_artifact(
            config=load_config("config/settings.yaml"),
            phase="open_account_risk_block",
            watchlist_artifact=None,
            skipped_open_reason="Account risk gate blocked new opens: Daily P&L -600 breaches max_daily_loss 500",
            account_risk_state={
                "daily_pnl": "-600.00",
                "weekly_pnl": "-600.00",
                "new_trades_today": 2,
                "max_new_trades_per_day": 3,
                "block_reasons": ["Daily P&L -600 breaches max_daily_loss 500"],
            },
        )
        notifier = FakeNotifier()

        ok = _send_open_discovery_summary(notifier, artifact, logging.getLogger("test"))

        self.assertTrue(ok)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("New trade search complete", notifier.messages[0])
        self.assertIn("What happened: Risk limits blocked", notifier.messages[0])
        self.assertIn("New trades: skipped", notifier.messages[0])
        self.assertIn("Account risk:", notifier.messages[0])
        self.assertNotIn("Phase:", notifier.messages[0])

    def test_watchlist_discord_sends_full_decision_reason_in_detail_message(self) -> None:
        reason = "This is a long decision reason. " * 12
        artifact = watchlist_artifact_with_reason(reason)
        notifier = FakeNotifier()

        ok = _send_watchlist_decision_summary(notifier, artifact, logging.getLogger("test"))

        self.assertTrue(ok)
        self.assertEqual(len(notifier.messages), 2)
        self.assertIn("Decision detail messages: 1", notifier.messages[0])
        self.assertNotIn(reason, notifier.messages[0])
        self.assertIn(reason, notifier.messages[1])

    def test_open_discovery_discord_chunks_full_decision_reason_when_needed(self) -> None:
        reason = "R" * 2300
        watchlist = watchlist_artifact_with_reason(reason)
        artifact = _build_open_discovery_cycle_artifact(
            config=load_config("config/settings.yaml"),
            phase="open_discovery",
            watchlist_artifact=watchlist,
            skipped_open_reason=None,
        )
        notifier = FakeNotifier()

        ok = _send_open_discovery_summary(notifier, artifact, logging.getLogger("test"))

        self.assertTrue(ok)
        self.assertGreater(len(notifier.messages), 2)
        self.assertIn("Decision detail messages: 1", notifier.messages[0])
        self.assertIn(reason, "".join(notifier.messages[1:]))

    def test_order_poll_discord_sends_all_changes(self) -> None:
        changes = [
            {
                "broker_order_id": f"order-{index}",
                "client_order_id": f"client-{index}",
                "symbol": "AAPL",
                "previous_status": "new",
                "status": "filled",
                "previous_filled_qty": "0",
                "filled_qty": "1",
                "qty": "1",
                "order_class": "mleg",
            }
            for index in range(12)
        ]
        notifier = FakeNotifier()

        ok = _send_order_poll_summary(
            notifier,
            {
                "status_filter": "all",
                "order_count": 12,
                "change_count": 12,
                "changes": changes,
            },
            logging.getLogger("test"),
        )

        self.assertTrue(ok)
        content = "\n".join(notifier.messages)
        self.assertIn("Changes: 12", content)
        self.assertIn("order-0", content)
        self.assertIn("order-11", content)

    def test_daily_trading_summary_discord_focuses_on_pnl_positions_orders(self) -> None:
        notifier = FakeNotifier()
        artifact = {
            "summary_date": "2026-06-04",
            "mode": "paper",
            "account": {
                "equity": "100500.00",
                "daily_pnl": "500.00",
                "buying_power": "198000.00",
            },
            "positions": {
                "broker_position_count": 2,
                "option_position_count": 2,
                "spread_count": 1,
                "estimated_open_spread_pnl": "65.00",
                "close_recommended_count": 0,
                "spreads": [
                    {
                        "spread_id": "AAPL-2026-06-12-190P-185P",
                        "quantity": 1,
                        "dte": 8,
                        "estimated_unrealized_pnl": "65.00",
                        "close_recommended": False,
                    }
                ],
            },
            "orders": {
                "open_order_count": 0,
                "recent_order_count": 2,
                "lifecycle_events": {
                    "total": 2,
                    "by_status": {"filled": 1, "new": 1},
                },
            },
            "execution_attempts": {
                "total": 1,
                "requested": 1,
                "submitted": 1,
                "by_status": {"submitted": 1},
            },
        }

        ok = _send_daily_trading_summary(notifier, artifact, logging.getLogger("test"))

        self.assertTrue(ok)
        content = "\n".join(notifier.messages)
        self.assertIn("daily P&L: 500.00", content)
        self.assertIn("Open positions: broker=2 option=2 spreads=1", content)
        self.assertIn("Estimated open spread P&L: 65.00", content)
        self.assertIn("Order event statuses: filled=1, new=1", content)
        self.assertNotIn("LLM", content)


if __name__ == "__main__":
    unittest.main()

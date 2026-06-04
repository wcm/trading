from __future__ import annotations

import logging
import unittest
from datetime import UTC, datetime

from trading_bot.config import load_config
from trading_bot.main import (
    _build_run_cycle_artifact,
    _close_recommended_spreads,
    _scheduler_cycle_args,
    _scheduler_cycle_json_output,
    _scheduler_heartbeat_minutes,
    _scheduler_interval_minutes,
    _send_watchlist_decision_summary,
    _send_run_cycle_summary,
    build_parser,
)
from trading_bot.notifications.discord import NotificationResult


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, content: str) -> NotificationResult:
        self.messages.append(content)
        return NotificationResult(ok=True)


def monitor_artifact_with_close() -> dict:
    return {
        "generated_at": "2026-06-04T00:00:00+00:00",
        "option_position_count": 2,
        "spread_count": 1,
        "spreads": [
            {
                "spread_id": "AAPL-2099-12-31-305P-300P",
                "close_recommended": True,
                "close_debit": "0.4",
                "estimated_unrealized_pnl": "65",
                "exit_flags": {"profit_target_hit": True},
                "close_order_preview": {"errors": [], "payload": {"order_class": "mleg"}},
            }
        ],
        "unpaired_legs": [],
        "warnings": [],
    }


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


class RunCycleTests(unittest.TestCase):
    def test_parser_accepts_run_cycle_args(self) -> None:
        args = build_parser().parse_args(
            [
                "run-cycle",
                "--symbols",
                "AAPL,MSFT",
                "--max-candidates",
                "3",
                "--mock-decision",
                "skip",
                "--submit-paper",
                "--submit-paper-close",
            ]
        )

        self.assertEqual(args.command, "run-cycle")
        self.assertEqual(args.symbols, "AAPL,MSFT")
        self.assertEqual(args.max_candidates, 3)
        self.assertEqual(args.mock_decision, "skip")
        self.assertTrue(args.submit_paper)
        self.assertTrue(args.submit_paper_close)

    def test_parser_accepts_schedule_local_args(self) -> None:
        args = build_parser().parse_args(
            [
                "schedule-local",
                "--symbols",
                "AAPL,MSFT",
                "--interval-minutes",
                "3",
                "--heartbeat-minutes",
                "30",
                "--send-discord",
                "--send-cycle-discord",
                "--submit-paper-close",
                "--once",
                "--ignore-market-hours",
                "--mock-decision",
                "skip",
            ]
        )

        self.assertEqual(args.command, "schedule-local")
        self.assertEqual(args.symbols, "AAPL,MSFT")
        self.assertEqual(args.interval_minutes, 3)
        self.assertEqual(args.heartbeat_minutes, 30)
        self.assertTrue(args.send_discord)
        self.assertTrue(args.send_cycle_discord)
        self.assertTrue(args.submit_paper_close)
        self.assertTrue(args.once)
        self.assertTrue(args.ignore_market_hours)

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

        self.assertEqual(_scheduler_interval_minutes(args, config), 3)
        self.assertEqual(_scheduler_heartbeat_minutes(args, config), 60)

        cycle_args = _scheduler_cycle_args(args, "data/test_cycle.json")
        self.assertEqual(cycle_args.command, "run-cycle")
        self.assertEqual(cycle_args.symbols, "AAPL")
        self.assertEqual(cycle_args.max_candidates, 2)
        self.assertFalse(cycle_args.send_discord)
        self.assertEqual(cycle_args.json_output, "data/test_cycle.json")
        self.assertFalse(cycle_args.submit_paper_close)

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
        self.assertTrue(path.endswith("data/scheduler_cycles/run_cycle_20260604T153000Z.json"))

    def test_cycle_artifact_skips_open_decisions_when_close_is_recommended(self) -> None:
        config = load_config("config/settings.yaml")
        monitor = monitor_artifact_with_close()
        close_spreads = _close_recommended_spreads(monitor)

        artifact = _build_run_cycle_artifact(
            config=config,
            phase="monitor_close_alert",
            monitor_artifact=monitor,
            close_recommended_spreads=close_spreads,
            close_execution_attempts=[],
            watchlist_artifact=None,
            skipped_open_reason="1 existing spread(s) have close_recommended=true",
        )

        self.assertEqual(artifact["phase"], "monitor_close_alert")
        self.assertEqual(artifact["close_recommended_count"], 1)
        self.assertTrue(artifact["skipped_open_decisions"])
        self.assertIsNone(artifact["watchlist_decision"])

    def test_run_cycle_discord_summary_reports_close_skip(self) -> None:
        config = load_config("config/settings.yaml")
        monitor = monitor_artifact_with_close()
        close_spreads = _close_recommended_spreads(monitor)
        artifact = _build_run_cycle_artifact(
            config=config,
            phase="monitor_close_alert",
            monitor_artifact=monitor,
            close_recommended_spreads=close_spreads,
            close_execution_attempts=[],
            watchlist_artifact=None,
            skipped_open_reason="1 existing spread(s) have close_recommended=true",
        )
        notifier = FakeNotifier()

        ok = _send_run_cycle_summary(notifier, artifact, logging.getLogger("test"))

        self.assertTrue(ok)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("Open decisions: skipped", notifier.messages[0])
        self.assertIn("AAPL-2099-12-31-305P-300P", notifier.messages[0])
        self.assertIn("preview=ready", notifier.messages[0])

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

    def test_run_cycle_discord_chunks_full_decision_reason_when_needed(self) -> None:
        reason = "R" * 2300
        watchlist = watchlist_artifact_with_reason(reason)
        artifact = _build_run_cycle_artifact(
            config=load_config("config/settings.yaml"),
            phase="monitor_then_open",
            monitor_artifact={
                "generated_at": "2026-06-04T00:00:00+00:00",
                "option_position_count": 0,
                "spread_count": 0,
                "spreads": [],
                "unpaired_legs": [],
                "warnings": [],
            },
            close_recommended_spreads=[],
            close_execution_attempts=[],
            watchlist_artifact=watchlist,
            skipped_open_reason=None,
        )
        notifier = FakeNotifier()

        ok = _send_run_cycle_summary(notifier, artifact, logging.getLogger("test"))

        self.assertTrue(ok)
        self.assertGreater(len(notifier.messages), 2)
        self.assertIn("Decision detail messages: 1", notifier.messages[0])
        self.assertIn(reason, "".join(notifier.messages[1:]))


if __name__ == "__main__":
    unittest.main()

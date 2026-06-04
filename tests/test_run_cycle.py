from __future__ import annotations

import logging
import unittest

from trading_bot.config import load_config
from trading_bot.main import (
    _build_run_cycle_artifact,
    _close_recommended_spreads,
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
            ]
        )

        self.assertEqual(args.command, "run-cycle")
        self.assertEqual(args.symbols, "AAPL,MSFT")
        self.assertEqual(args.max_candidates, 3)
        self.assertEqual(args.mock_decision, "skip")
        self.assertTrue(args.submit_paper)

    def test_cycle_artifact_skips_open_decisions_when_close_is_recommended(self) -> None:
        config = load_config("config/settings.yaml")
        monitor = monitor_artifact_with_close()
        close_spreads = _close_recommended_spreads(monitor)

        artifact = _build_run_cycle_artifact(
            config=config,
            phase="monitor_close_alert",
            monitor_artifact=monitor,
            close_recommended_spreads=close_spreads,
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


if __name__ == "__main__":
    unittest.main()

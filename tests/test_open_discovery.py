from __future__ import annotations

import contextlib
import io
import json
import logging
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from trading_bot.config import load_config
from trading_bot.cli.parser import build_parser
from trading_bot.cycles.open_discovery import (
    _build_decision_artifact,
    _build_open_discovery_cycle_artifact,
    _max_concurrent_symbols,
    _run_open_discovery_cycle_with_lock,
)
from trading_bot.data.news import NewsContext
from trading_bot.notifications.messages import (
    _send_daily_trading_summary,
    _send_open_discovery_summary,
    _send_order_poll_summary,
    _send_position_monitor_summary,
    _send_watchlist_decision_summary,
)
from trading_bot.scheduler.local import (
    _scheduler_closed_market_sleep_target,
    _scheduler_cycle_args,
    _scheduler_cycle_json_output,
    _scheduler_daily_summary_due_date,
    _scheduler_daily_summary_json_output,
    _scheduler_daily_summary_time_et,
    _scheduler_interval_minutes,
    _scheduler_open_interval_minutes,
    _scheduler_order_poll_limit,
)
from trading_bot.notifications.discord import NotificationResult
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.storage.db import init_db
from tests.test_put_credit_spread import FakeAlpacaClient


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


class FreshDecisionAlpaca(FakeAlpacaClient):
    def get_account(self) -> dict:
        return {
            "status": "ACTIVE",
            "equity": "100000",
            "last_equity": "100000",
            "buying_power": "200000",
            "portfolio_value": "100000",
        }

    def get_clock(self) -> dict:
        return {"is_open": True, "timestamp": datetime.now(UTC).isoformat()}

    def get_positions(self) -> list[dict]:
        return []

    def get_orders(self, *, status: str = "open", limit: int = 50) -> list[dict]:
        return []

    def get_stock_bars(
        self,
        symbols: list[str],
        *,
        timeframe: str,
        start: str,
        end: str | None = None,
        feed: str = "iex",
        limit: int = 10_000,
        sort: str = "asc",
    ) -> dict[str, list[dict[str, Any]]]:
        now = datetime.now(UTC)
        bars = []
        for index in range(25):
            close = 100 + index
            bars.append(
                {
                    "t": now.replace(microsecond=0).isoformat(),
                    "o": str(100 + index - 1),
                    "h": str(close + 1),
                    "l": str(close - 1),
                    "c": str(close),
                    "v": 1000,
                }
            )
        return {symbol: bars for symbol in symbols}

    def get_option_snapshots(
        self,
        symbols: list[str],
        *,
        feed: str = "indicative",
        chunk_size: int = 100,
    ) -> dict[str, dict[str, Any]]:
        snapshots = super().get_option_snapshots(symbols, feed=feed, chunk_size=chunk_size)
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        for snapshot in snapshots.values():
            snapshot["latestQuote"]["t"] = now
        return snapshots


class NoCandidateDecisionAlpaca(FreshDecisionAlpaca):
    def get_option_contracts(
        self,
        *,
        underlying_symbols: list[str],
        expiration_date_gte: str,
        expiration_date_lte: str,
        option_type: str = "put",
        status: str = "active",
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        return []


class FakeOpenAIClient:
    model = "test-model"

    def create_trading_decision(self, *, prompt_text: str, decision_packet: dict[str, Any]) -> tuple[dict, dict]:
        return (
            {
                "action": "skip",
                "symbol": "AAPL",
                "candidate_id": None,
                "quantity": 0,
                "limit_price": None,
                "confidence": 0.9,
                "decision_reason": "Fake model skip after hard filters passed.",
                "news_assessment": {
                    "risk_level": "low",
                    "sentiment": "neutral",
                    "summary": "No material test news.",
                },
                "risk_checklist": {
                    "defined_risk": True,
                    "within_max_loss": True,
                    "liquidity_ok": True,
                    "earnings_ok": True,
                    "no_material_negative_news": True,
                    "market_trend_ok": True,
                    "broad_market_ok": True,
                    "short_put_distance_ok": True,
                },
                "exit_plan": {
                    "profit_take_credit_pct": 50,
                    "loss_trigger": "2x initial credit or short put delta above 0.45",
                    "close_before_expiry_days": 3,
                },
            },
            {"fake": True},
        )


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

    def test_pre_llm_filter_skips_news_and_openai_when_no_numeric_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)

            with (
                patch("trading_bot.cycles.open_discovery.build_news_context") as news_context,
                patch("trading_bot.cycles.open_discovery.OpenAIClient.from_config") as openai_client,
            ):
                artifact, scan_result = _build_decision_artifact(
                    config=load_config("config/settings.yaml"),
                    db_path=db_path,
                    alpaca=NoCandidateDecisionAlpaca(),
                    symbols=["AAPL"],
                    max_candidates=5,
                    option_feed="indicative",
                    mock_decision=None,
                )

        news_context.assert_not_called()
        openai_client.assert_not_called()
        self.assertEqual(scan_result.candidates, [])
        self.assertEqual(artifact["decision_source"], "pre_llm_filter")
        self.assertFalse(artifact["llm_called"])
        self.assertEqual(artifact["decision"]["action"], "skip")
        self.assertEqual(artifact["pre_llm_filter"]["raw_candidate_count"], 0)
        self.assertEqual(artifact["pre_llm_filter"]["eligible_candidate_count"], 0)
        self.assertIn("No candidates passed scanner filters", artifact["decision"]["decision_reason"])

    def test_pre_llm_filter_allows_news_and_decision_when_numeric_candidates_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)
            news = NewsContext(
                provider="test",
                generated_at=datetime.now(UTC).isoformat(),
                lookback_hours=24,
                symbols=["AAPL"],
                item_count=0,
                items=[],
                warnings=[],
            )

            with (
                patch("trading_bot.cycles.open_discovery.build_news_context", return_value=news) as news_context,
                patch("trading_bot.cycles.open_discovery.OpenAIClient.from_config", return_value=FakeOpenAIClient()) as openai_client,
            ):
                artifact, scan_result = _build_decision_artifact(
                    config=load_config("config/settings.yaml"),
                    db_path=db_path,
                    alpaca=FreshDecisionAlpaca(),
                    symbols=["AAPL"],
                    max_candidates=5,
                    option_feed="indicative",
                    mock_decision=None,
                )

        news_context.assert_called_once()
        openai_client.assert_called_once()
        self.assertEqual(len(scan_result.candidates), 1)
        self.assertEqual(artifact["decision_source"], "openai")
        self.assertTrue(artifact["llm_called"])
        self.assertEqual(artifact["pre_llm_filter"]["raw_candidate_count"], 1)
        self.assertEqual(artifact["pre_llm_filter"]["eligible_candidate_count"], 1)
        self.assertEqual(artifact["pre_llm_filter"]["block_reasons"], [])

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
        self.assertIn("# New Trade Skipped", notifier.messages[0])
        self.assertIn("**Reason:** Daily P&L -600 breaches", notifier.messages[0])
        self.assertIn("**Daily P&L:** -600.00", notifier.messages[0])
        self.assertNotIn("Phase:", notifier.messages[0])

    def test_position_monitor_sends_only_close_recommendations(self) -> None:
        notifier = FakeNotifier()
        artifact = {
            "option_position_count": 2,
            "spread_count": 1,
            "unpaired_legs": [],
            "spreads": [
                {
                    "spread_id": "AAPL-2026-06-12-305P-300P",
                    "underlying_symbol": "AAPL",
                    "short_strike": "305",
                    "long_strike": "300",
                    "close_recommended": True,
                    "close_debit": "0.40",
                    "exit_flags": {"profit_target_hit": True},
                }
            ],
        }

        ok = _send_position_monitor_summary(notifier, artifact, logging.getLogger("test"))

        self.assertTrue(ok)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("# Order Close Recommendation", notifier.messages[0])
        self.assertIn("**AAPL 305/300P**", notifier.messages[0])
        self.assertIn("**Price:** 0.40", notifier.messages[0])
        self.assertIn("**Reason:** profit target reached", notifier.messages[0])

    def test_position_monitor_stays_quiet_without_close_recommendations(self) -> None:
        notifier = FakeNotifier()
        artifact = {
            "option_position_count": 2,
            "spread_count": 1,
            "unpaired_legs": [],
            "spreads": [
                {
                    "spread_id": "AAPL-2026-06-12-305P-300P",
                    "close_recommended": False,
                    "close_debit": "0.80",
                    "exit_flags": {},
                }
            ],
        }

        ok = _send_position_monitor_summary(notifier, artifact, logging.getLogger("test"))

        self.assertTrue(ok)
        self.assertEqual(notifier.messages, [])

    def test_watchlist_discord_sends_full_decision_reason_in_detail_message(self) -> None:
        reason = "This is a long decision reason. " * 12
        artifact = watchlist_artifact_with_reason(reason)
        notifier = FakeNotifier()

        ok = _send_watchlist_decision_summary(notifier, artifact, logging.getLogger("test"))

        self.assertTrue(ok)
        self.assertEqual(len(notifier.messages), 2)
        self.assertIn("# Watchlist Decision", notifier.messages[0])
        self.assertIn("**Selected:** none", notifier.messages[0])
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
        self.assertIn("# New Trade Search", notifier.messages[0])
        self.assertIn("**Selected:** none", notifier.messages[0])
        self.assertIn(reason, "".join(notifier.messages[1:]))

    def test_open_discovery_summary_reports_hard_filter_reason(self) -> None:
        watchlist = watchlist_artifact_with_reason("Hard filters blocked LLM call.")
        watchlist["per_symbol"][0]["pre_llm_filter"] = {
            "raw_candidate_count": 0,
            "eligible_candidate_count": 0,
            "block_reasons": ["No candidates passed scanner filters."],
        }
        artifact = _build_open_discovery_cycle_artifact(
            config=load_config("config/settings.yaml"),
            phase="open_discovery",
            watchlist_artifact=watchlist,
            skipped_open_reason=None,
        )
        notifier = FakeNotifier()

        ok = _send_open_discovery_summary(
            notifier,
            artifact,
            logging.getLogger("test"),
            include_decision_details=False,
        )

        self.assertTrue(ok)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("**Hard filters passed:** 0 / 1", notifier.messages[0])
        self.assertIn("**Reason:** No candidates passed scanner filters.", notifier.messages[0])

    def test_order_poll_discord_sends_simple_order_event_messages(self) -> None:
        open_order = {
            "id": "open-order-1",
            "client_order_id": "preview-aapl-001",
            "symbol": "",
            "status": "filled",
            "filled_qty": "1",
            "filled_avg_price": "-1.05",
            "qty": "1",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL260612P00305000", "position_intent": "sell_to_open"},
                {"symbol": "AAPL260612P00300000", "position_intent": "buy_to_open"},
            ],
        }
        close_order = {
            "id": "close-order-1",
            "client_order_id": "close-preview-aapl-001",
            "symbol": "",
            "status": "filled",
            "filled_qty": "1",
            "filled_avg_price": "0.40",
            "qty": "1",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL260612P00305000", "position_intent": "buy_to_close"},
                {"symbol": "AAPL260612P00300000", "position_intent": "sell_to_close"},
            ],
        }
        changes = [
            {
                "broker_order_id": "open-order-1",
                "client_order_id": "preview-aapl-001",
                "symbol": "",
                "previous_status": "new",
                "status": "filled",
                "previous_filled_qty": "0",
                "filled_qty": "1",
                "qty": "1",
                "order_class": "mleg",
                "raw_order": open_order,
            },
            {
                "broker_order_id": "close-order-1",
                "client_order_id": "close-preview-aapl-001",
                "symbol": "",
                "previous_status": "new",
                "status": "filled",
                "previous_filled_qty": "0",
                "filled_qty": "1",
                "qty": "1",
                "order_class": "mleg",
                "raw_order": close_order,
                "spread_trade": {
                    "spread_id": "AAPL-2026-06-12-305P-300P",
                    "entry_credit": "1.05",
                    "close_debit": "0.40",
                    "quantity": 1,
                    "realized_pnl": "65.00",
                },
            },
            {
                "broker_order_id": "rejected-order-1",
                "client_order_id": "preview-msft-001",
                "symbol": "MSFT",
                "previous_status": "new",
                "status": "rejected",
                "raw_order": {"symbol": "MSFT", "status": "rejected", "reject_reason": "buying power"},
            },
            {
                "broker_order_id": "canceled-order-1",
                "client_order_id": "preview-nvda-001",
                "symbol": "NVDA",
                "previous_status": "new",
                "status": "canceled",
                "raw_order": {"symbol": "NVDA", "status": "canceled", "limit_price": "0.80"},
            },
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
        self.assertEqual(len(notifier.messages), 4)
        content = "\n".join(notifier.messages)
        self.assertIn("# Orders Filled", content)
        self.assertIn("**AAPL 305/300P**", content)
        self.assertIn("**Price:** 1.05", content)
        self.assertIn("# Orders Closed", content)
        self.assertIn("**Price:** 0.40", content)
        self.assertIn("**P&L:** +65.00", content)
        self.assertIn("# Orders Rejected", content)
        self.assertIn("**MSFT**", content)
        self.assertIn("**Reason:** buying power", content)
        self.assertIn("# Orders Canceled", content)
        self.assertIn("**NVDA**", content)
        self.assertNotIn("Orders polled", content)

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
        self.assertIn("# Daily Summary", content)
        self.assertIn("**Daily P&L:** 500.00", content)
        self.assertIn("**Spreads:** 1", content)
        self.assertIn("**Estimated open P&L:** 65.00", content)
        self.assertIn("**Statuses:** filled=1, new=1", content)
        self.assertNotIn("LLM", content)


if __name__ == "__main__":
    unittest.main()

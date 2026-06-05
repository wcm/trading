from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from trading_bot.config import AppConfig, load_config
from trading_bot.execution.gate import maybe_submit_paper_close_order, maybe_submit_paper_order
from trading_bot.execution.orders import build_put_credit_spread_close_preview, build_put_credit_spread_order_preview
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.risk.kill_switch import KillSwitch
from trading_bot.storage.db import init_db, record_execution_attempt
from tests.test_order_preview import valid_candidate, valid_open_decision


class FakeAlpacaSubmitClient:
    def __init__(self) -> None:
        self.submitted_payloads: list[dict] = []

    def submit_order(self, payload: dict) -> dict:
        self.submitted_payloads.append(payload)
        return {"id": "paper-order-1", "status": "accepted"}


def config_with_paper_orders_enabled(enabled: bool) -> AppConfig:
    base = load_config("config/settings.yaml")
    values = copy.deepcopy(base.values)
    values.setdefault("execution", {})["enable_paper_orders"] = enabled
    return AppConfig(settings_path=base.settings_path, values=values)


def config_with_paper_close_orders_enabled(enabled: bool) -> AppConfig:
    base = load_config("config/settings.yaml")
    values = copy.deepcopy(base.values)
    values.setdefault("execution", {})["enable_paper_close_orders"] = enabled
    return AppConfig(settings_path=base.settings_path, values=values)


def clean_preview(config: AppConfig) -> dict:
    return build_put_credit_spread_order_preview(
        config=config,
        decision=valid_open_decision(),
        candidate=valid_candidate(),
        client_order_id="preview-aapl-test-001",
    )


def clean_close_preview(config: AppConfig) -> dict:
    return build_put_credit_spread_close_preview(
        config=config,
        spread=close_spread(),
        client_order_id="close-preview-aapl-test-001",
    )


def allocation() -> dict:
    return {
        "selected_open": {
            "decision_id": 1,
            "candidate_id": "AAPL-2026-06-12-305.00P-300.00P",
            "max_contracts_under_open_risk": 12,
        }
    }


def close_spread() -> dict:
    return {
        "spread_id": "AAPL-2026-06-12-305P-300P",
        "underlying_symbol": "AAPL",
        "short_put_symbol": "AAPL260612P00305000",
        "long_put_symbol": "AAPL260612P00300000",
        "quantity": 1,
        "close_limit_price": "0.80",
        "estimated_close_debit": "80.00",
        "close_recommended": True,
    }


def close_positions() -> list[dict]:
    return [
        {"symbol": "AAPL260612P00305000", "qty": "1", "side": "short"},
        {"symbol": "AAPL260612P00300000", "qty": "1", "side": "long"},
    ]


def open_spread_positions() -> list[dict]:
    return [
        {
            "symbol": "AAPL260612P00305000",
            "qty": "1",
            "side": "short",
            "avg_entry_price": "2.00",
        },
        {
            "symbol": "AAPL260612P00300000",
            "qty": "1",
            "side": "long",
            "avg_entry_price": "0.95",
        },
        {
            "symbol": "NVDA260618P00210000",
            "qty": "1",
            "side": "short",
            "avg_entry_price": "2.95",
        },
        {
            "symbol": "NVDA260618P00205000",
            "qty": "1",
            "side": "long",
            "avg_entry_price": "1.83",
        },
    ]


def inactive_kill_switch() -> KillSwitch:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "KILL_SWITCH"
    return KillSwitch(path)


class ExecutionGateTests(unittest.TestCase):
    def test_blocks_without_submit_flag(self) -> None:
        config = config_with_paper_orders_enabled(True)
        alpaca = FakeAlpacaSubmitClient()

        attempt = maybe_submit_paper_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=False,
            order_preview=clean_preview(config),
            open_orders=[],
            open_positions=[],
            allocation=allocation(),
        )

        self.assertFalse(attempt.submitted)
        self.assertIn("CLI did not request --submit-paper", attempt.block_reasons)
        self.assertEqual(alpaca.submitted_payloads, [])

    def test_blocks_when_config_flag_is_false(self) -> None:
        config = config_with_paper_orders_enabled(False)
        alpaca = FakeAlpacaSubmitClient()

        attempt = maybe_submit_paper_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            order_preview=clean_preview(config),
            open_orders=[],
            open_positions=[],
            allocation=allocation(),
        )

        self.assertFalse(attempt.submitted)
        self.assertIn("execution.enable_paper_orders is false", attempt.block_reasons)
        self.assertEqual(alpaca.submitted_payloads, [])

    def test_submits_when_all_guards_pass(self) -> None:
        config = config_with_paper_orders_enabled(True)
        alpaca = FakeAlpacaSubmitClient()
        preview = clean_preview(config)

        attempt = maybe_submit_paper_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            order_preview=preview,
            open_orders=[],
            open_positions=[],
            allocation=allocation(),
        )

        self.assertTrue(attempt.submitted)
        self.assertEqual(attempt.status, "submitted")
        self.assertEqual(alpaca.submitted_payloads, [preview["payload"]])
        self.assertEqual(attempt.broker_response, {"id": "paper-order-1", "status": "accepted"})

    def test_position_cap_counts_spread_contracts_not_option_legs(self) -> None:
        config = config_with_paper_orders_enabled(True)
        values = copy.deepcopy(config.values)
        values.setdefault("risk", {})["max_open_positions"] = 3
        values["risk"]["max_open_risk"] = 5000
        config = AppConfig(settings_path=config.settings_path, values=values)
        alpaca = FakeAlpacaSubmitClient()
        preview = clean_preview(config)

        attempt = maybe_submit_paper_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            order_preview=preview,
            open_orders=[],
            open_positions=open_spread_positions(),
            allocation=allocation(),
        )

        self.assertTrue(attempt.submitted)
        self.assertEqual(alpaca.submitted_payloads, [preview["payload"]])

    def test_blocks_when_projected_open_risk_exceeds_budget(self) -> None:
        config = config_with_paper_orders_enabled(True)
        values = copy.deepcopy(config.values)
        values.setdefault("risk", {})["max_open_positions"] = 50
        values["risk"]["max_open_risk"] = 1000
        config = AppConfig(settings_path=config.settings_path, values=values)
        alpaca = FakeAlpacaSubmitClient()

        attempt = maybe_submit_paper_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            order_preview=clean_preview(config),
            open_orders=[],
            open_positions=open_spread_positions(),
            allocation=allocation(),
        )

        self.assertFalse(attempt.submitted)
        self.assertTrue(any("Projected open risk" in reason for reason in attempt.block_reasons))
        self.assertEqual(alpaca.submitted_payloads, [])

    def test_blocks_when_account_risk_gate_blocks_new_opens(self) -> None:
        config = config_with_paper_orders_enabled(True)
        alpaca = FakeAlpacaSubmitClient()

        attempt = maybe_submit_paper_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            order_preview=clean_preview(config),
            open_orders=[],
            open_positions=[],
            allocation=allocation(),
            account_risk_state={
                "blocks_new_opens": True,
                "block_reasons": ["Daily P&L -600 breaches max_daily_loss 500"],
            },
        )

        self.assertFalse(attempt.submitted)
        self.assertIn(
            "Account risk gate: Daily P&L -600 breaches max_daily_loss 500",
            attempt.block_reasons,
        )
        self.assertEqual(alpaca.submitted_payloads, [])

    def test_blocks_when_final_state_refresh_failed(self) -> None:
        config = config_with_paper_orders_enabled(True)
        alpaca = FakeAlpacaSubmitClient()

        attempt = maybe_submit_paper_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            order_preview=clean_preview(config),
            open_orders=[],
            open_positions=[],
            allocation=allocation(),
            state_refresh_error="broker unavailable",
        )

        self.assertFalse(attempt.submitted)
        self.assertTrue(any("state refresh failed" in reason for reason in attempt.block_reasons))
        self.assertEqual(alpaca.submitted_payloads, [])

    def test_records_execution_attempt(self) -> None:
        config = config_with_paper_orders_enabled(False)
        attempt = maybe_submit_paper_order(
            config=config,
            alpaca=FakeAlpacaSubmitClient(),
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            order_preview=clean_preview(config),
            open_orders=[],
            open_positions=[],
            allocation=allocation(),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)
            attempt_id = record_execution_attempt(
                db_path,
                created_at=datetime.now(UTC).isoformat(),
                mode="paper",
                decision_id=1,
                attempt=attempt,
            )

        self.assertEqual(attempt_id, 1)

    def test_close_blocks_without_submit_close_flag(self) -> None:
        config = config_with_paper_close_orders_enabled(True)
        alpaca = FakeAlpacaSubmitClient()

        attempt = maybe_submit_paper_close_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=False,
            spread=close_spread(),
            order_preview=clean_close_preview(config),
            open_orders=[],
            open_positions=close_positions(),
        )

        self.assertFalse(attempt.submitted)
        self.assertIn("CLI did not request --submit-paper-close", attempt.block_reasons)
        self.assertEqual(alpaca.submitted_payloads, [])

    def test_close_blocks_when_config_flag_is_false(self) -> None:
        config = config_with_paper_close_orders_enabled(False)
        alpaca = FakeAlpacaSubmitClient()

        attempt = maybe_submit_paper_close_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            spread=close_spread(),
            order_preview=clean_close_preview(config),
            open_orders=[],
            open_positions=close_positions(),
        )

        self.assertFalse(attempt.submitted)
        self.assertIn("execution.enable_paper_close_orders is false", attempt.block_reasons)
        self.assertEqual(alpaca.submitted_payloads, [])

    def test_close_submits_when_all_guards_pass(self) -> None:
        config = config_with_paper_close_orders_enabled(True)
        alpaca = FakeAlpacaSubmitClient()
        preview = clean_close_preview(config)

        attempt = maybe_submit_paper_close_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            spread=close_spread(),
            order_preview=preview,
            open_orders=[],
            open_positions=close_positions(),
        )

        self.assertTrue(attempt.submitted)
        self.assertEqual(attempt.status, "submitted")
        self.assertEqual(alpaca.submitted_payloads, [preview["payload"]])

    def test_close_blocks_duplicate_leg_order(self) -> None:
        config = config_with_paper_close_orders_enabled(True)
        alpaca = FakeAlpacaSubmitClient()

        attempt = maybe_submit_paper_close_order(
            config=config,
            alpaca=alpaca,
            kill_switch=inactive_kill_switch(),
            notifier=DiscordNotifier("https://discord.test/webhook"),
            submit_requested=True,
            spread=close_spread(),
            order_preview=clean_close_preview(config),
            open_orders=[
                {
                    "id": "existing-close",
                    "legs": [{"symbol": "AAPL260612P00305000"}],
                }
            ],
            open_positions=close_positions(),
        )

        self.assertFalse(attempt.submitted)
        self.assertIn(
            "Duplicate close order already exists for at least one spread leg",
            attempt.block_reasons,
        )
        self.assertEqual(alpaca.submitted_payloads, [])


if __name__ == "__main__":
    unittest.main()

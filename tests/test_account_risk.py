from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from trading_bot.config import AppConfig, load_config
from trading_bot.risk.account import build_account_risk_state
from trading_bot.storage.db import init_db, record_account_snapshot, record_execution_attempt


def risk_config(**risk_overrides) -> AppConfig:
    base = load_config("config/settings.yaml")
    values = copy.deepcopy(base.values)
    values.setdefault("risk", {}).update(risk_overrides)
    values.setdefault("account", {})["emergency_equity_floor"] = 8000
    return AppConfig(settings_path=base.settings_path, values=values)


def account(*, equity: str = "100000", last_equity: str = "100000") -> dict:
    return {
        "status": "ACTIVE",
        "equity": equity,
        "last_equity": last_equity,
        "buying_power": "200000",
        "portfolio_value": equity,
    }


class AccountRiskTests(unittest.TestCase):
    def test_daily_loss_blocks_new_opens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)

            state = build_account_risk_state(
                config=risk_config(max_daily_loss=500),
                db_path=db_path,
                account=account(equity="99400", last_equity="100000"),
                now=datetime(2026, 6, 4, 15, 0, tzinfo=UTC),
            )

        self.assertTrue(state.blocks_new_opens)
        self.assertTrue(any("max_daily_loss" in reason for reason in state.block_reasons))
        self.assertEqual(state.daily_pnl, "-600.00")

    def test_new_trade_limit_blocks_after_limit_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)
            for index in range(3):
                record_execution_attempt(
                    db_path,
                    created_at=f"2026-06-04T15:0{index}:00+00:00",
                    mode="paper",
                    decision_id=None,
                    attempt={
                        "requested": True,
                        "submitted": True,
                        "status": "filled",
                        "order_preview": {"kind": "alpaca_mleg_order_preview"},
                        "order_payload": {"legs": [{"position_intent": "sell_to_open"}]},
                        "broker_response": {"id": f"order-{index}"},
                        "broker_error": None,
                        "block_reasons": [],
                    },
                )

            state = build_account_risk_state(
                config=risk_config(max_new_trades_per_day=3),
                db_path=db_path,
                account=account(),
                now=datetime(2026, 6, 4, 16, 0, tzinfo=UTC),
            )

        self.assertTrue(state.blocks_new_opens)
        self.assertEqual(state.new_trades_today, 3)
        self.assertTrue(any("New trade limit reached" in reason for reason in state.block_reasons))

    def test_weekly_loss_uses_first_snapshot_in_eastern_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)
            record_account_snapshot(
                db_path,
                captured_at="2026-06-01T14:00:00+00:00",
                broker="alpaca",
                mode="paper",
                payload=account(equity="100000", last_equity="100000"),
            )

            state = build_account_risk_state(
                config=risk_config(max_weekly_loss=1000),
                db_path=db_path,
                account=account(equity="98900", last_equity="99000"),
                now=datetime(2026, 6, 4, 16, 0, tzinfo=UTC),
            )

        self.assertTrue(state.blocks_new_opens)
        self.assertEqual(state.weekly_pnl, "-1100.00")
        self.assertTrue(any("max_weekly_loss" in reason for reason in state.block_reasons))


if __name__ == "__main__":
    unittest.main()

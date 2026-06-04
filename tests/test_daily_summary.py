from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from trading_bot.config import load_config
from trading_bot.storage.db import init_db
from trading_bot.summaries.daily import _build_daily_trading_summary


class FakeAlpaca:
    def get_account(self) -> dict:
        return {
            "status": "ACTIVE",
            "equity": "100500",
            "last_equity": "100000",
            "buying_power": "200000",
            "cash": "100000",
            "portfolio_value": "100500",
        }

    def get_positions(self) -> list[dict]:
        return []

    def get_orders(self, *, status: str = "open", limit: int = 50) -> list[dict]:
        return []


class DailySummaryTests(unittest.TestCase):
    def test_builds_summary_with_empty_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)

            artifact = _build_daily_trading_summary(
                config=load_config("config/settings.yaml"),
                db_path=db_path,
                alpaca=FakeAlpaca(),
                option_feed=None,
                summary_date=date(2026, 6, 4),
                order_poll={"orders": [], "order_count": 0, "change_count": 0},
            )

        self.assertEqual(artifact["account"]["daily_pnl"], "500.00")
        self.assertEqual(artifact["positions"]["spread_count"], 0)
        self.assertEqual(artifact["orders"]["open_order_count"], 0)


if __name__ == "__main__":
    unittest.main()

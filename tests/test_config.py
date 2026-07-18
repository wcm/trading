from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.config import load_config, load_env_file
from trading_bot.cli.parser import build_parser
from trading_bot.commands.backtesting import _grid_config_from_args
from trading_bot.dca.strategy import dca_config_from_app_config
from trading_bot.grid.strategy import grid_config_from_app_config


class ConfigTests(unittest.TestCase):
    def test_load_config(self) -> None:
        config = load_config("config/settings.yaml")
        self.assertEqual(config.mode, "paper")
        self.assertEqual(config.broker, "alpaca")
        self.assertEqual(config.get("notifications", "provider"), "discord")

    def test_load_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("TEST_TRADING_BOT_VALUE=hello\n", encoding="utf-8")
            try:
                loaded = load_env_file(env_path)
                self.assertEqual(loaded, 1)
                self.assertEqual(os.environ["TEST_TRADING_BOT_VALUE"], "hello")
            finally:
                os.environ.pop("TEST_TRADING_BOT_VALUE", None)

    def test_load_dca_config(self) -> None:
        config = load_config("config/settings.dca.yaml")
        strategy = dca_config_from_app_config(config)

        self.assertEqual(strategy.name, "dca_tqqq")
        self.assertEqual(strategy.symbol, "TQQQ")
        self.assertEqual(strategy.frequency, "biweekly")
        self.assertEqual(strategy.sizing_mode, "drawdown_scaled")
        self.assertEqual(strategy.drawdown_scale_factor, 4)
        self.assertEqual(strategy.drawdown_lookback_days, 365)
        self.assertEqual(strategy.max_contribution_multiplier, Decimal("5"))
        self.assertEqual(strategy.max_contribution_per_purchase, Decimal("1250"))
        self.assertIsNone(strategy.max_annual_contribution)
        self.assertFalse(config.get("execution", "enable_paper_orders"))

    def test_load_grid_config_enables_fractional_shares(self) -> None:
        config = load_config("config/settings.grid.yaml")
        strategy = grid_config_from_app_config(config)

        self.assertTrue(strategy.allow_fractional_shares)

    def test_grid_backtest_inherits_and_can_override_fractional_setting(self) -> None:
        config = load_config("config/settings.grid.yaml")

        configured_args = build_parser().parse_args(["backtest-grid"])
        whole_share_args = build_parser().parse_args(["backtest-grid", "--whole-shares"])

        self.assertTrue(_grid_config_from_args(configured_args, config).allow_fractional_shares)
        self.assertFalse(_grid_config_from_args(whole_share_args, config).allow_fractional_shares)


if __name__ == "__main__":
    unittest.main()

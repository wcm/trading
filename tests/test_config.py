from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from trading_bot.config import load_config, load_env_file


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


if __name__ == "__main__":
    unittest.main()


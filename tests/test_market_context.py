from __future__ import annotations

import unittest

from trading_bot.config import load_config
from trading_bot.data.market_data import build_market_context
from tests.test_put_credit_spread import FakeAlpacaClient


class MarketContextTests(unittest.TestCase):
    def test_stale_latest_bar_blocks_market_trend_ok(self) -> None:
        config = load_config("config/settings.yaml")
        context = build_market_context(config=config, alpaca=FakeAlpacaClient(), symbols=["AAPL"])
        symbol_context = context.symbols["AAPL"]

        self.assertFalse(symbol_context.latest_bar_fresh)
        self.assertFalse(symbol_context.market_trend_ok)


if __name__ == "__main__":
    unittest.main()


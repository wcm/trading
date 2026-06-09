from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

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

    def test_broad_market_symbol_is_included_and_evaluated(self) -> None:
        class FreshBarsAlpacaClient(FakeAlpacaClient):
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
                            "t": (now - timedelta(minutes=24 - index)).isoformat(),
                            "o": str(100 + index - 1),
                            "h": str(close + 1),
                            "l": str(close - 1),
                            "c": str(close),
                            "v": 1000,
                        }
                    )
                return {symbol: bars for symbol in symbols}

        config = load_config("config/settings.yaml")
        context = build_market_context(config=config, alpaca=FreshBarsAlpacaClient(), symbols=["AAPL"])

        self.assertIn("AAPL", context.symbols)
        self.assertIn("QQQ", context.symbols)
        self.assertEqual(context.broad_market_symbol, "QQQ")
        self.assertTrue(context.broad_market_filter_ok)


if __name__ == "__main__":
    unittest.main()

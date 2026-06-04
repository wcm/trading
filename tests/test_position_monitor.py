from __future__ import annotations

import unittest
from typing import Any

from trading_bot.config import load_config
from trading_bot.monitoring.positions import monitor_put_credit_spreads, parse_occ_option_symbol


class FakeMonitorAlpacaClient:
    def get_positions(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "AAPL991231P00305000",
                "asset_class": "us_option",
                "qty": "1",
                "side": "short",
                "avg_entry_price": "2.00",
                "cost_basis": "-200.00",
            },
            {
                "symbol": "AAPL991231P00300000",
                "asset_class": "us_option",
                "qty": "1",
                "side": "long",
                "avg_entry_price": "0.95",
                "cost_basis": "95.00",
            },
        ]

    def get_option_snapshots(
        self,
        symbols: list[str],
        *,
        feed: str = "indicative",
        chunk_size: int = 100,
    ) -> dict[str, dict[str, Any]]:
        return {
            "AAPL991231P00305000": {
                "latestQuote": {"bp": "1.10", "ap": "1.20", "t": "2026-06-04T14:00:00Z"}
            },
            "AAPL991231P00300000": {
                "latestQuote": {"bp": "0.40", "ap": "0.50", "t": "2026-06-04T14:00:00Z"}
            },
        }

    def get_latest_stock_bars(self, symbols: list[str], *, feed: str = "iex") -> dict[str, Any]:
        return {symbol: {"c": "312.00"} for symbol in symbols}


class ProfitableFakeMonitorAlpacaClient(FakeMonitorAlpacaClient):
    def get_option_snapshots(
        self,
        symbols: list[str],
        *,
        feed: str = "indicative",
        chunk_size: int = 100,
    ) -> dict[str, dict[str, Any]]:
        return {
            "AAPL991231P00305000": {
                "latestQuote": {"bp": "0.80", "ap": "0.90", "t": "2026-06-04T14:00:00Z"}
            },
            "AAPL991231P00300000": {
                "latestQuote": {"bp": "0.50", "ap": "0.60", "t": "2026-06-04T14:00:00Z"}
            },
        }


class PositionMonitorTests(unittest.TestCase):
    def test_parse_occ_option_symbol(self) -> None:
        parsed = parse_occ_option_symbol("AAPL991231P00305000")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.underlying_symbol, "AAPL")
        self.assertEqual(parsed.expiration_date.isoformat(), "2099-12-31")
        self.assertEqual(parsed.option_type, "put")
        self.assertEqual(str(parsed.strike), "305")

    def test_monitor_groups_put_credit_spread_and_marks_pnl(self) -> None:
        config = load_config("config/settings.yaml")
        result = monitor_put_credit_spreads(
            config=config,
            alpaca=FakeMonitorAlpacaClient(),
            option_feed="indicative",
        )

        self.assertEqual(result.option_position_count, 2)
        self.assertEqual(result.spread_count, 1)
        self.assertEqual(result.unpaired_legs, [])
        spread = result.spreads[0]
        self.assertEqual(spread["spread_id"], "AAPL-2099-12-31-305P-300P")
        self.assertEqual(spread["entry_credit"], "1.05")
        self.assertEqual(spread["close_debit"], "0.8")
        self.assertEqual(spread["estimated_unrealized_pnl"], "25")
        self.assertFalse(spread["close_recommended"])
        self.assertEqual(spread["close_order_preview"]["payload"]["limit_price"], "0.8")

    def test_monitor_flags_profit_target(self) -> None:
        config = load_config("config/settings.yaml")
        result = monitor_put_credit_spreads(
            config=config,
            alpaca=ProfitableFakeMonitorAlpacaClient(),
            option_feed="indicative",
        )

        spread = result.spreads[0]
        self.assertEqual(spread["close_debit"], "0.4")
        self.assertTrue(spread["exit_flags"]["profit_target_hit"])
        self.assertTrue(spread["close_recommended"])


if __name__ == "__main__":
    unittest.main()

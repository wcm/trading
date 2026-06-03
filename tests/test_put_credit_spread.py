from __future__ import annotations

import unittest
from typing import Any

from trading_bot.config import load_config
from trading_bot.strategy.put_credit_spread import scan_put_credit_spreads


class FakeAlpacaClient:
    def get_latest_stock_bars(self, symbols: list[str], *, feed: str = "iex") -> dict[str, Any]:
        return {symbol: {"c": "100.00"} for symbol in symbols}

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
        bars = []
        for index in range(25):
            close = 100 + index
            bars.append(
                {
                    "t": f"2026-06-03T{10 + index // 2:02d}:{(index % 2) * 30:02d}:00Z",
                    "o": str(100 + index - 1),
                    "h": str(close + 1),
                    "l": str(close - 1),
                    "c": str(close),
                    "v": 1000,
                }
            )
        return {symbol: bars for symbol in symbols}

    def get_news(
        self,
        symbols: list[str],
        *,
        start: str,
        end: str | None = None,
        limit: int = 10,
        include_content: bool = False,
        sort: str = "desc",
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "headline": "AAPL shares steady in broad tech session",
                "summary": "A neutral test news item.",
                "author": "Test",
                "created_at": "2026-06-03T14:00:00Z",
                "updated_at": "2026-06-03T14:05:00Z",
                "symbols": symbols,
                "url": "https://example.com/test",
            }
        ][:limit]

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
        return [
            {
                "symbol": "AAPL260619P00095000",
                "underlying_symbol": "AAPL",
                "type": "put",
                "status": "active",
                "tradable": True,
                "expiration_date": "2026-06-19",
                "strike_price": "95",
            },
            {
                "symbol": "AAPL260619P00090000",
                "underlying_symbol": "AAPL",
                "type": "put",
                "status": "active",
                "tradable": True,
                "expiration_date": "2026-06-19",
                "strike_price": "90",
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
            "AAPL260619P00095000": {
                "latestQuote": {"bp": "1.50", "ap": "1.60", "t": "2026-06-03T14:00:00Z"},
                "greeks": {"delta": "-0.25", "gamma": "0.01", "theta": "-0.02", "vega": "0.10"},
            },
            "AAPL260619P00090000": {
                "latestQuote": {"bp": "0.30", "ap": "0.40", "t": "2026-06-03T14:00:00Z"},
                "greeks": {"delta": "-0.15", "gamma": "0.01", "theta": "-0.01", "vega": "0.08"},
            },
        }


class PutCreditSpreadScanTests(unittest.TestCase):
    def test_scan_builds_candidate_from_fake_data(self) -> None:
        config = load_config("config/settings.yaml")
        result = scan_put_credit_spreads(
            config=config,
            alpaca=FakeAlpacaClient(),
            symbols=["AAPL"],
            max_candidates=5,
            option_feed="indicative",
        )

        self.assertEqual(result.contracts_seen, 2)
        self.assertEqual(result.snapshots_seen, 2)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.underlying_symbol, "AAPL")
        self.assertEqual(candidate.net_credit, "1.10")
        self.assertEqual(candidate.max_loss, "390.00")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from trading_bot.config import load_config
from trading_bot.execution.orders import build_put_credit_spread_order_preview


def valid_open_decision() -> dict:
    return {
        "action": "open",
        "symbol": "AAPL",
        "candidate_id": "AAPL-2026-06-12-305.00P-300.00P",
        "quantity": 1,
        "limit_price": "-1.05",
    }


def valid_candidate() -> dict:
    return {
        "candidate_id": "AAPL-2026-06-12-305.00P-300.00P",
        "underlying_symbol": "AAPL",
        "short_put_symbol": "AAPL260612P00305000",
        "long_put_symbol": "AAPL260612P00300000",
        "short_strike": "305.00",
        "long_strike": "300.00",
        "net_credit": "1.05",
        "max_profit": "105.00",
        "max_loss": "395.00",
    }


class OrderPreviewTests(unittest.TestCase):
    def test_builds_alpaca_mleg_preview_for_put_credit_spread(self) -> None:
        config = load_config("config/settings.yaml")
        preview = build_put_credit_spread_order_preview(
            config=config,
            decision=valid_open_decision(),
            candidate=valid_candidate(),
            client_order_id="preview-aapl-test-001",
        )

        self.assertEqual(preview["errors"], [])
        self.assertTrue(preview["submit_disabled"])
        self.assertEqual(preview["submit_endpoint"], "/v2/orders")
        self.assertEqual(preview["estimated_entry_credit"], "105")
        self.assertEqual(preview["estimated_max_loss"], "395")

        payload = preview["payload"]
        self.assertEqual(payload["order_class"], "mleg")
        self.assertEqual(payload["qty"], "1")
        self.assertEqual(payload["type"], "limit")
        self.assertEqual(payload["limit_price"], "-1.05")
        self.assertEqual(payload["time_in_force"], "day")
        self.assertEqual(payload["client_order_id"], "preview-aapl-test-001")
        self.assertEqual(
            payload["legs"],
            [
                {
                    "symbol": "AAPL260612P00305000",
                    "ratio_qty": "1",
                    "side": "sell",
                    "position_intent": "sell_to_open",
                },
                {
                    "symbol": "AAPL260612P00300000",
                    "ratio_qty": "1",
                    "side": "buy",
                    "position_intent": "buy_to_open",
                },
            ],
        )

    def test_rejects_positive_credit_limit_price(self) -> None:
        config = load_config("config/settings.yaml")
        decision = valid_open_decision()
        decision["limit_price"] = "1.05"

        preview = build_put_credit_spread_order_preview(
            config=config,
            decision=decision,
            candidate=valid_candidate(),
            client_order_id="preview-aapl-test-001",
        )

        self.assertTrue(any("must be negative" in error for error in preview["errors"]))


if __name__ == "__main__":
    unittest.main()

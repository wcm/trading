from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime

from trading_bot.config import AppConfig, load_config
from trading_bot.execution.entry_orders import manage_entry_order_after_submission
from trading_bot.execution.revalidation import revalidate_put_credit_spread_entry_preview
from trading_bot.execution.orders import build_put_credit_spread_order_preview
from tests.test_order_preview import valid_candidate, valid_open_decision


def execution_config(**execution_overrides: object) -> AppConfig:
    base = load_config("config/settings.yaml")
    values = copy.deepcopy(base.values)
    values.setdefault("execution", {}).update(execution_overrides)
    return AppConfig(settings_path=base.settings_path, values=values)


def snapshot(*, bid: str, ask: str) -> dict:
    return {
        "latestQuote": {
            "bp": bid,
            "ap": ask,
            "t": datetime.now(UTC).isoformat(),
        }
    }


def clean_preview(config: AppConfig) -> dict:
    candidate = valid_candidate()
    candidate["width"] = "5.00"
    return build_put_credit_spread_order_preview(
        config=config,
        decision=valid_open_decision(),
        candidate=candidate,
        client_order_id="preview-aapl-test-001",
    )


class FakeQuoteClient:
    def __init__(self, *, short_bid: str, short_ask: str, long_bid: str, long_ask: str) -> None:
        self.snapshots = {
            "AAPL260612P00305000": snapshot(bid=short_bid, ask=short_ask),
            "AAPL260612P00300000": snapshot(bid=long_bid, ask=long_ask),
        }

    def get_option_snapshots(self, symbols: list[str], *, feed: str = "opra") -> dict:
        return {symbol: self.snapshots[symbol] for symbol in symbols}


class FakeManagedAlpaca(FakeQuoteClient):
    def __init__(self) -> None:
        super().__init__(short_bid="2.00", short_ask="2.05", long_bid="0.95", long_ask="0.97")
        self.orders: dict[str, dict] = {
            "order-1": {
                "id": "order-1",
                "client_order_id": "preview-aapl-test-001",
                "status": "new",
                "filled_qty": "0",
                "qty": "1",
                "limit_price": "-1.05",
            }
        }
        self.submitted_payloads: list[dict] = []
        self.cancelled_order_ids: list[str] = []

    def get_order(self, order_id: str) -> dict:
        return dict(self.orders[order_id])

    def cancel_order(self, order_id: str) -> dict:
        self.cancelled_order_ids.append(order_id)
        self.orders[order_id]["status"] = "canceled"
        self.orders[order_id]["canceled_at"] = datetime.now(UTC).isoformat()
        return {}

    def submit_order(self, payload: dict) -> dict:
        self.submitted_payloads.append(payload)
        order_id = f"order-{len(self.orders) + 1}"
        order = {
            "id": order_id,
            "client_order_id": payload.get("client_order_id"),
            "status": "new",
            "filled_qty": "0",
            "qty": payload.get("qty"),
            "limit_price": payload.get("limit_price"),
        }
        self.orders[order_id] = order
        return dict(order)


class EntryExecutionManagementTests(unittest.TestCase):
    def test_revalidation_relaxes_limit_within_minimum_credit(self) -> None:
        config = execution_config(entry_limit_credit_buffer=0.05)
        preview = clean_preview(config)
        alpaca = FakeQuoteClient(
            short_bid="2.00",
            short_ask="2.05",
            long_bid="0.95",
            long_ask="0.97",
        )

        refreshed = revalidate_put_credit_spread_entry_preview(
            config=config,
            alpaca=alpaca,
            order_preview=preview,
        )

        self.assertEqual(refreshed["errors"], [])
        self.assertTrue(refreshed["revalidation"]["ok"])
        self.assertEqual(refreshed["revalidation"]["current_net_credit"], "1.03")
        self.assertEqual(refreshed["payload"]["limit_price"], "-1")
        self.assertEqual(refreshed["estimated_entry_credit"], "100")
        self.assertEqual(refreshed["estimated_max_loss"], "400")

    def test_revalidation_blocks_when_current_credit_is_below_minimum(self) -> None:
        config = execution_config(entry_limit_credit_buffer=0.05)
        preview = clean_preview(config)
        alpaca = FakeQuoteClient(
            short_bid="1.70",
            short_ask="1.75",
            long_bid="0.95",
            long_ask="0.97",
        )

        refreshed = revalidate_put_credit_spread_entry_preview(
            config=config,
            alpaca=alpaca,
            order_preview=preview,
        )

        self.assertFalse(refreshed["revalidation"]["ok"])
        self.assertTrue(any("below minimum acceptable credit" in error for error in refreshed["errors"]))

    def test_entry_manager_cancels_and_replaces_stale_unfilled_order(self) -> None:
        config = execution_config(
            entry_order_timeout_seconds=0,
            entry_order_poll_seconds=1,
            max_entry_price_adjustments=1,
            manage_entry_orders=True,
        )
        preview = clean_preview(config)
        alpaca = FakeManagedAlpaca()

        result = manage_entry_order_after_submission(
            config=config,
            alpaca=alpaca,
            order_preview=preview,
            initial_order=alpaca.get_order("order-1"),
        )

        self.assertEqual(result["final_status"], "canceled_unfilled")
        self.assertEqual(alpaca.cancelled_order_ids, ["order-1", "order-2"])
        self.assertEqual(len(alpaca.submitted_payloads), 1)
        self.assertEqual(alpaca.submitted_payloads[0]["client_order_id"], "preview-aapl-test-001-r1")
        self.assertEqual(alpaca.submitted_payloads[0]["limit_price"], "-1")


if __name__ == "__main__":
    unittest.main()

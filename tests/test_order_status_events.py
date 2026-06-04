from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trading_bot.storage.db import (
    init_db,
    record_execution_attempt,
    record_order_status_changes,
    summarize_execution_attempts,
    summarize_order_status_events,
)


def order_payload(*, status: str, filled_qty: str = "0") -> dict:
    return {
        "id": "order-1",
        "client_order_id": "bot-order-1",
        "symbol": "AAPL",
        "status": status,
        "filled_qty": filled_qty,
        "qty": "1",
        "order_class": "mleg",
        "side": None,
        "submitted_at": "2026-06-04T14:00:00Z",
    }


class OrderStatusEventTests(unittest.TestCase):
    def test_records_only_new_status_or_fill_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)

            first = record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:01:00Z",
                mode="paper",
                orders=[order_payload(status="new")],
            )
            duplicate = record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:02:00Z",
                mode="paper",
                orders=[order_payload(status="new")],
            )
            fill_change = record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:03:00Z",
                mode="paper",
                orders=[order_payload(status="partially_filled", filled_qty="0.5")],
            )

        self.assertEqual(len(first), 1)
        self.assertIsNone(first[0]["previous_status"])
        self.assertEqual(first[0]["status"], "new")
        self.assertEqual(duplicate, [])
        self.assertEqual(len(fill_change), 1)
        self.assertEqual(fill_change[0]["previous_status"], "new")
        self.assertEqual(fill_change[0]["status"], "partially_filled")
        self.assertEqual(fill_change[0]["previous_filled_qty"], "0")
        self.assertEqual(fill_change[0]["filled_qty"], "0.5")

    def test_skips_orders_without_broker_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)

            changes = record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:01:00Z",
                mode="paper",
                orders=[{"status": "new"}],
            )

        self.assertEqual(changes, [])

    def test_tracks_order_ids_separately_by_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)

            paper = record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:01:00Z",
                mode="paper",
                orders=[order_payload(status="new")],
            )
            live = record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:02:00Z",
                mode="live",
                orders=[order_payload(status="new")],
            )

        self.assertEqual(len(paper), 1)
        self.assertEqual(len(live), 1)
        self.assertIsNone(live[0]["previous_status"])

    def test_summarizes_order_status_events_for_daily_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)
            record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:01:00+00:00",
                mode="paper",
                orders=[order_payload(status="new")],
            )
            record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:02:00+00:00",
                mode="paper",
                orders=[order_payload(status="filled", filled_qty="1")],
            )

            summary = summarize_order_status_events(
                db_path,
                mode="paper",
                start_at="2026-06-04T00:00:00+00:00",
                end_at="2026-06-05T00:00:00+00:00",
            )

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_status"], {"filled": 1, "new": 1})
        self.assertEqual(summary["recent"][0]["status"], "filled")

    def test_summarizes_execution_attempts_for_daily_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)

            record_execution_attempt(
                db_path,
                created_at="2026-06-04T14:01:00+00:00",
                mode="paper",
                decision_id=None,
                attempt={
                    "requested": True,
                    "submitted": True,
                    "status": "submitted",
                    "order_preview": None,
                    "order_payload": {"order_class": "mleg"},
                    "broker_response": {"id": "order-1"},
                    "broker_error": None,
                    "block_reasons": [],
                },
            )

            summary = summarize_execution_attempts(
                db_path,
                mode="paper",
                start_at="2026-06-04T00:00:00+00:00",
                end_at="2026-06-05T00:00:00+00:00",
            )

        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["requested"], 1)
        self.assertEqual(summary["submitted"], 1)
        self.assertEqual(summary["by_status"], {"submitted": 1})


if __name__ == "__main__":
    unittest.main()

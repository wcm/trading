from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from trading_bot.storage.db import (
    count_submitted_open_attempts,
    init_db,
    list_spread_trades,
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


def open_spread_order_payload() -> dict:
    return {
        "id": "open-order-1",
        "client_order_id": "preview-aapl-001",
        "symbol": "",
        "status": "filled",
        "filled_qty": "1",
        "filled_avg_price": "-1.05",
        "qty": "1",
        "order_class": "mleg",
        "submitted_at": "2026-06-04T14:00:00Z",
        "filled_at": "2026-06-04T14:00:02Z",
        "legs": [
            {
                "symbol": "AAPL260612P00305000",
                "side": "sell",
                "position_intent": "sell_to_open",
                "filled_avg_price": "2.00",
            },
            {
                "symbol": "AAPL260612P00300000",
                "side": "buy",
                "position_intent": "buy_to_open",
                "filled_avg_price": "0.95",
            },
        ],
    }


def close_spread_order_payload() -> dict:
    return {
        "id": "close-order-1",
        "client_order_id": "close-preview-aapl-001",
        "symbol": "",
        "status": "filled",
        "filled_qty": "1",
        "filled_avg_price": "0.40",
        "qty": "1",
        "order_class": "mleg",
        "submitted_at": "2026-06-04T15:00:00Z",
        "filled_at": "2026-06-04T15:00:02Z",
        "legs": [
            {
                "symbol": "AAPL260612P00305000",
                "side": "buy",
                "position_intent": "buy_to_close",
                "filled_avg_price": "0.80",
            },
            {
                "symbol": "AAPL260612P00300000",
                "side": "sell",
                "position_intent": "sell_to_close",
                "filled_avg_price": "0.40",
            },
        ],
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

    def test_counts_submitted_open_attempts_only(self) -> None:
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
                    "status": "filled",
                    "order_preview": {"kind": "alpaca_mleg_order_preview"},
                    "order_payload": {"legs": [{"position_intent": "sell_to_open"}]},
                    "broker_response": {"id": "open-order-1"},
                    "broker_error": None,
                    "block_reasons": [],
                },
            )
            record_execution_attempt(
                db_path,
                created_at="2026-06-04T14:02:00+00:00",
                mode="paper",
                decision_id=None,
                attempt={
                    "requested": True,
                    "submitted": True,
                    "status": "submitted",
                    "order_preview": {"kind": "alpaca_mleg_close_preview"},
                    "order_payload": {"legs": [{"position_intent": "buy_to_close"}]},
                    "broker_response": {"id": "close-order-1"},
                    "broker_error": None,
                    "block_reasons": [],
                },
            )

            count = count_submitted_open_attempts(
                db_path,
                mode="paper",
                start_at="2026-06-04T00:00:00+00:00",
                end_at="2026-06-05T00:00:00+00:00",
            )

        self.assertEqual(count, 1)

    def test_records_spread_trade_from_filled_open_and_close_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)

            record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:01:00+00:00",
                mode="paper",
                orders=[open_spread_order_payload()],
            )
            open_trades = list_spread_trades(db_path, mode="paper", status="open")
            close_changes = record_order_status_changes(
                db_path,
                observed_at="2026-06-04T15:01:00+00:00",
                mode="paper",
                orders=[close_spread_order_payload()],
            )
            closed_trades = list_spread_trades(db_path, mode="paper", status="closed")

        self.assertEqual(len(open_trades), 1)
        self.assertEqual(open_trades[0]["spread_id"], "AAPL-2026-06-12-305P-300P")
        self.assertEqual(open_trades[0]["entry_credit"], "1.05")
        self.assertEqual(open_trades[0]["max_loss"], "395")
        self.assertEqual(len(closed_trades), 1)
        self.assertEqual(closed_trades[0]["close_debit"], "0.4")
        self.assertEqual(closed_trades[0]["status"], "closed")
        self.assertEqual(len(close_changes), 1)
        self.assertEqual(close_changes[0]["spread_trade"]["entry_credit"], "1.05")
        self.assertEqual(close_changes[0]["spread_trade"]["close_debit"], "0.4")
        self.assertEqual(close_changes[0]["spread_trade"]["realized_pnl"], "65")

    def test_backfills_spread_trade_when_filled_order_status_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.sqlite3"
            init_db(db_path)
            record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:01:00+00:00",
                mode="paper",
                orders=[open_spread_order_payload()],
            )
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("DELETE FROM spread_trades")
                conn.commit()

            changes = record_order_status_changes(
                db_path,
                observed_at="2026-06-04T14:02:00+00:00",
                mode="paper",
                orders=[open_spread_order_payload()],
            )
            trades = list_spread_trades(db_path, mode="paper", status="open")

        self.assertEqual(changes, [])
        self.assertEqual(len(trades), 1)


if __name__ == "__main__":
    unittest.main()

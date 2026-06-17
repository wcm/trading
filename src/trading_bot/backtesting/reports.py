from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from trading_bot.config import resolve_path


def write_csv(path_value: str | Path, rows: list[dict[str, Any]]) -> None:
    path = resolve_path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown_table(path_value: str | Path, rows: list[dict[str, Any]]) -> None:
    path = resolve_path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("No rows.\n", encoding="utf-8")
        return
    headers = list(rows[0].keys())
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def trades_to_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "trade_id": trade.get("trade_id"),
            "side": trade.get("side"),
            "timestamp": trade.get("timestamp"),
            "level_index": trade.get("level_index"),
            "price": trade.get("price"),
            "shares": trade.get("shares"),
            "notional": trade.get("notional"),
            "realized_pnl": trade.get("realized_pnl"),
            "paired_buy_trade_id": trade.get("paired_buy_trade_id"),
        }
        for trade in trades
    ]

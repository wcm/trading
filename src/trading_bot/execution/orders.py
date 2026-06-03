from __future__ import annotations

from datetime import UTC, datetime


def build_client_order_id(prefix: str, symbol: str, sequence: int) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    clean_symbol = "".join(ch for ch in symbol.lower() if ch.isalnum())
    return f"{prefix}-{clean_symbol}-{timestamp}-{sequence:03d}"


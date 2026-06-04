from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from typing import Any


def build_client_order_id(prefix: str, symbol: str, sequence: int) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    clean_symbol = "".join(ch for ch in symbol.lower() if ch.isalnum())
    return f"{prefix}-{clean_symbol}-{timestamp}-{sequence:03d}"


def build_put_credit_spread_order_preview(
    *,
    config: Any,
    decision: dict[str, Any],
    candidate: dict[str, Any] | None,
    client_order_id: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    if decision.get("action") != "open":
        errors.append("Order preview requires an open decision")
    if not candidate:
        errors.append("Order preview requires a selected candidate")
        candidate = {}

    quantity = decision.get("quantity")
    if not isinstance(quantity, int) or quantity <= 0:
        errors.append("Order preview requires a positive integer decision quantity")
        quantity = 0

    limit_price = _decimal_or_none(decision.get("limit_price"))
    if limit_price is None:
        errors.append("Order preview requires a decimal limit_price")
    elif bool(config.get("alpaca", "credit_limit_price_must_be_negative", default=True)) and limit_price >= 0:
        errors.append("Credit spread limit_price must be negative")

    short_symbol = candidate.get("short_put_symbol")
    long_symbol = candidate.get("long_put_symbol")
    if not short_symbol or not long_symbol:
        errors.append("Candidate must include short_put_symbol and long_put_symbol")

    short_strike = _decimal_or_none(candidate.get("short_strike"))
    long_strike = _decimal_or_none(candidate.get("long_strike"))
    if short_strike is None or long_strike is None:
        errors.append("Candidate must include decimal short_strike and long_strike")
    elif short_strike <= long_strike:
        errors.append("Put credit spread requires short strike above long strike")

    order_type = str(config.get("execution", "order_type", default="limit"))
    if order_type != "limit":
        errors.append("Only limit MLeg order previews are supported")

    if bool(config.get("execution", "no_market_orders", default=True)) and order_type == "market":
        errors.append("Market orders are disabled")

    max_loss = _decimal_or_none(candidate.get("max_loss")) or Decimal("0")
    max_profit = _decimal_or_none(candidate.get("max_profit")) or Decimal("0")
    net_credit = abs(limit_price) if limit_price is not None else Decimal("0")
    estimated_credit = net_credit * Decimal("100") * Decimal(quantity)
    estimated_max_loss = max_loss * Decimal(quantity)
    estimated_max_profit = max_profit * Decimal(quantity)

    payload = {
        "order_class": "mleg",
        "qty": str(quantity),
        "type": "limit",
        "limit_price": _fmt_decimal(limit_price) if limit_price is not None else None,
        "time_in_force": "day",
        "client_order_id": client_order_id,
        "legs": [
            {
                "symbol": short_symbol,
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
            {
                "symbol": long_symbol,
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
        ],
    }

    if not bool(config.get("alpaca", "require_client_order_id", default=True)):
        payload.pop("client_order_id", None)
        warnings.append("client_order_id omitted because config does not require it")

    return {
        "kind": "alpaca_mleg_order_preview",
        "submit_disabled": True,
        "submit_endpoint": "/v2/orders",
        "strategy": "put_credit_spread",
        "candidate_id": candidate.get("candidate_id"),
        "symbol": decision.get("symbol") or candidate.get("underlying_symbol"),
        "payload": payload,
        "estimated_entry_credit": _fmt_decimal(estimated_credit),
        "estimated_max_profit": _fmt_decimal(estimated_max_profit),
        "estimated_max_loss": _fmt_decimal(estimated_max_loss),
        "errors": errors,
        "warnings": warnings,
    }


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fmt_decimal(value: Decimal) -> str:
    return f"{value.normalize():f}"

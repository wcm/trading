from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_bot.grid.state import GridState
from trading_bot.utils.money import decimal_or_none


@dataclass(frozen=True)
class GridBrokerSnapshot:
    symbol: str
    position_qty: Decimal
    open_orders: list[dict[str, Any]]

    @property
    def open_order_ids(self) -> set[str]:
        return {
            str(order["id"])
            for order in self.open_orders
            if order.get("id")
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "position_qty": _fmt_decimal(self.position_qty),
            "open_orders": [
                {
                    "id": order.get("id"),
                    "client_order_id": order.get("client_order_id"),
                    "side": order.get("side"),
                    "status": order.get("status"),
                    "qty": order.get("qty"),
                    "filled_qty": order.get("filled_qty"),
                    "limit_price": order.get("limit_price"),
                    "time_in_force": order.get("time_in_force"),
                }
                for order in self.open_orders
            ],
        }


def load_grid_broker_snapshot(alpaca, *, symbol: str) -> GridBrokerSnapshot:
    normalized_symbol = symbol.upper()
    position_qty = Decimal("0")
    for position in alpaca.get_positions():
        if str(position.get("symbol") or "").upper() != normalized_symbol:
            continue
        position_qty += decimal_or_none(position.get("qty")) or Decimal("0")

    open_orders = [
        order
        for order in alpaca.get_orders(status="open", limit=500)
        if str(order.get("symbol") or "").upper() == normalized_symbol
    ]
    return GridBrokerSnapshot(
        symbol=normalized_symbol,
        position_qty=position_qty,
        open_orders=open_orders,
    )


def grid_reconciliation_errors(
    state: GridState,
    snapshot: GridBrokerSnapshot,
) -> list[str]:
    errors: list[str] = []
    expected_order_ids = _expected_open_order_ids(state)
    actual_order_ids = snapshot.open_order_ids

    missing_at_broker = sorted(expected_order_ids - actual_order_ids)
    if missing_at_broker:
        errors.append(
            "Local state expects open Alpaca orders that are missing: "
            + ", ".join(missing_at_broker)
        )

    unexpected_at_broker = sorted(actual_order_ids - expected_order_ids)
    if unexpected_at_broker:
        errors.append(
            "Alpaca has unexplained open TQQQ orders: "
            + ", ".join(unexpected_at_broker)
        )

    expected_position_qty = _expected_position_qty(state)
    if snapshot.position_qty != expected_position_qty:
        errors.append(
            "TQQQ position mismatch: "
            f"Alpaca={_fmt_decimal(snapshot.position_qty)}, "
            f"local_state={_fmt_decimal(expected_position_qty)}"
        )
    return errors


def _expected_open_order_ids(state: GridState) -> set[str]:
    order_ids: set[str] = set()
    for lot in state.lots:
        if lot.status == "buy_submitted" and lot.buy_order_id:
            order_ids.add(lot.buy_order_id)
        elif lot.status == "sell_submitted" and lot.sell_order_id:
            order_ids.add(lot.sell_order_id)
    return order_ids


def _expected_position_qty(state: GridState) -> Decimal:
    total = Decimal("0")
    for lot in state.lots:
        if lot.status == "buy_submitted":
            total += lot.qty or Decimal("0")
        elif lot.has_open_inventory():
            total += lot.remaining_qty()
    return total


def _fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


ACTIVE_LOT_STATUSES = {"buy_submitted", "open", "sell_submitted"}
OPEN_LOT_STATUSES = {"open", "sell_submitted"}


@dataclass
class GridLotState:
    lot_id: str
    level_index: int
    buy_price: Decimal
    sell_target: Decimal
    planned_notional: Decimal
    status: str = "planned"
    qty: Decimal | None = None
    created_at: str = ""
    buy_order_id: str | None = None
    buy_client_order_id: str | None = None
    buy_submitted_at: str | None = None
    buy_filled_at: str | None = None
    buy_fill_price: Decimal | None = None
    sell_order_id: str | None = None
    sell_client_order_id: str | None = None
    sell_submitted_at: str | None = None
    sell_filled_at: str | None = None
    sell_fill_price: Decimal | None = None
    sell_filled_qty: Decimal | None = None
    realized_pnl: Decimal | None = None
    last_order_status: str | None = None
    notes: list[str] = field(default_factory=list)

    def is_active(self) -> bool:
        return self.status in ACTIVE_LOT_STATUSES

    def has_open_inventory(self) -> bool:
        return self.status in OPEN_LOT_STATUSES

    def cost_basis(self) -> Decimal:
        if self.status == "buy_submitted":
            return self.planned_notional
        if self.qty is not None and self.buy_fill_price is not None:
            return self.remaining_qty() * self.buy_fill_price
        return self.planned_notional

    def remaining_qty(self) -> Decimal:
        qty = self.qty or Decimal("0")
        if self.status == "sell_submitted" and self.sell_filled_qty is not None:
            return max(Decimal("0"), qty - self.sell_filled_qty)
        return qty

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        if self.qty is None or self.buy_fill_price is None or not self.has_open_inventory():
            return Decimal("0")
        return (mark_price - self.buy_fill_price) * self.remaining_qty()

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "level_index": self.level_index,
            "buy_price": _fmt_decimal(self.buy_price),
            "sell_target": _fmt_decimal(self.sell_target),
            "planned_notional": _fmt_decimal(self.planned_notional),
            "status": self.status,
            "qty": _fmt_optional_decimal(self.qty),
            "created_at": self.created_at,
            "buy_order_id": self.buy_order_id,
            "buy_client_order_id": self.buy_client_order_id,
            "buy_submitted_at": self.buy_submitted_at,
            "buy_filled_at": self.buy_filled_at,
            "buy_fill_price": _fmt_optional_decimal(self.buy_fill_price),
            "sell_order_id": self.sell_order_id,
            "sell_client_order_id": self.sell_client_order_id,
            "sell_submitted_at": self.sell_submitted_at,
            "sell_filled_at": self.sell_filled_at,
            "sell_fill_price": _fmt_optional_decimal(self.sell_fill_price),
            "sell_filled_qty": _fmt_optional_decimal(self.sell_filled_qty),
            "realized_pnl": _fmt_optional_decimal(self.realized_pnl),
            "last_order_status": self.last_order_status,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GridLotState":
        return cls(
            lot_id=str(data["lot_id"]),
            level_index=int(data["level_index"]),
            buy_price=Decimal(str(data["buy_price"])),
            sell_target=Decimal(str(data["sell_target"])),
            planned_notional=Decimal(str(data.get("planned_notional", "0"))),
            status=str(data.get("status", "planned")),
            qty=_optional_decimal(data.get("qty")),
            created_at=str(data.get("created_at") or ""),
            buy_order_id=_optional_str(data.get("buy_order_id")),
            buy_client_order_id=_optional_str(data.get("buy_client_order_id")),
            buy_submitted_at=_optional_str(data.get("buy_submitted_at")),
            buy_filled_at=_optional_str(data.get("buy_filled_at")),
            buy_fill_price=_optional_decimal(data.get("buy_fill_price")),
            sell_order_id=_optional_str(data.get("sell_order_id")),
            sell_client_order_id=_optional_str(data.get("sell_client_order_id")),
            sell_submitted_at=_optional_str(data.get("sell_submitted_at")),
            sell_filled_at=_optional_str(data.get("sell_filled_at")),
            sell_fill_price=_optional_decimal(data.get("sell_fill_price")),
            sell_filled_qty=_optional_decimal(data.get("sell_filled_qty")),
            realized_pnl=_optional_decimal(data.get("realized_pnl")),
            last_order_status=_optional_str(data.get("last_order_status")),
            notes=[str(item) for item in data.get("notes", [])],
        )


@dataclass
class GridState:
    strategy_name: str
    symbol: str
    anchor_price: Decimal | None = None
    recenter_count: int = 0
    lots: list[GridLotState] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def active_lots(self) -> list[GridLotState]:
        return [lot for lot in self.lots if lot.is_active()]

    def active_level_indexes(self) -> set[int]:
        return {lot.level_index for lot in self.active_lots()}

    def open_inventory_lots(self) -> list[GridLotState]:
        return [lot for lot in self.lots if lot.has_open_inventory()]

    def summary(self, mark_price: Decimal | None = None) -> dict[str, Any]:
        active = self.active_lots()
        open_inventory = self.open_inventory_lots()
        planned_or_cost = sum((lot.cost_basis() for lot in active), Decimal("0"))
        unrealized = (
            sum((lot.unrealized_pnl(mark_price) for lot in open_inventory), Decimal("0"))
            if mark_price is not None
            else None
        )
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "anchor_price": _fmt_optional_decimal(self.anchor_price),
            "recenter_count": self.recenter_count,
            "lot_count": len(self.lots),
            "active_lot_count": len(active),
            "open_inventory_lot_count": len(open_inventory),
            "active_level_indexes": sorted(self.active_level_indexes()),
            "active_planned_or_cost": _fmt_decimal(planned_or_cost),
            "unrealized_pnl": _fmt_optional_decimal(unrealized),
            "updated_at": self.updated_at,
        }

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "anchor_price": _fmt_optional_decimal(self.anchor_price),
            "recenter_count": self.recenter_count,
            "lots": [lot.to_dict() for lot in self.lots],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GridState":
        return cls(
            strategy_name=str(data["strategy_name"]),
            symbol=str(data["symbol"]).upper(),
            anchor_price=_optional_decimal(data.get("anchor_price")),
            recenter_count=int(data.get("recenter_count", 0)),
            lots=[GridLotState.from_dict(item) for item in data.get("lots", [])],
            created_at=str(data.get("created_at") or datetime.now(UTC).isoformat()),
            updated_at=str(data.get("updated_at") or datetime.now(UTC).isoformat()),
        )


def load_grid_state(path: Path, *, strategy_name: str, symbol: str) -> GridState:
    if not path.exists():
        return GridState(strategy_name=strategy_name, symbol=symbol.upper())
    data = json.loads(path.read_text(encoding="utf-8"))
    state = GridState.from_dict(data)
    if state.strategy_name != strategy_name or state.symbol != symbol.upper():
        raise ValueError(
            f"Grid state at {path} belongs to {state.strategy_name}/{state.symbol}, "
            f"not {strategy_name}/{symbol.upper()}"
        )
    return state


def save_grid_state(path: Path, state: GridState) -> None:
    state.touch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _fmt_decimal(value)


def _fmt_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")

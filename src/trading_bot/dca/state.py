from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


COUNTED_PURCHASE_STATUSES = {
    "submitted",
    "partially_filled",
    "partially_filled_closed",
    "filled",
}


@dataclass
class DcaPurchaseState:
    purchase_id: str
    period_key: str
    scheduled_date: str
    contribution_amount: Decimal
    status: str
    client_order_id: str
    broker_order_id: str
    submitted_at: str
    filled_at: str | None = None
    filled_qty: Decimal | None = None
    filled_avg_price: Decimal | None = None
    last_order_status: str | None = None

    def counts_toward_period(self) -> bool:
        return self.status in COUNTED_PURCHASE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "purchase_id": self.purchase_id,
            "period_key": self.period_key,
            "scheduled_date": self.scheduled_date,
            "contribution_amount": _fmt_decimal(self.contribution_amount),
            "status": self.status,
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "submitted_at": self.submitted_at,
            "filled_at": self.filled_at,
            "filled_qty": _fmt_optional_decimal(self.filled_qty),
            "filled_avg_price": _fmt_optional_decimal(self.filled_avg_price),
            "last_order_status": self.last_order_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DcaPurchaseState":
        return cls(
            purchase_id=str(data["purchase_id"]),
            period_key=str(data["period_key"]),
            scheduled_date=str(data["scheduled_date"]),
            contribution_amount=Decimal(str(data["contribution_amount"])),
            status=str(data["status"]),
            client_order_id=str(data["client_order_id"]),
            broker_order_id=str(data["broker_order_id"]),
            submitted_at=str(data["submitted_at"]),
            filled_at=_optional_str(data.get("filled_at")),
            filled_qty=_optional_decimal(data.get("filled_qty")),
            filled_avg_price=_optional_decimal(data.get("filled_avg_price")),
            last_order_status=_optional_str(data.get("last_order_status")),
        )


@dataclass
class DcaState:
    strategy_name: str
    symbol: str
    purchases: list[DcaPurchaseState] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def completed_period_keys(self) -> set[str]:
        return {
            purchase.period_key
            for purchase in self.purchases
            if purchase.counts_toward_period()
        }

    def annual_contributed(self, year: int) -> Decimal:
        return sum(
            (
                purchase.contribution_amount
                for purchase in self.purchases
                if purchase.counts_toward_period()
                and purchase.scheduled_date.startswith(f"{year:04d}-")
            ),
            Decimal("0"),
        )

    def summary(self) -> dict[str, Any]:
        filled = [
            purchase
            for purchase in self.purchases
            if purchase.status in {"filled", "partially_filled_closed"}
        ]
        filled_qty = sum(
            (purchase.filled_qty or Decimal("0") for purchase in filled),
            Decimal("0"),
        )
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "purchase_count": len(self.purchases),
            "filled_purchase_count": len(filled),
            "filled_qty": _fmt_decimal(filled_qty),
            "completed_period_keys": sorted(self.completed_period_keys()),
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "purchases": [purchase.to_dict() for purchase in self.purchases],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DcaState":
        return cls(
            strategy_name=str(data["strategy_name"]),
            symbol=str(data["symbol"]).upper(),
            purchases=[
                DcaPurchaseState.from_dict(item)
                for item in data.get("purchases", [])
            ],
            created_at=str(data.get("created_at") or datetime.now(UTC).isoformat()),
            updated_at=str(data.get("updated_at") or datetime.now(UTC).isoformat()),
        )


def load_dca_state(path: Path, *, strategy_name: str, symbol: str) -> DcaState:
    if not path.exists():
        return DcaState(strategy_name=strategy_name, symbol=symbol.upper())
    data = json.loads(path.read_text(encoding="utf-8"))
    state = DcaState.from_dict(data)
    if state.strategy_name != strategy_name or state.symbol != symbol.upper():
        raise ValueError(
            f"DCA state at {path} belongs to {state.strategy_name}/{state.symbol}, "
            f"not {strategy_name}/{symbol.upper()}"
        )
    return state


def save_dca_state(path: Path, state: DcaState) -> None:
    state.updated_at = datetime.now(UTC).isoformat()
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


def _fmt_decimal(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    return _fmt_decimal(value) if value is not None else None

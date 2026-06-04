from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def sum_decimal_strings(values: list[Any]) -> Decimal | None:
    total = Decimal("0")
    seen = False
    for value in values:
        parsed = decimal_or_none(value)
        if parsed is None:
            continue
        total += parsed
        seen = True
    return total if seen else None


def format_optional_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01")))


def format_counts(counts: dict[str, Any]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))

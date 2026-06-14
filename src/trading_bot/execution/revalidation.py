from __future__ import annotations

import copy
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any

from trading_bot.brokers.alpaca import AlpacaClient
from trading_bot.config import AppConfig
from trading_bot.utils.mapping import nested_get as _nested_get
from trading_bot.utils.market_time import age_seconds as _age_seconds
from trading_bot.utils.market_time import parse_timestamp as _parse_time
from trading_bot.utils.money import decimal_or_none as _decimal_or_none
from trading_bot.utils.money import format_decimal as _fmt_decimal
from trading_bot.utils.money import format_optional_decimal_compact as _fmt_optional_decimal
from trading_bot.utils.money import int_or_none as _int_or_none


def revalidate_put_credit_spread_entry_preview(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    order_preview: dict[str, Any],
    adjustment_index: int = 0,
) -> dict[str, Any]:
    refreshed = copy.deepcopy(order_preview)
    report = _build_empty_report(adjustment_index=adjustment_index)
    refreshed["revalidation"] = report

    payload = refreshed.get("payload")
    if not isinstance(payload, dict):
        _append_revalidation_error(refreshed, report, "Order preview payload is unavailable")
        return refreshed

    candidate = refreshed.get("candidate")
    if not isinstance(candidate, dict):
        candidate = {}

    short_symbol = _payload_leg_symbol(payload, "sell_to_open")
    long_symbol = _payload_leg_symbol(payload, "buy_to_open")
    if not short_symbol or not long_symbol:
        _append_revalidation_error(
            refreshed,
            report,
            "Entry payload must include sell_to_open and buy_to_open legs",
        )
        return refreshed

    report["short_symbol"] = short_symbol
    report["long_symbol"] = long_symbol
    report["feed"] = str(config.get("alpaca", "option_data_feed", default="indicative"))

    snapshots = alpaca.get_option_snapshots([short_symbol, long_symbol], feed=report["feed"])
    short_quote = _latest_quote(snapshots.get(short_symbol))
    long_quote = _latest_quote(snapshots.get(long_symbol))
    short_bid = _decimal_or_none(_nested_get(short_quote, "bp", "bid_price", "bidPrice"))
    short_ask = _decimal_or_none(_nested_get(short_quote, "ap", "ask_price", "askPrice"))
    long_bid = _decimal_or_none(_nested_get(long_quote, "bp", "bid_price", "bidPrice"))
    long_ask = _decimal_or_none(_nested_get(long_quote, "ap", "ask_price", "askPrice"))

    now = datetime.now(UTC)
    short_quote_time = _parse_time(_nested_get(short_quote, "t", "timestamp"))
    long_quote_time = _parse_time(_nested_get(long_quote, "t", "timestamp"))
    short_age = _age_seconds(now, short_quote_time)
    long_age = _age_seconds(now, long_quote_time)
    max_quote_age_seconds = int(
        config.get("market_filters", "max_option_quote_age_minutes", default=30)
    ) * 60

    report.update(
        {
            "checked_at": now.isoformat(),
            "short_bid": _fmt_optional_decimal(short_bid),
            "short_ask": _fmt_optional_decimal(short_ask),
            "long_bid": _fmt_optional_decimal(long_bid),
            "long_ask": _fmt_optional_decimal(long_ask),
            "short_quote_time": short_quote_time.isoformat() if short_quote_time else None,
            "long_quote_time": long_quote_time.isoformat() if long_quote_time else None,
            "short_quote_age_seconds": short_age,
            "long_quote_age_seconds": long_age,
            "max_quote_age_seconds": max_quote_age_seconds,
        }
    )

    for label, bid, ask in (
        ("short", short_bid, short_ask),
        ("long", long_bid, long_ask),
    ):
        if bid is None or ask is None:
            _append_revalidation_error(refreshed, report, f"{label} leg quote is unavailable")
        elif bid <= 0 or ask <= 0 or ask < bid:
            _append_revalidation_error(refreshed, report, f"{label} leg quote is invalid")

    for label, age in (("short", short_age), ("long", long_age)):
        if age is None:
            _append_revalidation_error(refreshed, report, f"{label} leg quote timestamp is unavailable")
        elif age < -60:
            _append_revalidation_error(refreshed, report, f"{label} leg quote timestamp is from the future")
        elif age > max_quote_age_seconds:
            _append_revalidation_error(refreshed, report, f"{label} leg quote is stale: {age}s old")

    width = _spread_width(candidate)
    quantity = _int_or_none(payload.get("qty")) or 0
    requested_credit = abs(_decimal_or_none(payload.get("limit_price")) or Decimal("0"))
    min_credit_pct = _decimal_or_none(
        config.get("strategy", "min_credit_as_width_pct", default=0.20)
    ) or Decimal("0.20")
    credit_buffer = _decimal_or_none(
        config.get("execution", "entry_limit_credit_buffer", default=0)
    ) or Decimal("0")
    adjustment_step = _decimal_or_none(
        config.get("execution", "entry_price_adjustment_step", default=0)
    ) or Decimal("0")

    if width is None or width <= 0:
        _append_revalidation_error(refreshed, report, "Spread width is unavailable")
        width = Decimal("0")
    if quantity <= 0:
        _append_revalidation_error(refreshed, report, "Order quantity is unavailable")

    min_credit = width * min_credit_pct
    min_credit_floor = _round_credit_up(min_credit)
    current_credit = None
    if short_bid is not None and long_ask is not None:
        current_credit = short_bid - long_ask

    total_buffer = credit_buffer + (adjustment_step * Decimal(max(0, adjustment_index)))
    selected_credit = None
    if current_credit is None:
        _append_revalidation_error(refreshed, report, "Current spread credit could not be calculated")
    elif current_credit <= 0:
        _append_revalidation_error(refreshed, report, f"Current spread credit is not positive: {current_credit}")
    elif current_credit < min_credit_floor:
        _append_revalidation_error(
            refreshed,
            report,
            f"Current spread credit {current_credit} is below minimum acceptable credit {min_credit_floor}",
        )
    else:
        relaxed_credit = current_credit - total_buffer
        selected_credit = max(min_credit_floor, min(requested_credit, relaxed_credit))
        selected_credit = _round_credit_down(selected_credit)
        if selected_credit <= 0:
            _append_revalidation_error(refreshed, report, "Selected entry credit is not positive")
        elif selected_credit > current_credit:
            _append_revalidation_error(
                refreshed,
                report,
                f"Selected entry credit {selected_credit} exceeds current spread credit {current_credit}",
            )

    report.update(
        {
            "requested_credit": _fmt_decimal(requested_credit),
            "current_net_credit": _fmt_optional_decimal(current_credit),
            "min_acceptable_credit": _fmt_decimal(min_credit_floor),
            "credit_buffer": _fmt_decimal(credit_buffer),
            "adjustment_step": _fmt_decimal(adjustment_step),
            "total_credit_buffer": _fmt_decimal(total_buffer),
            "selected_credit": _fmt_optional_decimal(selected_credit),
            "selected_limit_price": _fmt_optional_decimal(-selected_credit) if selected_credit else None,
        }
    )

    if selected_credit is not None and not report["errors"]:
        payload["limit_price"] = _fmt_decimal(-selected_credit)
        refreshed["estimated_entry_credit"] = _fmt_decimal(selected_credit * Decimal("100") * Decimal(quantity))
        refreshed["estimated_max_profit"] = refreshed["estimated_entry_credit"]
        if width > 0:
            refreshed["estimated_max_loss"] = _fmt_decimal(
                (width - selected_credit) * Decimal("100") * Decimal(quantity)
            )
        report["ok"] = True

    return refreshed


def _build_empty_report(*, adjustment_index: int) -> dict[str, Any]:
    return {
        "kind": "put_credit_spread_entry_revalidation",
        "checked_at": datetime.now(UTC).isoformat(),
        "ok": False,
        "adjustment_index": adjustment_index,
        "errors": [],
        "warnings": [],
    }


def _append_revalidation_error(
    order_preview: dict[str, Any],
    report: dict[str, Any],
    error: str,
) -> None:
    report.setdefault("errors", []).append(error)
    order_preview.setdefault("errors", []).append(f"Revalidation: {error}")


def _payload_leg_symbol(payload: dict[str, Any], position_intent: str) -> str | None:
    legs = payload.get("legs")
    if not isinstance(legs, list):
        return None
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        if leg.get("position_intent") == position_intent and leg.get("symbol"):
            return str(leg["symbol"])
    return None


def _latest_quote(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    return quote if isinstance(quote, dict) else {}


def _spread_width(candidate: dict[str, Any]) -> Decimal | None:
    width = _decimal_or_none(candidate.get("width"))
    if width is not None:
        return width
    short_strike = _decimal_or_none(candidate.get("short_strike"))
    long_strike = _decimal_or_none(candidate.get("long_strike"))
    if short_strike is None or long_strike is None:
        return None
    return short_strike - long_strike


def _round_credit_down(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _round_credit_up(value: Decimal) -> Decimal:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if rounded == value:
        return rounded
    return rounded + Decimal("0.01")

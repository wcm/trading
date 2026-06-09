from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


ALLOWED_ACTIONS = {"open", "close", "hold", "skip", "disable_trading"}
REQUIRED_DECISION_FIELDS = {
    "action",
    "symbol",
    "candidate_id",
    "quantity",
    "limit_price",
    "confidence",
    "decision_reason",
    "news_assessment",
    "risk_checklist",
    "exit_plan",
}


DECISION_RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(REQUIRED_DECISION_FIELDS),
    "properties": {
        "action": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
        "symbol": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "candidate_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "quantity": {"type": "integer", "minimum": 0, "maximum": 1},
        "limit_price": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "decision_reason": {"type": "string"},
        "news_assessment": {
            "type": "object",
            "additionalProperties": False,
            "required": ["risk_level", "sentiment", "summary"],
            "properties": {
                "risk_level": {"type": "string", "enum": ["low", "medium", "high", "unknown"]},
                "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative", "mixed", "unknown"]},
                "summary": {"type": "string"},
            },
        },
        "risk_checklist": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "defined_risk",
                "within_max_loss",
                "liquidity_ok",
                "earnings_ok",
                "no_material_negative_news",
                "market_trend_ok",
                "broad_market_ok",
                "short_put_distance_ok",
            ],
            "properties": {
                "defined_risk": {"type": "boolean"},
                "within_max_loss": {"type": "boolean"},
                "liquidity_ok": {"type": "boolean"},
                "earnings_ok": {"type": "boolean"},
                "no_material_negative_news": {"type": "boolean"},
                "market_trend_ok": {"type": "boolean"},
                "broad_market_ok": {"type": "boolean"},
                "short_put_distance_ok": {"type": "boolean"},
            },
        },
        "exit_plan": {
            "type": "object",
            "additionalProperties": False,
            "required": ["profit_take_credit_pct", "loss_trigger", "close_before_expiry_days"],
            "properties": {
                "profit_take_credit_pct": {"type": "integer", "minimum": 0, "maximum": 100},
                "loss_trigger": {"type": "string"},
                "close_before_expiry_days": {"type": "integer", "minimum": 0, "maximum": 30},
            },
        },
    },
}


def validate_decision_payload(
    payload: dict[str, Any],
    *,
    candidate_ids: set[str] | None = None,
    candidates_by_id: dict[str, dict[str, Any]] | None = None,
    allowed_symbols: set[str] | None = None,
    open_position_symbols: set[str] | None = None,
    market_context_by_symbol: dict[str, dict[str, Any]] | None = None,
    event_context_by_symbol: dict[str, dict[str, Any]] | None = None,
    market_context: dict[str, Any] | None = None,
    broad_market_symbol: str | None = None,
    require_broad_market_above_ma: bool = True,
    max_loss_per_trade: str | int | float | None = None,
    max_option_quote_age_seconds: int | None = None,
    min_short_put_distance_pct: str | int | float | None = None,
    min_open_confidence: str | int | float | None = None,
) -> list[str]:
    errors: list[str] = []

    missing = sorted(REQUIRED_DECISION_FIELDS - payload.keys())
    if missing:
        errors.append(f"Missing required fields: {', '.join(missing)}")

    action = payload.get("action")
    if action not in ALLOWED_ACTIONS:
        errors.append(f"Unsupported action: {action!r}")

    symbol = payload.get("symbol")
    if symbol is not None:
        if not isinstance(symbol, str) or not symbol.strip():
            errors.append("symbol must be a non-empty string or null")
        elif allowed_symbols is not None and symbol not in allowed_symbols:
            errors.append(f"Symbol is not allowed: {symbol}")

    quantity = payload.get("quantity")
    if not isinstance(quantity, int) or quantity < 0:
        errors.append("quantity must be a non-negative integer")
    elif quantity > 1:
        errors.append("quantity cannot exceed 1 in v1")

    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
        errors.append("confidence must be a number between 0 and 1")
    elif action == "open":
        _validate_open_confidence(confidence, min_open_confidence, errors)

    candidate_id = payload.get("candidate_id")
    if action == "open":
        if not candidate_id:
            errors.append("Open decision requires candidate_id")
        elif candidate_ids is not None and candidate_id not in candidate_ids:
            errors.append(f"Candidate was not generated by code: {candidate_id}")
        if quantity != 1:
            errors.append("Open decision must use quantity 1 in v1")
        _validate_open_limit_price(payload, candidates_by_id, errors)
        _validate_open_candidate_context(
            payload,
            candidates_by_id,
            market_context_by_symbol,
            event_context_by_symbol,
            max_loss_per_trade,
            max_option_quote_age_seconds,
            min_short_put_distance_pct,
            errors,
        )
        _validate_open_broad_market_context(
            market_context,
            broad_market_symbol,
            require_broad_market_above_ma,
            errors,
        )
    elif quantity not in (0, None):
        errors.append(f"{action} decision must use quantity 0")

    if action == "close" and symbol and open_position_symbols is not None and symbol not in open_position_symbols:
        errors.append(f"Cannot close {symbol}; no matching open position is known")

    _validate_nested_objects(payload, errors)

    return errors


def _validate_open_limit_price(
    payload: dict[str, Any],
    candidates_by_id: dict[str, dict[str, Any]] | None,
    errors: list[str],
) -> None:
    limit_price = payload.get("limit_price")
    if not isinstance(limit_price, str) or not limit_price:
        errors.append("Open decision requires negative credit limit_price string")
        return

    try:
        limit = Decimal(limit_price)
    except (InvalidOperation, ValueError):
        errors.append("limit_price must be a decimal string")
        return

    if limit >= 0:
        errors.append("Open decision limit_price must be negative for credit spreads")
        return

    candidate_id = payload.get("candidate_id")
    if candidates_by_id is None or candidate_id not in candidates_by_id:
        return

    candidate = candidates_by_id[candidate_id]
    try:
        max_credit = Decimal(str(candidate["net_credit"]))
    except (InvalidOperation, KeyError, ValueError):
        errors.append("Candidate net_credit is unavailable for limit price validation")
        return

    if abs(limit) > max_credit:
        errors.append(
            f"Open limit_price credit {abs(limit)} exceeds candidate net_credit {max_credit}"
        )


def _validate_open_candidate_context(
    payload: dict[str, Any],
    candidates_by_id: dict[str, dict[str, Any]] | None,
    market_context_by_symbol: dict[str, dict[str, Any]] | None,
    event_context_by_symbol: dict[str, dict[str, Any]] | None,
    max_loss_per_trade: str | int | float | None,
    max_option_quote_age_seconds: int | None,
    min_short_put_distance_pct: str | int | float | None,
    errors: list[str],
) -> None:
    candidate_id = payload.get("candidate_id")
    if candidates_by_id is None or candidate_id not in candidates_by_id:
        return

    candidate = candidates_by_id[candidate_id]
    candidate_symbol = candidate.get("underlying_symbol")
    decision_symbol = payload.get("symbol")
    if decision_symbol and candidate_symbol and decision_symbol != candidate_symbol:
        errors.append(f"Decision symbol {decision_symbol} does not match candidate symbol {candidate_symbol}")

    if candidate.get("liquidity_ok") is not True:
        errors.append("Candidate liquidity_ok is not true")

    min_distance = _decimal_or_none(min_short_put_distance_pct)
    if min_distance is not None and min_distance > 0:
        candidate_distance = _decimal_or_none(candidate.get("short_put_distance_pct"))
        if candidate_distance is None:
            errors.append("Candidate short put distance is unavailable")
        elif candidate_distance < min_distance:
            errors.append(
                f"Candidate short put distance {candidate_distance} is below minimum {min_distance}%"
            )

    if max_loss_per_trade is not None:
        candidate_max_loss = _decimal_or_none(candidate.get("max_loss"))
        max_loss = _decimal_or_none(max_loss_per_trade)
        if candidate_max_loss is None or max_loss is None:
            errors.append("Could not validate candidate max_loss against risk limit")
        elif candidate_max_loss > max_loss:
            errors.append(f"Candidate max_loss {candidate_max_loss} exceeds risk limit {max_loss}")

    max_quote_age = candidate.get("max_quote_age_seconds")
    if max_option_quote_age_seconds is not None:
        if not isinstance(max_quote_age, int):
            errors.append("Candidate option quote age is unavailable")
        elif max_quote_age < -60:
            errors.append(f"Candidate option quote timestamp is from the future: age={max_quote_age}")
        elif max_quote_age > max_option_quote_age_seconds:
            errors.append(
                f"Candidate option quote age {max_quote_age}s exceeds limit {max_option_quote_age_seconds}s"
            )

    if market_context_by_symbol is None or not candidate_symbol:
        errors.append("Market context is unavailable for open decision")
        return

    market_context = market_context_by_symbol.get(str(candidate_symbol))
    if not isinstance(market_context, dict):
        errors.append(f"Market context is unavailable for {candidate_symbol}")
        return
    if market_context.get("latest_bar_fresh") is not True:
        errors.append(f"Latest market bar is not fresh for {candidate_symbol}")
    if market_context.get("market_trend_ok") is not True:
        errors.append(f"Market trend filter is not passing for {candidate_symbol}")

    if event_context_by_symbol is None or not candidate_symbol:
        errors.append("Event context is unavailable for open decision")
        return
    event_context = event_context_by_symbol.get(str(candidate_symbol))
    if not isinstance(event_context, dict):
        errors.append(f"Event context is unavailable for {candidate_symbol}")
    elif event_context.get("earnings_ok") is not True:
        errors.append(f"Earnings/event filter is not passing for {candidate_symbol}")


def _validate_open_confidence(
    confidence: int | float,
    min_open_confidence: str | int | float | None,
    errors: list[str],
) -> None:
    minimum = _decimal_or_none(min_open_confidence)
    if minimum is None:
        return
    confidence_decimal = _decimal_or_none(confidence)
    if confidence_decimal is None:
        return
    if confidence_decimal < minimum:
        errors.append(f"Open confidence {confidence_decimal} is below minimum {minimum}")


def _validate_open_broad_market_context(
    market_context: dict[str, Any] | None,
    broad_market_symbol: str | None,
    require_broad_market_above_ma: bool,
    errors: list[str],
) -> None:
    symbol = str(broad_market_symbol or "").strip().upper()
    if not symbol:
        return
    if not isinstance(market_context, dict):
        errors.append("Broad market context is unavailable")
        return

    symbols = market_context.get("symbols")
    if not isinstance(symbols, dict):
        errors.append("Broad market symbols context is unavailable")
        return
    broad_context = symbols.get(symbol)
    if not isinstance(broad_context, dict):
        errors.append(f"Broad market context is unavailable for {symbol}")
        return

    if broad_context.get("latest_bar_fresh") is not True:
        errors.append(f"Broad market latest bar is not fresh for {symbol}")
    if broad_context.get("block_intraday_down") is not False:
        errors.append(f"Broad market intraday down filter is not passing for {symbol}")
    if require_broad_market_above_ma and broad_context.get("above_trend_ma") is not True:
        errors.append(f"Broad market moving-average filter is not passing for {symbol}")


def _validate_nested_objects(payload: dict[str, Any], errors: list[str]) -> None:
    news = payload.get("news_assessment")
    if not isinstance(news, dict):
        errors.append("news_assessment must be an object")
    elif payload.get("action") == "open":
        if news.get("risk_level") == "high":
            errors.append("Open decision rejected because news risk_level is high")
        if news.get("sentiment") == "negative":
            errors.append("Open decision rejected because news sentiment is negative")

    risk = payload.get("risk_checklist")
    if not isinstance(risk, dict):
        errors.append("risk_checklist must be an object")
    else:
        for key in DECISION_RESPONSE_JSON_SCHEMA["properties"]["risk_checklist"]["required"]:
            if not isinstance(risk.get(key), bool):
                errors.append(f"risk_checklist.{key} must be boolean")
        if payload.get("action") == "open":
            for key in DECISION_RESPONSE_JSON_SCHEMA["properties"]["risk_checklist"]["required"]:
                if risk.get(key) is not True:
                    errors.append(f"Open decision requires risk_checklist.{key} to be true")

    exit_plan = payload.get("exit_plan")
    if not isinstance(exit_plan, dict):
        errors.append("exit_plan must be an object")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_bot.utils.money import decimal_or_none as _decimal_or_none
from trading_bot.utils.money import format_decimal as _fmt_decimal


def build_allocation_summary(config: Any, decision_artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    max_open_risk = _decimal_or_none(config.get("risk", "max_open_risk", default=0)) or Decimal("0")
    ranked: list[dict[str, Any]] = []

    for artifact in decision_artifacts:
        decision = artifact.get("decision")
        packet = artifact.get("packet")
        if not isinstance(decision, dict) or not isinstance(packet, dict):
            continue
        if not artifact.get("accepted") or decision.get("action") != "open":
            continue

        order_preview = artifact.get("order_preview")
        execution_block_reasons = _order_preview_block_reasons(order_preview)
        execution_eligible = not execution_block_reasons
        candidate_id = decision.get("candidate_id")
        candidate = _candidate_by_id(packet, candidate_id)
        if not candidate:
            continue

        preview_max_loss = (
            _decimal_or_none(order_preview.get("estimated_max_loss"))
            if isinstance(order_preview, dict)
            else None
        )
        preview_max_profit = (
            _decimal_or_none(order_preview.get("estimated_max_profit"))
            if isinstance(order_preview, dict)
            else None
        )
        max_loss = preview_max_loss or _decimal_or_none(candidate.get("max_loss"))
        max_profit = preview_max_profit or _decimal_or_none(candidate.get("max_profit"))
        confidence = _decimal_or_none(decision.get("confidence"))
        if max_loss is None or max_profit is None or confidence is None or max_loss <= 0:
            continue

        reward_risk = max_profit / max_loss
        max_contracts = int(max_open_risk // max_loss) if max_open_risk > 0 else 0
        limit_price = _preview_limit_price(order_preview) or decision.get("limit_price")
        net_credit = abs(_decimal_or_none(limit_price) or Decimal("0"))
        ranked.append(
            {
                "symbol": decision.get("symbol") or candidate.get("underlying_symbol"),
                "decision_id": artifact.get("decision_id"),
                "candidate_id": candidate_id,
                "confidence": _fmt_decimal(confidence),
                "net_credit": (
                    _fmt_decimal(net_credit) if net_credit > 0 else str(candidate.get("net_credit"))
                ),
                "max_profit": _fmt_decimal(max_profit),
                "max_loss": _fmt_decimal(max_loss),
                "reward_risk": _fmt_decimal(reward_risk),
                "max_contracts_under_open_risk": max_contracts,
                "v1_order_quantity": decision.get("quantity"),
                "limit_price": limit_price,
                "execution_eligible": execution_eligible,
                "execution_block_reasons": execution_block_reasons,
                "decision_reason": decision.get("decision_reason"),
            }
        )

    ranked.sort(key=_allocation_sort_key)
    selected = next(
        (
            row
            for row in ranked
            if row["execution_eligible"] is True and row["max_contracts_under_open_risk"] >= 1
        ),
        None,
    )

    return {
        "selection_policy": (
            "fresh_execution_eligible_only_then_confidence_desc_then_reward_risk_desc_then_max_profit_desc"
        ),
        "max_open_risk": _fmt_decimal(max_open_risk),
        "accepted_open_count": len(ranked),
        "execution_eligible_open_count": len(
            [row for row in ranked if row["execution_eligible"] is True]
        ),
        "selected_open": selected,
        "ranked_opens": ranked,
    }


def _allocation_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal, str]:
    confidence = _decimal_or_none(row.get("confidence")) or Decimal("0")
    reward_risk = _decimal_or_none(row.get("reward_risk")) or Decimal("0")
    max_profit = _decimal_or_none(row.get("max_profit")) or Decimal("0")
    max_loss = _decimal_or_none(row.get("max_loss")) or Decimal("0")
    symbol = str(row.get("symbol") or "")
    return (-confidence, -reward_risk, -max_profit, max_loss, symbol)


def _candidate_by_id(packet: dict[str, Any], candidate_id: Any) -> dict[str, Any] | None:
    candidates = packet.get("option_scan", {}).get("candidates", [])
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("candidate_id") == candidate_id:
            return candidate
    return None


def _order_preview_block_reasons(order_preview: Any) -> list[str]:
    if not isinstance(order_preview, dict):
        return ["No order preview is available"]
    errors = order_preview.get("errors") or []
    return [str(error) for error in errors]


def _preview_limit_price(order_preview: Any) -> Any:
    if not isinstance(order_preview, dict):
        return None
    payload = order_preview.get("payload")
    if not isinstance(payload, dict):
        return None
    return payload.get("limit_price")

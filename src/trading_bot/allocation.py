from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


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

        candidate_id = decision.get("candidate_id")
        candidate = _candidate_by_id(packet, candidate_id)
        if not candidate:
            continue

        max_loss = _decimal_or_none(candidate.get("max_loss"))
        max_profit = _decimal_or_none(candidate.get("max_profit"))
        confidence = _decimal_or_none(decision.get("confidence"))
        if max_loss is None or max_profit is None or confidence is None or max_loss <= 0:
            continue

        reward_risk = max_profit / max_loss
        max_contracts = int(max_open_risk // max_loss) if max_open_risk > 0 else 0
        ranked.append(
            {
                "symbol": decision.get("symbol") or candidate.get("underlying_symbol"),
                "decision_id": artifact.get("decision_id"),
                "candidate_id": candidate_id,
                "confidence": _fmt_decimal(confidence),
                "net_credit": str(candidate.get("net_credit")),
                "max_profit": _fmt_decimal(max_profit),
                "max_loss": _fmt_decimal(max_loss),
                "reward_risk": _fmt_decimal(reward_risk),
                "max_contracts_under_open_risk": max_contracts,
                "v1_order_quantity": decision.get("quantity"),
                "limit_price": decision.get("limit_price"),
                "decision_reason": decision.get("decision_reason"),
            }
        )

    ranked.sort(key=_allocation_sort_key)
    selected = next((row for row in ranked if row["max_contracts_under_open_risk"] >= 1), None)

    return {
        "selection_policy": "confidence_desc_then_reward_risk_desc_then_max_profit_desc",
        "max_open_risk": _fmt_decimal(max_open_risk),
        "accepted_open_count": len(ranked),
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


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fmt_decimal(value: Decimal) -> str:
    return f"{value.normalize():f}"

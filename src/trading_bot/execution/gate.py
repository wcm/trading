from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_bot.brokers.alpaca import AlpacaClient
from trading_bot.config import AppConfig
from trading_bot.notifications.discord import DiscordNotifier
from trading_bot.risk.kill_switch import KillSwitch


@dataclass(frozen=True)
class PaperExecutionAttempt:
    requested: bool
    submitted: bool
    status: str
    block_reasons: list[str]
    order_preview: dict[str, Any] | None
    order_payload: dict[str, Any] | None
    broker_response: dict[str, Any] | None
    broker_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def maybe_submit_paper_order(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    kill_switch: KillSwitch,
    notifier: DiscordNotifier,
    submit_requested: bool,
    order_preview: dict[str, Any] | None,
    open_orders: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    allocation: dict[str, Any],
    state_refresh_error: str | None = None,
) -> PaperExecutionAttempt:
    block_reasons = _paper_execution_block_reasons(
        config=config,
        kill_switch=kill_switch,
        notifier=notifier,
        submit_requested=submit_requested,
        order_preview=order_preview,
        open_orders=open_orders,
        open_positions=open_positions,
        allocation=allocation,
        state_refresh_error=state_refresh_error,
    )
    payload = order_preview.get("payload") if isinstance(order_preview, dict) else None
    if block_reasons:
        return PaperExecutionAttempt(
            requested=submit_requested,
            submitted=False,
            status="blocked",
            block_reasons=block_reasons,
            order_preview=order_preview,
            order_payload=payload if isinstance(payload, dict) else None,
            broker_response=None,
            broker_error=None,
        )

    try:
        broker_response = alpaca.submit_order(payload)
    except Exception as exc:  # noqa: BLE001 - preserve broker failure in attempt log
        return PaperExecutionAttempt(
            requested=submit_requested,
            submitted=False,
            status="broker_error",
            block_reasons=[],
            order_preview=order_preview,
            order_payload=payload,
            broker_response=None,
            broker_error=str(exc),
        )

    return PaperExecutionAttempt(
        requested=submit_requested,
        submitted=True,
        status="submitted",
        block_reasons=[],
        order_preview=order_preview,
        order_payload=payload,
        broker_response=broker_response,
        broker_error=None,
    )


def _paper_execution_block_reasons(
    *,
    config: AppConfig,
    kill_switch: KillSwitch,
    notifier: DiscordNotifier,
    submit_requested: bool,
    order_preview: dict[str, Any] | None,
    open_orders: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    allocation: dict[str, Any],
    state_refresh_error: str | None,
) -> list[str]:
    reasons: list[str] = []
    if state_refresh_error:
        reasons.append(f"Final execution state refresh failed: {state_refresh_error}")
    if not submit_requested:
        reasons.append("CLI did not request --submit-paper")
    if config.mode != "paper":
        reasons.append(f"Mode is not paper: {config.mode}")
    if not bool(config.get("execution", "enable_paper_orders", default=False)):
        reasons.append("execution.enable_paper_orders is false")
    if kill_switch.is_active():
        reasons.append(f"Kill switch is active at {kill_switch.path}")
    if not notifier.is_configured:
        reasons.append("Discord webhook is not configured")
    if not isinstance(order_preview, dict):
        reasons.append("No selected order preview is available")
        return reasons

    preview_errors = order_preview.get("errors") or []
    if preview_errors:
        reasons.append(f"Order preview has errors: {'; '.join(str(error) for error in preview_errors)}")
    if order_preview.get("submit_disabled") is not True:
        reasons.append("Order preview must be generated with submit_disabled=true")

    payload = order_preview.get("payload")
    if not isinstance(payload, dict):
        reasons.append("Order preview payload is unavailable")
        return reasons
    if payload.get("order_class") != "mleg":
        reasons.append("Order payload order_class is not mleg")
    if payload.get("type") != "limit":
        reasons.append("Order payload type is not limit")
    if bool(config.get("execution", "no_market_orders", default=True)) and payload.get("type") == "market":
        reasons.append("Market orders are disabled")

    selected_open = allocation.get("selected_open") if isinstance(allocation, dict) else None
    if not isinstance(selected_open, dict):
        reasons.append("No allocator-selected open is available")
    else:
        max_contracts = selected_open.get("max_contracts_under_open_risk")
        if not isinstance(max_contracts, int) or max_contracts < 1:
            reasons.append("Selected open does not fit max_open_risk budget")

    symbol = str(order_preview.get("symbol") or "")
    max_open_positions = int(config.get("risk", "max_open_positions", default=0))
    if max_open_positions > 0 and len(open_positions) >= max_open_positions:
        reasons.append(f"Max open positions reached: {len(open_positions)}/{max_open_positions}")
    if symbol and _has_duplicate_open_order(symbol=symbol, open_orders=open_orders):
        reasons.append(f"Duplicate open order already exists for {symbol}")

    max_open_risk = _decimal_or_none(config.get("risk", "max_open_risk", default=0)) or Decimal("0")
    preview_max_loss = _decimal_or_none(order_preview.get("estimated_max_loss")) or Decimal("0")
    if max_open_risk > 0 and preview_max_loss > max_open_risk:
        reasons.append(f"Preview max loss {preview_max_loss} exceeds max_open_risk {max_open_risk}")

    return reasons


def _has_duplicate_open_order(*, symbol: str, open_orders: list[dict[str, Any]]) -> bool:
    for order in open_orders:
        order_symbol = str(order.get("symbol") or "")
        if order_symbol == symbol:
            return True
        legs = order.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                if isinstance(leg, dict) and str(leg.get("symbol") or "").startswith(symbol):
                    return True
    return False


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

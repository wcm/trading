from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_bot.brokers.alpaca import AlpacaClient
from trading_bot.config import AppConfig
from trading_bot.execution.entry_orders import manage_entry_order_after_submission
from trading_bot.execution.revalidation import revalidate_put_credit_spread_entry_preview
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
    order_management: dict[str, Any] | None = None

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
    order_preview = _maybe_revalidate_entry_order_preview(
        config=config,
        alpaca=alpaca,
        submit_requested=submit_requested,
        order_preview=order_preview,
    )
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
            order_management=None,
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
            order_management=None,
        )

    order_management = None
    status = "submitted"
    if isinstance(order_preview, dict) and isinstance(broker_response, dict):
        order_management = manage_entry_order_after_submission(
            config=config,
            alpaca=alpaca,
            order_preview=order_preview,
            initial_order=broker_response,
        )
        final_management_status = order_management.get("final_status") if isinstance(order_management, dict) else None
        if final_management_status and final_management_status not in {"disabled", "unsupported"}:
            status = str(final_management_status)

    return PaperExecutionAttempt(
        requested=submit_requested,
        submitted=True,
        status=status,
        block_reasons=[],
        order_preview=order_preview,
        order_payload=payload,
        broker_response=broker_response,
        broker_error=None,
        order_management=order_management,
    )


def maybe_submit_paper_close_order(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    kill_switch: KillSwitch,
    notifier: DiscordNotifier,
    submit_requested: bool,
    spread: dict[str, Any] | None,
    order_preview: dict[str, Any] | None,
    open_orders: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    state_refresh_error: str | None = None,
) -> PaperExecutionAttempt:
    block_reasons = _paper_close_execution_block_reasons(
        config=config,
        kill_switch=kill_switch,
        notifier=notifier,
        submit_requested=submit_requested,
        spread=spread,
        order_preview=order_preview,
        open_orders=open_orders,
        open_positions=open_positions,
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
            order_management=None,
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
            order_management=None,
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
        order_management=None,
    )


def _maybe_revalidate_entry_order_preview(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    submit_requested: bool,
    order_preview: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not submit_requested:
        return order_preview
    if not bool(config.get("execution", "pre_submit_revalidate_quotes", default=True)):
        return order_preview
    if not isinstance(order_preview, dict):
        return order_preview
    if not callable(getattr(alpaca, "get_option_snapshots", None)):
        return order_preview
    try:
        return revalidate_put_credit_spread_entry_preview(
            config=config,
            alpaca=alpaca,
            order_preview=order_preview,
            adjustment_index=0,
        )
    except Exception as exc:  # noqa: BLE001 - execution gate must fail closed
        updated = dict(order_preview)
        updated.setdefault("errors", []).append(f"Revalidation failed: {exc}")
        updated["revalidation"] = {
            "kind": "put_credit_spread_entry_revalidation",
            "ok": False,
            "errors": [str(exc)],
            "warnings": [],
        }
        return updated


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


def _paper_close_execution_block_reasons(
    *,
    config: AppConfig,
    kill_switch: KillSwitch,
    notifier: DiscordNotifier,
    submit_requested: bool,
    spread: dict[str, Any] | None,
    order_preview: dict[str, Any] | None,
    open_orders: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    state_refresh_error: str | None,
) -> list[str]:
    reasons: list[str] = []
    if state_refresh_error:
        reasons.append(f"Final close execution state refresh failed: {state_refresh_error}")
    if not submit_requested:
        reasons.append("CLI did not request --submit-paper-close")
    if config.mode != "paper":
        reasons.append(f"Mode is not paper: {config.mode}")
    if not bool(config.get("execution", "enable_paper_close_orders", default=False)):
        reasons.append("execution.enable_paper_close_orders is false")
    if kill_switch.is_active():
        reasons.append(f"Kill switch is active at {kill_switch.path}")
    if not notifier.is_configured:
        reasons.append("Discord webhook is not configured")
    if not isinstance(spread, dict):
        reasons.append("No monitored spread is available")
    elif spread.get("close_recommended") is not True:
        reasons.append("Spread does not have close_recommended=true")

    if not isinstance(order_preview, dict):
        reasons.append("No close order preview is available")
        return reasons

    preview_errors = order_preview.get("errors") or []
    if preview_errors:
        reasons.append(f"Close order preview has errors: {'; '.join(str(error) for error in preview_errors)}")
    if order_preview.get("kind") != "alpaca_mleg_close_preview":
        reasons.append("Order preview is not an Alpaca MLeg close preview")
    if order_preview.get("submit_disabled") is not True:
        reasons.append("Close order preview must be generated with submit_disabled=true")

    payload = order_preview.get("payload")
    if not isinstance(payload, dict):
        reasons.append("Close order preview payload is unavailable")
        return reasons
    if payload.get("order_class") != "mleg":
        reasons.append("Close order payload order_class is not mleg")
    if payload.get("type") != "limit":
        reasons.append("Close order payload type is not limit")
    if bool(config.get("execution", "no_market_orders", default=True)) and payload.get("type") == "market":
        reasons.append("Market orders are disabled")

    leg_symbols = _payload_leg_symbols(payload)
    if len(leg_symbols) < 2:
        reasons.append("Close order payload must include at least two leg symbols")
    if _has_duplicate_leg_order(leg_symbols=leg_symbols, open_orders=open_orders):
        reasons.append("Duplicate close order already exists for at least one spread leg")
    missing_position_symbols = _missing_position_symbols(leg_symbols=leg_symbols, open_positions=open_positions)
    if missing_position_symbols:
        reasons.append(f"Spread legs are not all open positions: {', '.join(missing_position_symbols)}")

    intents = {
        str(leg.get("position_intent") or "")
        for leg in payload.get("legs", [])
        if isinstance(leg, dict)
    }
    if "buy_to_close" not in intents or "sell_to_close" not in intents:
        reasons.append("Close order payload must include buy_to_close and sell_to_close legs")

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


def _has_duplicate_leg_order(*, leg_symbols: set[str], open_orders: list[dict[str, Any]]) -> bool:
    if not leg_symbols:
        return False
    for order in open_orders:
        order_symbol = str(order.get("symbol") or "")
        if order_symbol in leg_symbols:
            return True
        legs = order.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                if isinstance(leg, dict) and str(leg.get("symbol") or "") in leg_symbols:
                    return True
    return False


def _payload_leg_symbols(payload: dict[str, Any]) -> set[str]:
    legs = payload.get("legs")
    if not isinstance(legs, list):
        return set()
    return {
        str(leg.get("symbol"))
        for leg in legs
        if isinstance(leg, dict) and leg.get("symbol")
    }


def _missing_position_symbols(*, leg_symbols: set[str], open_positions: list[dict[str, Any]]) -> list[str]:
    position_symbols = {
        str(position.get("symbol"))
        for position in open_positions
        if isinstance(position, dict) and position.get("symbol")
    }
    return sorted(symbol for symbol in leg_symbols if symbol not in position_symbols)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

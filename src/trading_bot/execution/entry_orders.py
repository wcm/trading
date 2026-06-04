from __future__ import annotations

import copy
import time
from datetime import UTC, datetime
from typing import Any

from trading_bot.brokers.alpaca import AlpacaClient
from trading_bot.config import AppConfig
from trading_bot.execution.revalidation import revalidate_put_credit_spread_entry_preview


TERMINAL_STATUSES = {
    "filled",
    "canceled",
    "expired",
    "rejected",
    "done_for_day",
}


def manage_entry_order_after_submission(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    order_preview: dict[str, Any],
    initial_order: dict[str, Any],
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    management = {
        "kind": "entry_order_timeout_cancel_replace",
        "enabled": bool(config.get("execution", "manage_entry_orders", default=True)),
        "started_at": started_at,
        "finished_at": None,
        "final_status": None,
        "timeout_seconds": int(config.get("execution", "entry_order_timeout_seconds", default=60)),
        "poll_seconds": max(1, int(config.get("execution", "entry_order_poll_seconds", default=5))),
        "max_price_adjustments": max(
            0,
            int(config.get("execution", "max_entry_price_adjustments", default=0)),
        ),
        "events": [],
        "orders": [],
        "errors": [],
    }

    if not management["enabled"]:
        return _finish(management, "disabled")
    if not _supports_entry_management(alpaca):
        management["errors"].append("Broker client does not support get_order/cancel_order")
        return _finish(management, "unsupported")

    current_order = dict(initial_order)
    current_preview = copy.deepcopy(order_preview)
    adjustment_index = 0

    while True:
        current_order_id = str(current_order.get("id") or "")
        if not current_order_id:
            management["errors"].append("Submitted order response did not include an order id")
            return _finish(management, "missing_order_id")

        management["orders"].append(_compact_order(current_order))
        wait_result = _wait_for_order_resolution(
            alpaca=alpaca,
            order_id=current_order_id,
            timeout_seconds=management["timeout_seconds"],
            poll_seconds=management["poll_seconds"],
        )
        management["events"].extend(wait_result["events"])
        latest_order = wait_result.get("latest_order") or current_order
        management["orders"].append(_compact_order(latest_order))

        latest_status = _order_status(latest_order)
        if latest_status == "filled":
            return _finish(management, "filled")
        if _filled_quantity(latest_order) > 0:
            return _finish(management, f"{latest_status or 'unknown'}_with_partial_fill")
        if latest_status in TERMINAL_STATUSES:
            return _finish(management, latest_status or "terminal")
        if wait_result.get("timed_out") is not True:
            return _finish(management, latest_status or "unknown")

        cancel_event = _cancel_open_order(alpaca=alpaca, order_id=current_order_id)
        management["events"].append(cancel_event)
        if cancel_event.get("error"):
            management["errors"].append(str(cancel_event["error"]))
            return _finish(management, "cancel_error")

        cancel_wait = _wait_for_order_resolution(
            alpaca=alpaca,
            order_id=current_order_id,
            timeout_seconds=max(5, management["poll_seconds"]),
            poll_seconds=management["poll_seconds"],
        )
        management["events"].extend(cancel_wait["events"])
        canceled_order = cancel_wait.get("latest_order") or latest_order
        management["orders"].append(_compact_order(canceled_order))
        canceled_status = _order_status(canceled_order)
        if canceled_status == "filled":
            return _finish(management, "filled_after_cancel_request")
        if _filled_quantity(canceled_order) > 0:
            return _finish(management, f"{canceled_status or 'unknown'}_with_partial_fill")
        if canceled_status != "canceled":
            return _finish(management, f"cancel_not_confirmed_{canceled_status or 'unknown'}")

        adjustment_index += 1
        if adjustment_index > management["max_price_adjustments"]:
            return _finish(management, "canceled_unfilled")

        replacement_preview = revalidate_put_credit_spread_entry_preview(
            config=config,
            alpaca=alpaca,
            order_preview=current_preview,
            adjustment_index=adjustment_index,
        )
        replacement_preview = _with_replacement_client_order_id(replacement_preview, adjustment_index)
        revalidation = replacement_preview.get("revalidation") or {}
        if revalidation.get("ok") is not True or replacement_preview.get("errors"):
            management["events"].append(
                {
                    "event": "replacement_blocked",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "adjustment_index": adjustment_index,
                    "revalidation": revalidation,
                    "errors": replacement_preview.get("errors") or [],
                }
            )
            return _finish(management, "replacement_blocked")

        try:
            replacement_order = alpaca.submit_order(replacement_preview["payload"])
        except Exception as exc:  # noqa: BLE001 - preserve broker failure in artifact
            management["errors"].append(str(exc))
            management["events"].append(
                {
                    "event": "replacement_submit_error",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "adjustment_index": adjustment_index,
                    "error": str(exc),
                }
            )
            return _finish(management, "replacement_submit_error")

        management["events"].append(
            {
                "event": "replacement_submitted",
                "observed_at": datetime.now(UTC).isoformat(),
                "adjustment_index": adjustment_index,
                "limit_price": replacement_preview.get("payload", {}).get("limit_price"),
                "broker_order_id": replacement_order.get("id"),
                "client_order_id": replacement_order.get("client_order_id"),
                "status": replacement_order.get("status"),
            }
        )
        current_order = dict(replacement_order)
        current_preview = replacement_preview


def _supports_entry_management(alpaca: AlpacaClient) -> bool:
    return callable(getattr(alpaca, "get_order", None)) and callable(getattr(alpaca, "cancel_order", None))


def _wait_for_order_resolution(
    *,
    alpaca: AlpacaClient,
    order_id: str,
    timeout_seconds: int,
    poll_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, timeout_seconds)
    events: list[dict[str, Any]] = []
    latest_order: dict[str, Any] | None = None

    while True:
        try:
            latest_order = alpaca.get_order(order_id)
        except Exception as exc:  # noqa: BLE001 - preserve broker failure in artifact
            return {
                "timed_out": False,
                "latest_order": latest_order,
                "events": events
                + [
                    {
                        "event": "poll_error",
                        "observed_at": datetime.now(UTC).isoformat(),
                        "broker_order_id": order_id,
                        "error": str(exc),
                    }
                ],
            }

        status = _order_status(latest_order)
        events.append(
            {
                "event": "order_poll",
                "observed_at": datetime.now(UTC).isoformat(),
                "broker_order_id": order_id,
                "status": status,
                "filled_qty": latest_order.get("filled_qty"),
                "limit_price": latest_order.get("limit_price"),
            }
        )
        if status in TERMINAL_STATUSES or _filled_quantity(latest_order) > 0:
            return {"timed_out": False, "latest_order": latest_order, "events": events}

        if time.monotonic() >= deadline:
            events.append(
                {
                    "event": "entry_timeout",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "broker_order_id": order_id,
                    "timeout_seconds": timeout_seconds,
                }
            )
            return {"timed_out": True, "latest_order": latest_order, "events": events}

        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def _cancel_open_order(*, alpaca: AlpacaClient, order_id: str) -> dict[str, Any]:
    try:
        response = alpaca.cancel_order(order_id)
    except Exception as exc:  # noqa: BLE001 - preserve broker failure in artifact
        return {
            "event": "cancel_error",
            "observed_at": datetime.now(UTC).isoformat(),
            "broker_order_id": order_id,
            "error": str(exc),
        }
    return {
        "event": "cancel_requested",
        "observed_at": datetime.now(UTC).isoformat(),
        "broker_order_id": order_id,
        "broker_response": response,
    }


def _with_replacement_client_order_id(order_preview: dict[str, Any], adjustment_index: int) -> dict[str, Any]:
    updated = copy.deepcopy(order_preview)
    payload = updated.get("payload")
    if not isinstance(payload, dict):
        return updated
    client_order_id = payload.get("client_order_id")
    if not client_order_id:
        return updated
    base = str(client_order_id)
    suffix = f"-r{adjustment_index}"
    payload["client_order_id"] = f"{base[: 48 - len(suffix)]}{suffix}"
    return updated


def _finish(management: dict[str, Any], final_status: str) -> dict[str, Any]:
    management["final_status"] = final_status
    management["finished_at"] = datetime.now(UTC).isoformat()
    return management


def _order_status(order: dict[str, Any] | None) -> str | None:
    if not isinstance(order, dict):
        return None
    status = order.get("status")
    return str(status).lower() if status is not None else None


def _filled_quantity(order: dict[str, Any] | None) -> float:
    if not isinstance(order, dict):
        return 0.0
    try:
        return float(order.get("filled_qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _compact_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "client_order_id": order.get("client_order_id"),
        "status": order.get("status"),
        "limit_price": order.get("limit_price"),
        "filled_qty": order.get("filled_qty"),
        "qty": order.get("qty"),
        "submitted_at": order.get("submitted_at"),
        "filled_at": order.get("filled_at"),
        "canceled_at": order.get("canceled_at"),
        "expired_at": order.get("expired_at"),
    }

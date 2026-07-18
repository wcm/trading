from __future__ import annotations

from decimal import Decimal

from trading_bot.commands.dca import (
    _dca_client_order_id,
    _dca_market_order_payload,
    _reconcile_dca_orders,
)
from trading_bot.dca.notifications import dca_event_messages
from trading_bot.dca.state import (
    DcaPurchaseState,
    DcaState,
    load_dca_state,
    save_dca_state,
)


def test_fractional_dca_uses_exact_notional_market_order() -> None:
    payload = _dca_market_order_payload(
        symbol="QQQ",
        contribution_amount=Decimal("500"),
        price=Decimal("600"),
        allow_fractional_shares=True,
        client_order_id="dca-qqq-monthly202607",
    )

    assert payload == {
        "symbol": "QQQ",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": "dca-qqq-monthly202607",
        "notional": "500.00",
    }


def test_whole_share_dca_rounds_down_quantity() -> None:
    payload = _dca_market_order_payload(
        symbol="QQQ",
        contribution_amount=Decimal("500"),
        price=Decimal("190"),
        allow_fractional_shares=False,
        client_order_id="dca-qqq-monthly202607",
    )

    assert payload["qty"] == "2"
    assert "notional" not in payload


def test_dca_client_order_id_is_stable_for_period() -> None:
    first = _dca_client_order_id("QQQ", "monthly:2026-07")
    second = _dca_client_order_id("QQQ", "monthly:2026-07")

    assert first == second == "dca-qqq-monthly202607"


def test_dca_state_round_trip_preserves_completed_period(tmp_path) -> None:
    path = tmp_path / "dca_state.json"
    state = DcaState(
        strategy_name="dca_qqq",
        symbol="QQQ",
        purchases=[_purchase()],
    )

    save_dca_state(path, state)
    restored = load_dca_state(path, strategy_name="dca_qqq", symbol="QQQ")

    assert restored.completed_period_keys() == {"monthly:2026-07"}
    assert restored.annual_contributed(2026) == Decimal("500")


def test_dca_fill_notification_is_simple() -> None:
    messages = dca_event_messages(
        {
            "symbol": "QQQ",
            "reconciliation_events": [
                {
                    "order_status": "filled",
                    "filled_qty": "0.8",
                    "filled_avg_price": "625",
                    "contribution_amount": "500",
                }
            ],
        }
    )

    assert messages == [
        "# DCA Buy Filled\n\n"
        "**QQQ:** 0.8 shares at $625\n"
        "**Invested:** $500"
    ]


def test_terminal_partial_fill_is_completed_and_not_polled_again() -> None:
    purchase = _purchase()
    purchase.status = "partially_filled"
    purchase.filled_at = None
    purchase.filled_qty = Decimal("0.25")
    purchase.filled_avg_price = Decimal("620")
    purchase.last_order_status = "partially_filled"
    state = DcaState(
        strategy_name="dca_qqq",
        symbol="QQQ",
        purchases=[purchase],
    )
    broker = _CanceledPartialFillBroker()

    events = _reconcile_dca_orders(broker, state, _NullLogger())

    assert events[0]["order_status"] == "canceled"
    assert purchase.status == "partially_filled_closed"
    assert state.completed_period_keys() == {"monthly:2026-07"}
    assert state.summary()["filled_purchase_count"] == 1

    assert _reconcile_dca_orders(broker, state, _NullLogger()) == []
    assert broker.calls == 1


def _purchase() -> DcaPurchaseState:
    return DcaPurchaseState(
        purchase_id="dca-1",
        period_key="monthly:2026-07",
        scheduled_date="2026-07-01",
        contribution_amount=Decimal("500"),
        status="filled",
        client_order_id="dca-qqq-monthly202607",
        broker_order_id="order-1",
        submitted_at="2026-07-01T14:00:00+00:00",
        filled_at="2026-07-01T14:00:01+00:00",
        filled_qty=Decimal("0.8"),
        filled_avg_price=Decimal("625"),
    )


class _CanceledPartialFillBroker:
    def __init__(self) -> None:
        self.calls = 0

    def get_order(self, _order_id: str) -> dict[str, str]:
        self.calls += 1
        return {
            "status": "canceled",
            "filled_qty": "0.25",
            "filled_avg_price": "620",
            "filled_at": "2026-07-01T14:00:02+00:00",
        }


class _NullLogger:
    def info(self, *_args: object, **_kwargs: object) -> None:
        return None

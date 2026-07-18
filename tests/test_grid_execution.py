from __future__ import annotations

from decimal import Decimal

from trading_bot.commands.grid import (
    _equity_limit_order_payload,
    _grid_sell_time_in_force,
    _reconcile_buy_order,
    _reconcile_sell_order,
)
from trading_bot.grid.notifications import grid_event_messages
from trading_bot.grid.reconciliation import (
    GridBrokerSnapshot,
    grid_reconciliation_errors,
)
from trading_bot.grid.state import GridLotState, GridState
from trading_bot.grid.strategy import GridStrategyConfig


def test_broker_reconciliation_accepts_matching_position_and_order() -> None:
    state = GridState(strategy_name="grid_tqqq", symbol="TQQQ")
    state.lots.append(_open_lot(status="sell_submitted", sell_order_id="sell-1"))
    snapshot = GridBrokerSnapshot(
        symbol="TQQQ",
        position_qty=Decimal("5"),
        open_orders=[{"id": "sell-1", "symbol": "TQQQ"}],
    )

    assert grid_reconciliation_errors(state, snapshot) == []


def test_broker_reconciliation_blocks_unexplained_position_and_order() -> None:
    state = GridState(strategy_name="grid_tqqq", symbol="TQQQ")
    snapshot = GridBrokerSnapshot(
        symbol="TQQQ",
        position_qty=Decimal("5"),
        open_orders=[{"id": "manual-1", "symbol": "TQQQ"}],
    )

    errors = grid_reconciliation_errors(state, snapshot)

    assert any("unexplained" in error for error in errors)
    assert any("position mismatch" in error for error in errors)


def test_broker_reconciliation_accounts_for_partial_sell_fill() -> None:
    state = GridState(strategy_name="grid_tqqq", symbol="TQQQ")
    lot = _open_lot(status="sell_submitted", sell_order_id="sell-1")
    lot.sell_filled_qty = Decimal("2")
    state.lots.append(lot)
    snapshot = GridBrokerSnapshot(
        symbol="TQQQ",
        position_qty=Decimal("3"),
        open_orders=[{"id": "sell-1", "symbol": "TQQQ"}],
    )

    assert grid_reconciliation_errors(state, snapshot) == []


def test_filled_buy_becomes_open_with_new_sell_target() -> None:
    lot = GridLotState(
        lot_id="lot-1",
        level_index=1,
        buy_price=Decimal("97"),
        sell_target=Decimal("99.91"),
        planned_notional=Decimal("485"),
        status="buy_submitted",
        buy_order_id="buy-1",
    )

    event = _reconcile_buy_order(
        lot,
        {
            "status": "filled",
            "filled_qty": "5",
            "filled_avg_price": "96.80",
            "filled_at": "2026-07-13T15:00:00Z",
        },
        _config(),
    )

    assert lot.status == "open"
    assert lot.qty == Decimal("5")
    assert lot.sell_target == Decimal("99.70")
    assert event is not None
    assert event["sell_target"] == "99.7"


def test_partial_sell_fill_tracks_remaining_inventory() -> None:
    lot = _open_lot(status="sell_submitted", sell_order_id="sell-1")

    event = _reconcile_sell_order(
        lot,
        {
            "status": "partially_filled",
            "filled_qty": "2",
            "filled_avg_price": "100",
        },
    )

    assert lot.sell_filled_qty == Decimal("2")
    assert lot.remaining_qty() == Decimal("3")
    assert event is not None
    assert event["remaining_qty"] == "3"


def test_later_partial_sell_fill_is_processed_without_status_change() -> None:
    lot = _open_lot(status="sell_submitted", sell_order_id="sell-1")
    lot.last_order_status = "partially_filled"
    lot.sell_filled_qty = Decimal("2")

    event = _reconcile_sell_order(
        lot,
        {
            "status": "partially_filled",
            "filled_qty": "3",
            "filled_avg_price": "100",
        },
    )

    assert event is not None
    assert lot.sell_filled_qty == Decimal("3")
    assert lot.remaining_qty() == Decimal("2")


def test_equity_limit_orders_support_day_buys_and_gtc_sells() -> None:
    buy = _equity_limit_order_payload(
        symbol="TQQQ",
        side="buy",
        qty=Decimal("5"),
        limit_price=Decimal("97"),
        client_order_id="grid-buy-1",
        time_in_force="day",
    )
    sell = _equity_limit_order_payload(
        symbol="TQQQ",
        side="sell",
        qty=Decimal("5"),
        limit_price=Decimal("99.91"),
        client_order_id="grid-sell-1",
        time_in_force="gtc",
    )

    assert buy["time_in_force"] == "day"
    assert sell["time_in_force"] == "gtc"


def test_fractional_grid_sells_use_day_time_in_force() -> None:
    assert _grid_sell_time_in_force(Decimal("5")) == "gtc"
    assert _grid_sell_time_in_force(Decimal("5.250001")) == "day"


def test_expired_fractional_sell_returns_lot_to_open_for_recreation() -> None:
    lot = _open_lot(
        status="sell_submitted",
        sell_order_id="sell-1",
        qty=Decimal("5.250001"),
    )

    event = _reconcile_sell_order(
        lot,
        {
            "status": "expired",
            "filled_qty": "0",
        },
    )

    assert lot.status == "open"
    assert lot.qty == Decimal("5.250001")
    assert lot.sell_order_id is None
    assert event is not None
    assert event["order_status"] == "expired"


def test_grid_discord_messages_are_event_only() -> None:
    quiet_artifact = {
        "symbol": "TQQQ",
        "safety_errors": [],
        "reconciliation_events": [],
        "submitted_orders": [],
    }
    event_artifact = {
        "symbol": "TQQQ",
        "safety_errors": [],
        "reconciliation_events": [
            {
                "side": "buy",
                "order_status": "filled",
                "filled_qty": "5",
                "fill_price": "96.8",
                "sell_target": "99.7",
                "level_index": 1,
            }
        ],
        "submitted_orders": [],
    }

    assert grid_event_messages(quiet_artifact) == []
    messages = grid_event_messages(event_artifact)
    assert len(messages) == 1
    assert messages[0].startswith("# Grid Buy Filled")
    assert "Sell target" in messages[0]


def test_grid_discord_can_include_cycle_status() -> None:
    artifact = {
        "symbol": "TQQQ",
        "market_open": True,
        "bar": {"open": "75.00", "close": "74.25"},
        "state": {
            "anchor_price": "76.00",
            "open_inventory_lot_count": 1,
            "unrealized_pnl": "-12.50",
        },
        "broker_snapshot": {
            "position_qty": "5",
            "open_orders": [{"id": "sell-1"}],
        },
        "next_buy_level": {"level_index": 2, "price": "71.51"},
        "safety_errors": [],
        "reconciliation_events": [],
        "submitted_orders": [],
    }

    messages = grid_event_messages(artifact, include_status=True)

    assert len(messages) == 1
    assert messages[0].startswith("# Grid Status")
    assert "**TQQQ price:** $74.25" in messages[0]
    assert "**Latest 5-minute change:** -$0.75 (-1.00%)" in messages[0]
    assert "**Next buy:** Level 2 at $71.51" in messages[0]
    assert "**Shares held:** 5" in messages[0]
    assert "**Working orders:** 1" in messages[0]
    assert "**Unrealized P&L:** -$12.50" in messages[0]
    assert "**Status:** Holding shares and waiting to sell" in messages[0]


def _open_lot(
    *,
    status: str,
    sell_order_id: str | None = None,
    qty: Decimal = Decimal("5"),
) -> GridLotState:
    return GridLotState(
        lot_id="lot-1",
        level_index=1,
        buy_price=Decimal("97"),
        sell_target=Decimal("99.91"),
        planned_notional=Decimal("485"),
        status=status,
        qty=qty,
        buy_fill_price=Decimal("97"),
        sell_order_id=sell_order_id,
    )


def _config() -> GridStrategyConfig:
    return GridStrategyConfig(
        name="grid_tqqq",
        symbol="TQQQ",
        strategy_capital=Decimal("10000"),
        grid_spacing_pct=Decimal("3"),
        base_order_notional=Decimal("400"),
        max_buy_levels_below_anchor=16,
        max_inventory_value=Decimal("8000"),
        cash_reserve=Decimal("2000"),
        max_unrealized_loss=Decimal("1200"),
        pause_new_buys_after_consecutive_down_levels=5,
        recenter_up_pct=Decimal("5"),
        adaptive_sizing_enabled=True,
        adaptive_scale_factor=Decimal("8"),
        adaptive_max_order_multiplier=Decimal("2"),
        max_single_order_notional=Decimal("800"),
    )

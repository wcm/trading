from __future__ import annotations

from decimal import Decimal

from trading_bot.backtesting.bars import PriceBar
from trading_bot.grid.state import GridLotState, GridState
from trading_bot.grid.strategy import GridStrategyConfig, build_grid_plan


def test_grid_cycle_initializes_anchor_without_buying() -> None:
    state = GridState(strategy_name="grid_tqqq", symbol="TQQQ")

    plan = build_grid_plan(state, _config(), _bar(open_="100", high="101", low="99", close="100"))

    assert state.anchor_price == Decimal("100.00")
    assert plan.intents == []
    assert plan.events == ["Initialized anchor at 100.00"]


def test_grid_cycle_creates_buy_intent_when_level_is_reached() -> None:
    state = GridState(strategy_name="grid_tqqq", symbol="TQQQ", anchor_price=Decimal("100"))

    plan = build_grid_plan(state, _config(), _bar(open_="100", high="100", low="97", close="97.50"))

    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert intent.action == "buy"
    assert intent.level_index == 1
    assert intent.price == Decimal("97.00")
    assert intent.sell_target == Decimal("99.91")
    assert intent.qty == Decimal("5")


def test_grid_cycle_uses_fractional_shares_to_match_the_target_notional() -> None:
    state = GridState(strategy_name="grid_tqqq", symbol="TQQQ", anchor_price=Decimal("100"))

    plan = build_grid_plan(
        state,
        _config(allow_fractional_shares=True),
        _bar(open_="100", high="100", low="97", close="97.50"),
    )

    intent = plan.intents[0]
    assert intent.qty == Decimal("5.113402")
    assert intent.notional == Decimal("495.999994")


def test_grid_cycle_adaptive_size_increases_deeper_buys() -> None:
    state = GridState(strategy_name="grid_tqqq", symbol="TQQQ", anchor_price=Decimal("100"))

    plan = build_grid_plan(state, _config(), _bar(open_="100", high="100", low="94", close="94"))

    assert [intent.level_index for intent in plan.intents] == [1, 2]
    assert plan.intents[0].notional == Decimal("485.00")
    assert plan.intents[1].notional == Decimal("564.54")


def test_grid_cycle_blocks_new_buys_after_active_down_level_cap() -> None:
    state = GridState(strategy_name="grid_tqqq", symbol="TQQQ", anchor_price=Decimal("100"))
    for level in range(1, 6):
        state.lots.append(
            GridLotState(
                lot_id=f"lot-{level}",
                level_index=level,
                buy_price=Decimal("90"),
                sell_target=Decimal("92.70"),
                planned_notional=Decimal("400"),
                status="sell_submitted",
                qty=Decimal("4"),
                buy_fill_price=Decimal("90"),
            )
        )

    plan = build_grid_plan(state, _config(), _bar(open_="85", high="85", low="80", close="80"))

    assert plan.intents == []
    assert plan.blocked
    assert {item["reason"] for item in plan.blocked} == {"consecutive_down_levels"}


def test_grid_cycle_places_paired_sell_immediately_and_skips_same_cycle_buys() -> None:
    state = GridState(strategy_name="grid_tqqq", symbol="TQQQ", anchor_price=Decimal("100"))
    state.lots.append(
        GridLotState(
            lot_id="lot-1",
            level_index=1,
            buy_price=Decimal("97"),
            sell_target=Decimal("99.91"),
            planned_notional=Decimal("388"),
            status="open",
            qty=Decimal("4"),
            buy_fill_price=Decimal("97"),
        )
    )

    plan = build_grid_plan(state, _config(), _bar(open_="98", high="98", low="94", close="96"))

    assert len(plan.intents) == 1
    assert plan.intents[0].action == "sell"
    assert plan.intents[0].lot_id == "lot-1"
    assert plan.intents[0].price == Decimal("99.91")
    assert "new buys are skipped" in plan.events[-1]


def _config(*, allow_fractional_shares: bool = False) -> GridStrategyConfig:
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
        allow_fractional_shares=allow_fractional_shares,
    )


def _bar(*, open_: str, high: str, low: str, close: str) -> PriceBar:
    return PriceBar(
        timestamp="2026-06-18T14:30:00+00:00",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )

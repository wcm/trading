from __future__ import annotations

from decimal import Decimal

from trading_bot.backtesting.bars import PriceBar
from trading_bot.backtesting.grid import GridBacktestConfig, run_grid_backtest


def test_grid_backtest_does_not_allow_same_bar_buy_and_sell() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="106", low="95", close="95"),
        _bar("2026-01-05T14:30:00+00:00", open_="95", high="100", low="94", close="100"),
    ]

    result = run_grid_backtest(bars, _config())

    assert result.metrics["buy_count"] == 1
    assert result.metrics["sell_count"] == 1
    assert result.metrics["realized_pnl"] == "23.75"
    assert result.trades[0].side == "buy"
    assert result.trades[0].timestamp == bars[0].timestamp
    assert result.trades[1].side == "sell"
    assert result.trades[1].timestamp == bars[1].timestamp


def test_grid_backtest_blocks_buys_when_inventory_limit_is_reached() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="100", low="80", close="80"),
    ]

    result = run_grid_backtest(
        bars,
        _config(max_inventory_value=Decimal("600"), max_unrealized_loss=None),
    )

    assert result.metrics["buy_count"] == 1
    assert result.metrics["sell_count"] == 0
    assert result.risk_block_counts["max_inventory_value"] >= 1
    assert result.metrics["open_lot_count"] == 1


def test_grid_backtest_can_cap_active_buy_levels() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="100", low="70", close="70"),
    ]

    result = run_grid_backtest(
        bars,
        _config(
            max_inventory_value=Decimal("10000"),
            max_unrealized_loss=None,
            pause_new_buys_after_consecutive_down_levels=2,
        ),
    )

    assert result.metrics["buy_count"] == 2
    assert result.risk_block_counts["consecutive_down_levels"] >= 1
    assert result.metrics["paused_days"] == 1


def test_grid_backtest_active_level_cap_still_applies_after_a_sell() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="100", low="90", close="90"),
        _bar("2026-01-05T14:30:00+00:00", open_="90", high="95", low="90", close="92"),
        _bar("2026-01-06T14:30:00+00:00", open_="92", high="94", low="80", close="80"),
    ]

    result = run_grid_backtest(
        bars,
        _config(
            max_inventory_value=Decimal("10000"),
            max_unrealized_loss=None,
            pause_new_buys_after_consecutive_down_levels=2,
        ),
    )

    assert result.metrics["buy_count"] == 3
    assert result.metrics["sell_count"] == 1
    assert result.metrics["open_lot_count"] == 2
    assert result.risk_block_counts["consecutive_down_levels"] >= 1


def test_grid_backtest_waits_until_next_bar_after_any_sell_fill() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="100", low="90", close="90"),
        _bar("2026-01-05T14:30:00+00:00", open_="90", high="95", low="90", close="90"),
    ]

    result = run_grid_backtest(
        bars,
        _config(max_inventory_value=Decimal("10000"), max_unrealized_loss=None),
    )

    assert result.metrics["buy_count"] == 2
    assert result.metrics["sell_count"] == 1
    assert result.metrics["open_lot_count"] == 1


def test_grid_backtest_persists_buy_grid_across_bars() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="100", low="98", close="98"),
        _bar("2026-01-05T14:30:00+00:00", open_="98", high="98", low="95", close="95"),
    ]

    result = run_grid_backtest(
        bars,
        _config(max_inventory_value=Decimal("10000"), max_unrealized_loss=None),
    )

    assert result.metrics["buy_count"] == 1
    assert result.trades[0].price == Decimal("95.00")


def test_grid_backtest_recenters_up_when_flat_without_same_bar_buy() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="106", low="99", close="105"),
    ]

    result = run_grid_backtest(
        bars,
        _config(recenter_up_pct=Decimal("5")),
    )

    assert result.metrics["recenter_count"] == 1
    assert result.metrics["buy_count"] == 0


def test_grid_backtest_buys_from_recentered_anchor_on_later_bar() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="106", low="100", close="105"),
        _bar("2026-01-05T14:30:00+00:00", open_="105", high="105", low="99.75", close="100"),
    ]

    result = run_grid_backtest(
        bars,
        _config(recenter_up_pct=Decimal("5")),
    )

    assert result.metrics["recenter_count"] == 1
    assert result.metrics["buy_count"] == 1
    assert result.trades[0].price == Decimal("99.75")
    assert result.metrics["open_lot_count"] == 1


def test_grid_backtest_can_require_multiple_closes_before_recentering() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="106", low="100", close="105"),
        _bar("2026-01-05T14:30:00+00:00", open_="105", high="107", low="104", close="106"),
    ]

    result = run_grid_backtest(
        bars,
        _config(recenter_up_pct=Decimal("5"), recenter_confirmation_bars=2),
    )

    assert result.metrics["recenter_count"] == 1
    assert result.metrics["buy_count"] == 0


def test_grid_backtest_keeps_anchor_after_all_lots_are_sold() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="100", low="95", close="95"),
        _bar("2026-01-05T14:30:00+00:00", open_="95", high="100", low="95", close="100"),
        _bar("2026-01-06T14:30:00+00:00", open_="96", high="96", low="94", close="94"),
    ]

    result = run_grid_backtest(bars, _config())

    assert result.metrics["buy_count"] == 2
    assert result.metrics["sell_count"] == 1
    assert result.trades[-1].side == "buy"
    assert result.trades[-1].price == Decimal("95.00")


def test_grid_backtest_adaptive_sizing_increases_deeper_buy_size() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="100", low="90", close="90"),
    ]

    result = run_grid_backtest(
        bars,
        _config(
            max_inventory_value=Decimal("10000"),
            max_unrealized_loss=None,
            adaptive_sizing_enabled=True,
            adaptive_scale_factor=Decimal("5"),
            adaptive_max_order_multiplier=Decimal("3"),
        ),
    )

    assert result.trades[0].price == Decimal("95.00")
    assert result.trades[0].shares == Decimal("6")
    assert result.trades[1].price == Decimal("90.25")
    assert result.trades[1].shares == Decimal("8")


def test_grid_backtest_adaptive_sizing_respects_order_cap() -> None:
    bars = [
        _bar("2026-01-02T14:30:00+00:00", open_="100", high="100", low="95", close="95"),
    ]

    result = run_grid_backtest(
        bars,
        _config(
            max_inventory_value=Decimal("10000"),
            max_unrealized_loss=None,
            adaptive_sizing_enabled=True,
            adaptive_scale_factor=Decimal("20"),
            adaptive_max_order_multiplier=Decimal("2"),
            max_single_order_notional=Decimal("900"),
        ),
    )

    assert result.metrics["buy_count"] == 1
    assert result.trades[0].price == Decimal("95.00")
    assert result.trades[0].notional == Decimal("855.00")


def _config(
    *,
    max_inventory_value: Decimal = Decimal("8000"),
    max_unrealized_loss: Decimal | None = Decimal("1200"),
    pause_new_buys_after_consecutive_down_levels: int | None = 5,
    recenter_up_pct: Decimal | None = None,
    recenter_confirmation_bars: int = 1,
    adaptive_sizing_enabled: bool = False,
    adaptive_scale_factor: Decimal = Decimal("0"),
    adaptive_max_order_multiplier: Decimal = Decimal("1"),
    max_single_order_notional: Decimal | None = None,
) -> GridBacktestConfig:
    return GridBacktestConfig(
        symbol="TQQQ",
        starting_cash=Decimal("10000"),
        grid_spacing_pct=Decimal("5"),
        base_order_notional=Decimal("500"),
        max_buy_levels_below_anchor=16,
        max_inventory_value=max_inventory_value,
        cash_reserve=Decimal("2000"),
        max_unrealized_loss=max_unrealized_loss,
        pause_new_buys_after_consecutive_down_levels=pause_new_buys_after_consecutive_down_levels,
        recenter_up_pct=recenter_up_pct,
        recenter_confirmation_bars=recenter_confirmation_bars,
        adaptive_sizing_enabled=adaptive_sizing_enabled,
        adaptive_scale_factor=adaptive_scale_factor,
        adaptive_max_order_multiplier=adaptive_max_order_multiplier,
        max_single_order_notional=max_single_order_notional,
    )


def _bar(timestamp: str, *, open_: str, high: str, low: str, close: str) -> PriceBar:
    return PriceBar(
        timestamp=timestamp,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )

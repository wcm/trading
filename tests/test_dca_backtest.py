from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from trading_bot.backtesting.bars import PriceBar
from trading_bot.backtesting.dca import run_dca_backtest
from trading_bot.dca.strategy import DcaStrategyConfig


def test_fixed_monthly_dca_invests_same_amount_each_month() -> None:
    result = run_dca_backtest(_bars(), _config())

    assert result.metrics["contribution_count"] == 3
    assert result.metrics["total_contributed"] == "300.00"
    assert result.metrics["total_shares"] == "3.250000"
    assert result.metrics["final_value"] == "325.00"
    assert result.metrics["investment_gain"] == "25.00"
    assert result.metrics["portfolio_max_drawdown_pct"] == "20.00"
    assert result.metrics["portfolio_max_drawdown_date"] == "2026-02-02"
    assert result.metrics["worst_unrealized_loss"] == "-20.00"
    assert result.metrics["worst_unrealized_loss_pct"] == "-20.00"
    assert {purchase.contribution_amount for purchase in result.purchases} == {
        Decimal("100.00")
    }


def test_drawdown_scaled_dca_invests_more_below_prior_peak() -> None:
    result = run_dca_backtest(
        _bars(),
        _config(sizing_mode="drawdown_scaled"),
    )

    assert result.metrics["total_contributed"] == "340.00"
    assert result.metrics["total_shares"] == "3.750000"
    assert result.metrics["final_value"] == "375.00"
    assert result.metrics["investment_gain"] == "35.00"
    assert result.metrics["largest_contribution"] == "140.00"
    assert result.purchases[1].drawdown_pct == Decimal("20.0")


def test_drawdown_sizing_ignores_peaks_older_than_lookback() -> None:
    config = _config(sizing_mode="drawdown_scaled")
    config = replace(config, drawdown_lookback_days=30)
    bars = [
        _bar("2025-01-02T21:00:00+00:00", "100"),
        _bar("2026-01-02T21:00:00+00:00", "80"),
    ]

    result = run_dca_backtest(bars, config)

    assert result.purchases[-1].drawdown_pct == Decimal("0")
    assert result.purchases[-1].contribution_amount == Decimal("100.00")


def _bars() -> list[PriceBar]:
    return [
        _bar("2026-01-02T21:00:00+00:00", "100"),
        _bar("2026-02-02T21:00:00+00:00", "80"),
        _bar("2026-03-02T21:00:00+00:00", "100"),
    ]


def _bar(timestamp: str, price: str) -> PriceBar:
    value = Decimal(price)
    return PriceBar(
        timestamp=timestamp,
        open=value,
        high=value,
        low=value,
        close=value,
    )


def _config(*, sizing_mode: str = "fixed") -> DcaStrategyConfig:
    return DcaStrategyConfig(
        name="dca_qqq",
        symbol="QQQ",
        frequency="monthly",
        base_contribution=Decimal("100"),
        day_of_month=1,
        biweekly_anchor_date=date(2026, 1, 2),
        sizing_mode=sizing_mode,
        drawdown_scale_factor=Decimal("2"),
        max_contribution_multiplier=Decimal("2"),
        max_contribution_per_purchase=Decimal("500"),
        max_annual_contribution=Decimal("12000"),
        allow_fractional_shares=True,
    )

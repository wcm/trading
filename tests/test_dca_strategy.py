from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from trading_bot.dca.strategy import (
    DcaStrategyConfig,
    build_dca_plan,
    contribution_for_drawdown,
    due_dca_period,
)


def test_monthly_dca_runs_on_first_check_after_scheduled_weekend() -> None:
    config = _config(day_of_month=1)

    due = due_dca_period(
        as_of=date(2026, 8, 3),
        config=config,
        completed_period_keys=set(),
    )

    assert due is not None
    assert due.period_key == "monthly:2026-08"
    assert due.scheduled_date == date(2026, 8, 1)


def test_monthly_dca_does_not_repeat_completed_period() -> None:
    config = _config()

    due = due_dca_period(
        as_of=date(2026, 7, 15),
        config=config,
        completed_period_keys={"monthly:2026-07"},
    )

    assert due is None


def test_monthly_day_is_capped_to_last_day_of_short_month() -> None:
    due = due_dca_period(
        as_of=date(2026, 2, 28),
        config=_config(day_of_month=31),
        completed_period_keys=set(),
    )

    assert due is not None
    assert due.scheduled_date == date(2026, 2, 28)


def test_biweekly_schedule_uses_fourteen_day_periods() -> None:
    config = _config(
        frequency="biweekly",
        biweekly_anchor_date=date(2026, 1, 2),
    )

    due = due_dca_period(
        as_of=date(2026, 1, 19),
        config=config,
        completed_period_keys=set(),
    )

    assert due is not None
    assert due.period_key == "biweekly:2026-01-16"


def test_drawdown_scaled_contribution_increases_and_caps_amount() -> None:
    config = _config(
        sizing_mode="drawdown_scaled",
        drawdown_scale_factor=Decimal("2"),
        max_contribution_multiplier=Decimal("1.5"),
    )

    amount, multiplier = contribution_for_drawdown(
        config,
        drawdown_pct=Decimal("30"),
    )

    assert multiplier == Decimal("1.5")
    assert amount == Decimal("150.00")


def test_dca_plan_blocks_amount_above_annual_limit() -> None:
    plan = build_dca_plan(
        as_of=date(2026, 7, 1),
        config=_config(max_annual_contribution=Decimal("1200")),
        completed_period_keys=set(),
        annual_contributed=Decimal("1150"),
    )

    assert not plan.should_buy
    assert plan.blocked_reason == "max_annual_contribution"


def test_drawdown_sizing_can_run_without_caps() -> None:
    config = _config(
        sizing_mode="drawdown_scaled",
        drawdown_scale_factor=Decimal("12"),
        max_contribution_multiplier=None,
        max_annual_contribution=None,
    )
    config = replace(config, max_contribution_per_purchase=None)

    amount, multiplier = contribution_for_drawdown(
        config,
        drawdown_pct=Decimal("35"),
    )
    plan = build_dca_plan(
        as_of=date(2026, 7, 1),
        config=config,
        completed_period_keys=set(),
        annual_contributed=Decimal("999999"),
        drawdown_pct=Decimal("35"),
    )

    assert multiplier == Decimal("5.2")
    assert amount == Decimal("520.00")
    assert plan.should_buy


def _config(
    *,
    frequency: str = "monthly",
    day_of_month: int = 1,
    biweekly_anchor_date: date | None = None,
    sizing_mode: str = "fixed",
    drawdown_scale_factor: Decimal = Decimal("2"),
    max_contribution_multiplier: Decimal | None = Decimal("2"),
    max_annual_contribution: Decimal | None = Decimal("12000"),
) -> DcaStrategyConfig:
    return DcaStrategyConfig(
        name="dca_qqq",
        symbol="QQQ",
        frequency=frequency,
        base_contribution=Decimal("100"),
        day_of_month=day_of_month,
        biweekly_anchor_date=biweekly_anchor_date,
        sizing_mode=sizing_mode,
        drawdown_scale_factor=drawdown_scale_factor,
        max_contribution_multiplier=max_contribution_multiplier,
        max_contribution_per_purchase=Decimal("500"),
        max_annual_contribution=max_annual_contribution,
        allow_fractional_shares=True,
    )

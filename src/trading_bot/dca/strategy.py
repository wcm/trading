from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from trading_bot.config import AppConfig


@dataclass(frozen=True)
class DcaStrategyConfig:
    name: str
    symbol: str
    frequency: str
    base_contribution: Decimal
    day_of_month: int
    biweekly_anchor_date: date | None
    sizing_mode: str
    drawdown_scale_factor: Decimal
    max_contribution_multiplier: Decimal | None
    max_contribution_per_purchase: Decimal | None
    max_annual_contribution: Decimal | None
    drawdown_lookback_days: int = 365
    allow_fractional_shares: bool = True


@dataclass(frozen=True)
class DcaDuePeriod:
    period_key: str
    scheduled_date: date

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_key": self.period_key,
            "scheduled_date": self.scheduled_date.isoformat(),
        }


@dataclass(frozen=True)
class DcaPlan:
    due_period: DcaDuePeriod | None
    contribution_amount: Decimal | None
    contribution_multiplier: Decimal | None
    drawdown_pct: Decimal
    blocked_reason: str | None = None

    @property
    def should_buy(self) -> bool:
        return self.due_period is not None and self.blocked_reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_buy": self.should_buy,
            "due_period": self.due_period.to_dict() if self.due_period else None,
            "contribution_amount": _fmt_optional_decimal(self.contribution_amount),
            "contribution_multiplier": _fmt_optional_decimal(
                self.contribution_multiplier
            ),
            "drawdown_pct": _fmt_decimal(self.drawdown_pct),
            "blocked_reason": self.blocked_reason,
        }


def dca_config_from_app_config(config: AppConfig) -> DcaStrategyConfig:
    anchor_value = config.get("dca_strategy", "biweekly_anchor_date", default=None)
    return DcaStrategyConfig(
        name=str(config.get("dca_strategy", "name", default="dca_qqq")),
        symbol=str(config.get("dca_strategy", "symbol", default="QQQ")).upper(),
        frequency=str(
            config.get("dca_strategy", "frequency", default="monthly")
        ).lower(),
        base_contribution=Decimal(
            str(config.get("dca_strategy", "base_contribution", default=500))
        ),
        day_of_month=int(config.get("dca_strategy", "day_of_month", default=1)),
        biweekly_anchor_date=(
            date.fromisoformat(str(anchor_value)) if anchor_value else None
        ),
        sizing_mode=str(
            config.get("dca_sizing", "mode", default="fixed")
        ).lower(),
        drawdown_scale_factor=Decimal(
            str(config.get("dca_sizing", "drawdown_scale_factor", default=2))
        ),
        max_contribution_multiplier=_optional_decimal_config(
            config.get("dca_sizing", "max_contribution_multiplier", default=2)
        ),
        max_contribution_per_purchase=_optional_decimal_config(
            config.get(
                "dca_risk", "max_contribution_per_purchase", default=1_500
            )
        ),
        max_annual_contribution=_optional_decimal_config(
            config.get("dca_risk", "max_annual_contribution", default=12_000)
        ),
        drawdown_lookback_days=int(
            config.get("dca_sizing", "drawdown_lookback_days", default=365)
        ),
        allow_fractional_shares=bool(
            config.get("dca_strategy", "allow_fractional_shares", default=True)
        ),
    )


def validate_dca_config(config: DcaStrategyConfig) -> None:
    if config.frequency not in {"monthly", "biweekly"}:
        raise ValueError("DCA frequency must be monthly or biweekly")
    if config.frequency == "biweekly" and config.biweekly_anchor_date is None:
        raise ValueError("Biweekly DCA requires biweekly_anchor_date")
    if not 1 <= config.day_of_month <= 31:
        raise ValueError("DCA day_of_month must be between 1 and 31")
    if config.base_contribution <= 0:
        raise ValueError("DCA base_contribution must be positive")
    if config.sizing_mode not in {"fixed", "drawdown_scaled"}:
        raise ValueError("DCA sizing mode must be fixed or drawdown_scaled")
    if config.drawdown_scale_factor < 0:
        raise ValueError("DCA drawdown_scale_factor must not be negative")
    if config.drawdown_lookback_days <= 0:
        raise ValueError("DCA drawdown_lookback_days must be positive")
    if (
        config.max_contribution_multiplier is not None
        and config.max_contribution_multiplier < 1
    ):
        raise ValueError("DCA max_contribution_multiplier must be at least 1")
    if (
        config.max_contribution_per_purchase is not None
        and config.max_contribution_per_purchase <= 0
    ):
        raise ValueError("DCA max_contribution_per_purchase must be positive")
    if (
        config.max_annual_contribution is not None
        and config.max_annual_contribution <= 0
    ):
        raise ValueError("DCA max_annual_contribution must be positive")


def due_dca_period(
    *,
    as_of: date,
    config: DcaStrategyConfig,
    completed_period_keys: set[str],
) -> DcaDuePeriod | None:
    validate_dca_config(config)
    if config.frequency == "monthly":
        scheduled_day = min(
            config.day_of_month,
            monthrange(as_of.year, as_of.month)[1],
        )
        scheduled_date = date(as_of.year, as_of.month, scheduled_day)
        period_key = f"monthly:{as_of.year:04d}-{as_of.month:02d}"
    else:
        anchor = config.biweekly_anchor_date
        if anchor is None or as_of < anchor:
            return None
        interval = (as_of - anchor).days // 14
        scheduled_date = anchor + timedelta(days=interval * 14)
        period_key = f"biweekly:{scheduled_date.isoformat()}"

    if as_of < scheduled_date or period_key in completed_period_keys:
        return None
    return DcaDuePeriod(period_key=period_key, scheduled_date=scheduled_date)


def contribution_for_drawdown(
    config: DcaStrategyConfig,
    *,
    drawdown_pct: Decimal,
) -> tuple[Decimal, Decimal]:
    validate_dca_config(config)
    multiplier = Decimal("1")
    if config.sizing_mode == "drawdown_scaled":
        normalized_drawdown = max(Decimal("0"), drawdown_pct) / Decimal("100")
        multiplier += config.drawdown_scale_factor * normalized_drawdown
        if config.max_contribution_multiplier is not None:
            multiplier = min(multiplier, config.max_contribution_multiplier)
    amount = config.base_contribution * multiplier
    if config.max_contribution_per_purchase is not None:
        amount = min(amount, config.max_contribution_per_purchase)
    amount = amount.quantize(Decimal("0.01"))
    return amount, multiplier


def build_dca_plan(
    *,
    as_of: date,
    config: DcaStrategyConfig,
    completed_period_keys: set[str],
    annual_contributed: Decimal,
    drawdown_pct: Decimal = Decimal("0"),
) -> DcaPlan:
    due_period = due_dca_period(
        as_of=as_of,
        config=config,
        completed_period_keys=completed_period_keys,
    )
    if due_period is None:
        return DcaPlan(
            due_period=None,
            contribution_amount=None,
            contribution_multiplier=None,
            drawdown_pct=drawdown_pct,
        )

    amount, multiplier = contribution_for_drawdown(
        config,
        drawdown_pct=drawdown_pct,
    )
    blocked_reason = None
    if (
        config.max_annual_contribution is not None
        and annual_contributed + amount > config.max_annual_contribution
    ):
        blocked_reason = "max_annual_contribution"
    return DcaPlan(
        due_period=due_period,
        contribution_amount=amount,
        contribution_multiplier=multiplier,
        drawdown_pct=drawdown_pct,
        blocked_reason=blocked_reason,
    )


def _fmt_decimal(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):f}"


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    return _fmt_decimal(value) if value is not None else None


def _optional_decimal_config(value: Any) -> Decimal | None:
    if value is None or str(value).strip().lower() in {"off", "none", "null"}:
        return None
    return Decimal(str(value))

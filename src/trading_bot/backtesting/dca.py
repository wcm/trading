from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any

from trading_bot.backtesting.bars import PriceBar
from trading_bot.dca.strategy import DcaStrategyConfig, build_dca_plan, validate_dca_config


@dataclass(frozen=True)
class DcaBacktestPurchase:
    sequence: int
    period_key: str
    scheduled_date: str
    purchase_timestamp: str
    price: Decimal
    contribution_amount: Decimal
    contribution_multiplier: Decimal
    drawdown_pct: Decimal
    shares: Decimal
    cumulative_shares: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "period_key": self.period_key,
            "scheduled_date": self.scheduled_date,
            "purchase_timestamp": self.purchase_timestamp,
            "price": _fmt_decimal(self.price),
            "contribution_amount": _fmt_decimal(self.contribution_amount),
            "contribution_multiplier": _fmt_decimal(
                self.contribution_multiplier
            ),
            "drawdown_pct": _fmt_decimal(self.drawdown_pct),
            "shares": _fmt_decimal(self.shares, places="0.000001"),
            "cumulative_shares": _fmt_decimal(
                self.cumulative_shares,
                places="0.000001",
            ),
        }


@dataclass(frozen=True)
class DcaBacktestResult:
    config: DcaStrategyConfig
    start_timestamp: str
    end_timestamp: str
    bar_count: int
    metrics: dict[str, Any]
    purchases: list[DcaBacktestPurchase] = field(default_factory=list)
    blocked_periods: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": {
                "name": self.config.name,
                "symbol": self.config.symbol,
                "frequency": self.config.frequency,
                "base_contribution": _fmt_decimal(
                    self.config.base_contribution
                ),
                "day_of_month": self.config.day_of_month,
                "biweekly_anchor_date": (
                    self.config.biweekly_anchor_date.isoformat()
                    if self.config.biweekly_anchor_date
                    else None
                ),
                "sizing_mode": self.config.sizing_mode,
                "drawdown_scale_factor": _fmt_decimal(
                    self.config.drawdown_scale_factor
                ),
                "drawdown_lookback_days": self.config.drawdown_lookback_days,
                "max_contribution_multiplier": _fmt_optional_decimal(
                    self.config.max_contribution_multiplier
                ),
                "max_contribution_per_purchase": _fmt_optional_decimal(
                    self.config.max_contribution_per_purchase
                ),
                "max_annual_contribution": _fmt_optional_decimal(
                    self.config.max_annual_contribution
                ),
                "allow_fractional_shares": self.config.allow_fractional_shares,
            },
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "bar_count": self.bar_count,
            "metrics": self.metrics,
            "blocked_periods": list(self.blocked_periods),
            "purchases": [purchase.to_dict() for purchase in self.purchases],
        }


def run_dca_backtest(
    bars: list[PriceBar],
    config: DcaStrategyConfig,
) -> DcaBacktestResult:
    if not bars:
        raise ValueError("DCA backtest requires at least one price bar")
    validate_dca_config(config)

    completed_periods: set[str] = set()
    processed_periods: set[str] = set()
    annual_contributions: dict[int, Decimal] = {}
    purchases: list[DcaBacktestPurchase] = []
    blocked_periods: list[dict[str, str]] = []
    total_contributed = Decimal("0")
    total_shares = Decimal("0")
    peak_price = bars[0].close
    sizing_window: deque[PriceBar] = deque()
    max_underlying_drawdown = Decimal("0")
    deepest_purchase_drawdown = Decimal("0")
    portfolio_units = Decimal("0")
    peak_unit_value = Decimal("0")
    max_portfolio_drawdown = Decimal("0")
    max_portfolio_drawdown_date: str | None = None
    worst_unrealized_loss = Decimal("0")
    worst_unrealized_loss_pct = Decimal("0")
    worst_unrealized_loss_date: str | None = None

    for bar in bars:
        peak_price = max(peak_price, bar.close)
        underlying_drawdown_pct = (
            ((peak_price - bar.close) / peak_price) * Decimal("100")
            if peak_price > 0
            else Decimal("0")
        )
        max_underlying_drawdown = max(
            max_underlying_drawdown,
            underlying_drawdown_pct,
        )
        current_unit_value: Decimal | None = None
        if portfolio_units > 0:
            portfolio_value = total_shares * bar.close
            current_unit_value = portfolio_value / portfolio_units
            peak_unit_value = max(peak_unit_value, current_unit_value)
            portfolio_drawdown = (
                (peak_unit_value - current_unit_value) / peak_unit_value
                * Decimal("100")
                if peak_unit_value > 0
                else Decimal("0")
            )
            if portfolio_drawdown > max_portfolio_drawdown:
                max_portfolio_drawdown = portfolio_drawdown
                max_portfolio_drawdown_date = bar.day.isoformat()
            unrealized_loss = portfolio_value - total_contributed
            unrealized_loss_pct = (
                unrealized_loss / total_contributed * Decimal("100")
                if total_contributed > 0
                else Decimal("0")
            )
            if unrealized_loss < worst_unrealized_loss:
                worst_unrealized_loss = unrealized_loss
                worst_unrealized_loss_pct = unrealized_loss_pct
                worst_unrealized_loss_date = bar.day.isoformat()
        sizing_window.append(bar)
        cutoff = bar.day - timedelta(days=config.drawdown_lookback_days)
        while sizing_window and sizing_window[0].day < cutoff:
            sizing_window.popleft()
        sizing_peak = max(item.close for item in sizing_window)
        sizing_drawdown_pct = (
            ((sizing_peak - bar.close) / sizing_peak) * Decimal("100")
            if sizing_peak > 0
            else Decimal("0")
        )
        annual_contributed = annual_contributions.get(bar.day.year, Decimal("0"))
        plan = build_dca_plan(
            as_of=bar.day,
            config=config,
            completed_period_keys=completed_periods | processed_periods,
            annual_contributed=annual_contributed,
            drawdown_pct=sizing_drawdown_pct,
        )
        if plan.due_period is None:
            continue
        if plan.blocked_reason:
            processed_periods.add(plan.due_period.period_key)
            blocked_periods.append(
                {
                    "period_key": plan.due_period.period_key,
                    "scheduled_date": plan.due_period.scheduled_date.isoformat(),
                    "reason": plan.blocked_reason,
                }
            )
            continue

        amount = plan.contribution_amount or Decimal("0")
        multiplier = plan.contribution_multiplier or Decimal("1")
        shares = _shares_for_contribution(
            amount,
            bar.close,
            allow_fractional=config.allow_fractional_shares,
        )
        if shares <= 0:
            processed_periods.add(plan.due_period.period_key)
            blocked_periods.append(
                {
                    "period_key": plan.due_period.period_key,
                    "scheduled_date": plan.due_period.scheduled_date.isoformat(),
                    "reason": "contribution_too_small_for_one_share",
                }
            )
            continue

        actual_contribution = (
            amount if config.allow_fractional_shares else shares * bar.close
        )
        issue_unit_value = current_unit_value or Decimal("1")
        portfolio_units += actual_contribution / issue_unit_value
        if peak_unit_value <= 0:
            peak_unit_value = issue_unit_value
        total_contributed += actual_contribution
        total_shares += shares
        annual_contributions[bar.day.year] = annual_contributed + actual_contribution
        completed_periods.add(plan.due_period.period_key)
        deepest_purchase_drawdown = max(
            deepest_purchase_drawdown,
            sizing_drawdown_pct,
        )
        purchases.append(
            DcaBacktestPurchase(
                sequence=len(purchases) + 1,
                period_key=plan.due_period.period_key,
                scheduled_date=plan.due_period.scheduled_date.isoformat(),
                purchase_timestamp=bar.timestamp,
                price=bar.close,
                contribution_amount=actual_contribution,
                contribution_multiplier=multiplier,
                drawdown_pct=sizing_drawdown_pct,
                shares=shares,
                cumulative_shares=total_shares,
            )
        )

    final_price = bars[-1].close
    final_value = total_shares * final_price
    investment_gain = final_value - total_contributed
    simple_return_pct = (
        investment_gain / total_contributed * Decimal("100")
        if total_contributed > 0
        else Decimal("0")
    )
    average_cost = (
        total_contributed / total_shares if total_shares > 0 else Decimal("0")
    )
    underlying_return_pct = (
        (final_price / bars[0].close - Decimal("1")) * Decimal("100")
        if bars[0].close > 0
        else Decimal("0")
    )
    amounts = [purchase.contribution_amount for purchase in purchases]
    metrics = {
        "symbol": config.symbol,
        "start_date": bars[0].day.isoformat(),
        "end_date": bars[-1].day.isoformat(),
        "start_price": _fmt_decimal(bars[0].close),
        "final_price": _fmt_decimal(final_price),
        "underlying_return_pct": _fmt_decimal(underlying_return_pct),
        "underlying_max_drawdown_pct": _fmt_decimal(max_underlying_drawdown),
        "portfolio_max_drawdown_pct": _fmt_decimal(max_portfolio_drawdown),
        "portfolio_max_drawdown_date": max_portfolio_drawdown_date,
        "worst_unrealized_loss": _fmt_decimal(worst_unrealized_loss),
        "worst_unrealized_loss_pct": _fmt_decimal(worst_unrealized_loss_pct),
        "worst_unrealized_loss_date": worst_unrealized_loss_date,
        "contribution_count": len(purchases),
        "blocked_period_count": len(blocked_periods),
        "total_contributed": _fmt_decimal(total_contributed),
        "total_shares": _fmt_decimal(total_shares, places="0.000001"),
        "average_cost": _fmt_decimal(average_cost),
        "final_value": _fmt_decimal(final_value),
        "investment_gain": _fmt_decimal(investment_gain),
        "simple_return_pct": _fmt_decimal(simple_return_pct),
        "average_contribution": _fmt_decimal(
            sum(amounts, Decimal("0")) / len(amounts)
            if amounts
            else Decimal("0")
        ),
        "largest_contribution": _fmt_decimal(
            max(amounts) if amounts else Decimal("0")
        ),
        "deepest_purchase_drawdown_pct": _fmt_decimal(
            deepest_purchase_drawdown
        ),
    }
    return DcaBacktestResult(
        config=config,
        start_timestamp=bars[0].timestamp,
        end_timestamp=bars[-1].timestamp,
        bar_count=len(bars),
        metrics=metrics,
        purchases=purchases,
        blocked_periods=blocked_periods,
    )


def _shares_for_contribution(
    amount: Decimal,
    price: Decimal,
    *,
    allow_fractional: bool,
) -> Decimal:
    if amount <= 0 or price <= 0:
        return Decimal("0")
    shares = amount / price
    if allow_fractional:
        return shares
    return shares.to_integral_value(rounding=ROUND_DOWN)


def _fmt_decimal(value: Decimal, *, places: str = "0.01") -> str:
    return f"{value.quantize(Decimal(places)):f}"


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    return _fmt_decimal(value) if value is not None else None

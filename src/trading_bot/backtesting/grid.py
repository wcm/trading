from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_DOWN
from typing import Any

from trading_bot.backtesting.bars import PriceBar


@dataclass(frozen=True)
class GridBacktestConfig:
    symbol: str
    starting_cash: Decimal
    grid_spacing_pct: Decimal
    base_order_notional: Decimal
    max_buy_levels_below_anchor: int
    max_inventory_value: Decimal
    cash_reserve: Decimal
    max_unrealized_loss: Decimal | None = None
    pause_new_buys_after_consecutive_down_levels: int | None = None
    recenter_up_pct: Decimal | None = None
    recenter_confirmation_bars: int = 1
    adaptive_sizing_enabled: bool = False
    adaptive_scale_factor: Decimal = Decimal("0")
    adaptive_max_order_multiplier: Decimal = Decimal("1")
    max_single_order_notional: Decimal | None = None
    allow_fractional_shares: bool = False


@dataclass(frozen=True)
class GridTrade:
    trade_id: int
    side: str
    timestamp: str
    level_index: int
    price: Decimal
    shares: Decimal
    notional: Decimal
    realized_pnl: Decimal | None = None
    paired_buy_trade_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "trade_id": self.trade_id,
            "side": self.side,
            "timestamp": self.timestamp,
            "level_index": self.level_index,
            "price": _fmt_decimal(self.price),
            "shares": _fmt_decimal(self.shares),
            "notional": _fmt_decimal(self.notional),
            "realized_pnl": _fmt_optional_decimal(self.realized_pnl),
            "paired_buy_trade_id": self.paired_buy_trade_id,
        }
        return data


@dataclass
class GridLot:
    buy_trade_id: int
    level_index: int
    buy_timestamp: str
    buy_day: date
    buy_price: Decimal
    shares: Decimal
    cost: Decimal
    sell_target: Decimal

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        return (mark_price - self.buy_price) * self.shares

    def market_value(self, mark_price: Decimal) -> Decimal:
        return mark_price * self.shares

    def to_dict(self, *, mark_price: Decimal | None = None, end_day: date | None = None) -> dict[str, Any]:
        data = {
            "buy_trade_id": self.buy_trade_id,
            "level_index": self.level_index,
            "buy_timestamp": self.buy_timestamp,
            "buy_price": _fmt_decimal(self.buy_price),
            "shares": _fmt_decimal(self.shares),
            "cost": _fmt_decimal(self.cost),
            "sell_target": _fmt_decimal(self.sell_target),
        }
        if mark_price is not None:
            data["market_value"] = _fmt_decimal(self.market_value(mark_price))
            data["unrealized_pnl"] = _fmt_decimal(self.unrealized_pnl(mark_price))
        if end_day is not None:
            data["holding_days"] = max(0, (end_day - self.buy_day).days)
        return data


@dataclass(frozen=True)
class GridBacktestResult:
    config: GridBacktestConfig
    start_timestamp: str
    end_timestamp: str
    bar_count: int
    metrics: dict[str, Any]
    trades: list[GridTrade] = field(default_factory=list)
    open_lots: list[GridLot] = field(default_factory=list)
    risk_block_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        mark_price = Decimal(str(self.metrics["final_price"]))
        end_day = date.fromisoformat(str(self.metrics["end_date"]))
        return {
            "config": {
                "symbol": self.config.symbol,
                "starting_cash": _fmt_decimal(self.config.starting_cash),
                "grid_spacing_pct": _fmt_decimal(self.config.grid_spacing_pct),
                "base_order_notional": _fmt_decimal(self.config.base_order_notional),
                "max_buy_levels_below_anchor": self.config.max_buy_levels_below_anchor,
                "max_inventory_value": _fmt_decimal(self.config.max_inventory_value),
                "cash_reserve": _fmt_decimal(self.config.cash_reserve),
                "max_unrealized_loss": _fmt_optional_decimal(self.config.max_unrealized_loss),
                "pause_new_buys_after_consecutive_down_levels": (
                    self.config.pause_new_buys_after_consecutive_down_levels
                ),
                "recenter_up_pct": _fmt_optional_decimal(self.config.recenter_up_pct),
                "recenter_confirmation_bars": self.config.recenter_confirmation_bars,
                "adaptive_sizing_enabled": self.config.adaptive_sizing_enabled,
                "adaptive_scale_factor": _fmt_decimal(self.config.adaptive_scale_factor),
                "adaptive_max_order_multiplier": _fmt_decimal(
                    self.config.adaptive_max_order_multiplier
                ),
                "max_single_order_notional": _fmt_optional_decimal(
                    self.config.max_single_order_notional
                ),
                "allow_fractional_shares": self.config.allow_fractional_shares,
            },
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "bar_count": self.bar_count,
            "metrics": self.metrics,
            "risk_block_counts": self.risk_block_counts,
            "trades": [trade.to_dict() for trade in self.trades],
            "open_lots": [
                lot.to_dict(mark_price=mark_price, end_day=end_day)
                for lot in self.open_lots
            ],
        }


def run_grid_backtest(bars: list[PriceBar], config: GridBacktestConfig) -> GridBacktestResult:
    if not bars:
        raise ValueError("Grid backtest requires at least one price bar")
    if config.grid_spacing_pct <= 0:
        raise ValueError("grid_spacing_pct must be positive")
    if config.base_order_notional <= 0:
        raise ValueError("base_order_notional must be positive")
    if config.max_buy_levels_below_anchor <= 0:
        raise ValueError("max_buy_levels_below_anchor must be positive")
    if config.recenter_confirmation_bars <= 0:
        raise ValueError("recenter_confirmation_bars must be positive")

    spacing = config.grid_spacing_pct / Decimal("100")
    cash = config.starting_cash
    realized_pnl = Decimal("0")
    anchor: Decimal | None = None
    open_lots: list[GridLot] = []
    trades: list[GridTrade] = []
    risk_block_counts: dict[str, int] = {}
    paused_days: set[str] = set()
    trade_days: set[str] = set()
    next_trade_id = 1

    peak_equity = config.starting_cash
    max_drawdown = Decimal("0")
    worst_unrealized = Decimal("0")
    max_shares = Decimal("0")
    max_inventory_value = Decimal("0")
    max_cash_used = Decimal("0")
    longest_holding_days = 0
    recenter_count = 0
    recenter_confirmation_count = 0

    for bar in bars:
        if anchor is None:
            anchor = bar.open

        lots_to_keep: list[GridLot] = []
        sold_count = 0
        for lot in open_lots:
            if bar.high >= lot.sell_target:
                notional = lot.sell_target * lot.shares
                pnl = (lot.sell_target - lot.buy_price) * lot.shares
                cash += notional
                realized_pnl += pnl
                trades.append(
                    GridTrade(
                        trade_id=next_trade_id,
                        side="sell",
                        timestamp=bar.timestamp,
                        level_index=lot.level_index,
                        price=lot.sell_target,
                        shares=lot.shares,
                        notional=notional,
                        realized_pnl=pnl,
                        paired_buy_trade_id=lot.buy_trade_id,
                    )
                )
                next_trade_id += 1
                sold_count += 1
                trade_days.add(bar.day.isoformat())
                longest_holding_days = max(longest_holding_days, (bar.day - lot.buy_day).days)
            else:
                lots_to_keep.append(lot)
        open_lots = lots_to_keep

        if sold_count > 0:
            recenter_confirmation_count = 0
            (
                peak_equity,
                max_drawdown,
                worst_unrealized,
                max_shares,
                max_inventory_value,
                max_cash_used,
            ) = _mark_bar_metrics_values(
                bar=bar,
                cash=cash,
                open_lots=open_lots,
                starting_cash=config.starting_cash,
                peak_equity=peak_equity,
                max_drawdown=max_drawdown,
                worst_unrealized=worst_unrealized,
                max_shares=max_shares,
                max_inventory_value=max_inventory_value,
                max_cash_used=max_cash_used,
            )
            continue

        if open_lots:
            recenter_confirmation_count = 0
        elif _should_recenter_up(anchor, bar.close, config.recenter_up_pct):
            recenter_confirmation_count += 1
            if recenter_confirmation_count >= config.recenter_confirmation_bars:
                anchor = bar.close
                recenter_count += 1
                recenter_confirmation_count = 0
                (
                    peak_equity,
                    max_drawdown,
                    worst_unrealized,
                    max_shares,
                    max_inventory_value,
                    max_cash_used,
                ) = _mark_bar_metrics_values(
                    bar=bar,
                    cash=cash,
                    open_lots=open_lots,
                    starting_cash=config.starting_cash,
                    peak_equity=peak_equity,
                    max_drawdown=max_drawdown,
                    worst_unrealized=worst_unrealized,
                    max_shares=max_shares,
                    max_inventory_value=max_inventory_value,
                    max_cash_used=max_cash_used,
                )
                continue
        else:
            recenter_confirmation_count = 0

        open_level_indexes = {lot.level_index for lot in open_lots}
        for level_index in range(1, config.max_buy_levels_below_anchor + 1):
            if level_index in open_level_indexes:
                continue
            buy_price = _round_price(anchor * ((Decimal("1") - spacing) ** level_index))
            if bar.low > buy_price:
                continue

            order_notional = _order_notional_for_buy(config=config, anchor=anchor, buy_price=buy_price)
            shares = _shares_for_order(
                order_notional,
                buy_price,
                allow_fractional=config.allow_fractional_shares,
            )
            if shares <= 0:
                _count_block(risk_block_counts, "order_too_small")
                paused_days.add(bar.day.isoformat())
                continue
            cost = buy_price * shares
            block_reason = _buy_block_reason(
                config=config,
                cash=cash,
                cost=cost,
                open_lots=open_lots,
                mark_price=bar.close,
            )
            if block_reason:
                _count_block(risk_block_counts, block_reason)
                paused_days.add(bar.day.isoformat())
                continue

            cash -= cost
            sell_target = _round_price(buy_price * (Decimal("1") + spacing))
            lot = GridLot(
                buy_trade_id=next_trade_id,
                level_index=level_index,
                buy_timestamp=bar.timestamp,
                buy_day=bar.day,
                buy_price=buy_price,
                shares=shares,
                cost=cost,
                sell_target=sell_target,
            )
            open_lots.append(lot)
            open_level_indexes.add(level_index)
            trades.append(
                GridTrade(
                    trade_id=next_trade_id,
                    side="buy",
                    timestamp=bar.timestamp,
                    level_index=level_index,
                    price=buy_price,
                    shares=shares,
                    notional=cost,
                )
            )
            next_trade_id += 1
            trade_days.add(bar.day.isoformat())

        (
            peak_equity,
            max_drawdown,
            worst_unrealized,
            max_shares,
            max_inventory_value,
            max_cash_used,
        ) = _mark_bar_metrics_values(
            bar=bar,
            cash=cash,
            open_lots=open_lots,
            starting_cash=config.starting_cash,
            peak_equity=peak_equity,
            max_drawdown=max_drawdown,
            worst_unrealized=worst_unrealized,
            max_shares=max_shares,
            max_inventory_value=max_inventory_value,
            max_cash_used=max_cash_used,
        )

    final_bar = bars[-1]
    final_inventory_value = _inventory_value(open_lots, final_bar.close)
    final_unrealized = _unrealized_pnl(open_lots, final_bar.close)
    final_equity = cash + final_inventory_value
    for lot in open_lots:
        longest_holding_days = max(longest_holding_days, (final_bar.day - lot.buy_day).days)

    buy_count = sum(1 for trade in trades if trade.side == "buy")
    sell_count = sum(1 for trade in trades if trade.side == "sell")
    metrics = {
        "symbol": config.symbol,
        "start_date": bars[0].day.isoformat(),
        "end_date": final_bar.day.isoformat(),
        "start_price": _fmt_decimal(bars[0].open),
        "final_price": _fmt_decimal(final_bar.close),
        "starting_cash": _fmt_decimal(config.starting_cash),
        "final_cash": _fmt_decimal(cash),
        "final_equity": _fmt_decimal(final_equity),
        "realized_pnl": _fmt_decimal(realized_pnl),
        "unrealized_pnl": _fmt_decimal(final_unrealized),
        "total_pnl": _fmt_decimal(final_equity - config.starting_cash),
        "total_return_pct": _fmt_decimal(((final_equity / config.starting_cash) - Decimal("1")) * Decimal("100")),
        "max_drawdown_pct": _fmt_decimal(max_drawdown * Decimal("100")),
        "worst_unrealized_loss": _fmt_decimal(worst_unrealized),
        "max_shares_held": _fmt_decimal(max_shares),
        "max_inventory_value": _fmt_decimal(max_inventory_value),
        "max_cash_used": _fmt_decimal(max_cash_used),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "trade_count": len(trades),
        "average_trades_per_day": _fmt_decimal(Decimal(str(len(trades))) / Decimal(str(len(bars)))),
        "days_with_trade": len(trade_days),
        "longest_holding_days": longest_holding_days,
        "paused_days": len(paused_days),
        "recenter_count": recenter_count,
        "open_lot_count": len(open_lots),
        "open_shares": _fmt_decimal(_total_shares(open_lots)),
    }
    return GridBacktestResult(
        config=config,
        start_timestamp=bars[0].timestamp,
        end_timestamp=final_bar.timestamp,
        bar_count=len(bars),
        metrics=metrics,
        trades=trades,
        open_lots=open_lots,
        risk_block_counts=risk_block_counts,
    )


def result_summary_row(result: GridBacktestResult) -> dict[str, Any]:
    metrics = result.metrics
    return {
        "spacing_pct": _fmt_decimal(result.config.grid_spacing_pct),
        "order_notional": _fmt_decimal(result.config.base_order_notional),
        "max_inventory_limit": _fmt_decimal(result.config.max_inventory_value),
        "adaptive_scale_factor": (
            _fmt_decimal(result.config.adaptive_scale_factor)
            if result.config.adaptive_sizing_enabled
            else "0.00"
        ),
        "adaptive_max_multiplier": _fmt_decimal(result.config.adaptive_max_order_multiplier),
        "total_pnl": metrics["total_pnl"],
        "total_return_pct": metrics["total_return_pct"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "worst_unrealized_loss": metrics["worst_unrealized_loss"],
        "buys": metrics["buy_count"],
        "sells": metrics["sell_count"],
        "max_inventory_value": metrics["max_inventory_value"],
        "max_cash_used": metrics["max_cash_used"],
        "paused_days": metrics["paused_days"],
        "recenters": metrics["recenter_count"],
        "open_lots": metrics["open_lot_count"],
        "longest_holding_days": metrics["longest_holding_days"],
    }


def _buy_block_reason(
    *,
    config: GridBacktestConfig,
    cash: Decimal,
    cost: Decimal,
    open_lots: list[GridLot],
    mark_price: Decimal,
) -> str | None:
    if cash - cost < config.cash_reserve:
        return "cash_reserve"
    inventory_cost = sum((lot.cost for lot in open_lots), Decimal("0"))
    if inventory_cost + cost > config.max_inventory_value:
        return "max_inventory_value"
    if config.max_unrealized_loss is not None:
        unrealized = _unrealized_pnl(open_lots, mark_price)
        if unrealized <= -abs(config.max_unrealized_loss):
            return "max_unrealized_loss"
    if (
        config.pause_new_buys_after_consecutive_down_levels is not None
        and len(open_lots) >= config.pause_new_buys_after_consecutive_down_levels
    ):
        return "consecutive_down_levels"
    return None


def _order_notional_for_buy(
    *,
    config: GridBacktestConfig,
    anchor: Decimal,
    buy_price: Decimal,
) -> Decimal:
    if not config.adaptive_sizing_enabled or config.adaptive_scale_factor <= 0 or anchor <= 0:
        return config.base_order_notional
    drop_pct = max(Decimal("0"), (anchor - buy_price) / anchor)
    multiplier = Decimal("1") + config.adaptive_scale_factor * drop_pct
    if config.adaptive_max_order_multiplier > 0:
        multiplier = min(multiplier, config.adaptive_max_order_multiplier)
    notional = config.base_order_notional * multiplier
    if config.max_single_order_notional is not None:
        notional = min(notional, config.max_single_order_notional)
    return notional


def _should_recenter_up(anchor: Decimal, close: Decimal, recenter_up_pct: Decimal | None) -> bool:
    if recenter_up_pct is None or recenter_up_pct <= 0:
        return False
    return close >= anchor * (Decimal("1") + recenter_up_pct / Decimal("100"))


def _shares_for_order(notional: Decimal, price: Decimal, *, allow_fractional: bool) -> Decimal:
    if price <= 0:
        return Decimal("0")
    raw_shares = notional / price
    if allow_fractional:
        return raw_shares.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return raw_shares.quantize(Decimal("1"), rounding=ROUND_DOWN)


def _inventory_value(open_lots: list[GridLot], mark_price: Decimal) -> Decimal:
    return sum((lot.market_value(mark_price) for lot in open_lots), Decimal("0"))


def _unrealized_pnl(open_lots: list[GridLot], mark_price: Decimal) -> Decimal:
    return sum((lot.unrealized_pnl(mark_price) for lot in open_lots), Decimal("0"))


def _total_shares(open_lots: list[GridLot]) -> Decimal:
    return sum((lot.shares for lot in open_lots), Decimal("0"))


def _count_block(counts: dict[str, int], reason: str) -> None:
    counts[reason] = counts.get(reason, 0) + 1


def _mark_bar_metrics_values(
    *,
    bar: PriceBar,
    cash: Decimal,
    open_lots: list[GridLot],
    starting_cash: Decimal,
    peak_equity: Decimal,
    max_drawdown: Decimal,
    worst_unrealized: Decimal,
    max_shares: Decimal,
    max_inventory_value: Decimal,
    max_cash_used: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    inventory_value = _inventory_value(open_lots, bar.close)
    equity = cash + inventory_value
    peak_equity = max(peak_equity, equity)
    if peak_equity > 0:
        max_drawdown = min(max_drawdown, (equity / peak_equity) - Decimal("1"))
    worst_unrealized = min(worst_unrealized, _unrealized_pnl(open_lots, bar.close))
    max_shares = max(max_shares, _total_shares(open_lots))
    max_inventory_value = max(max_inventory_value, inventory_value)
    max_cash_used = max(max_cash_used, starting_cash - cash)
    return peak_equity, max_drawdown, worst_unrealized, max_shares, max_inventory_value, max_cash_used


def _round_price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _fmt_decimal(value)


def _fmt_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")

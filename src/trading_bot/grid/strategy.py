from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from trading_bot.backtesting.bars import PriceBar
from trading_bot.config import AppConfig
from trading_bot.grid.state import GridLotState, GridState


@dataclass(frozen=True)
class GridStrategyConfig:
    name: str
    symbol: str
    strategy_capital: Decimal
    grid_spacing_pct: Decimal
    base_order_notional: Decimal
    max_buy_levels_below_anchor: int
    max_inventory_value: Decimal
    cash_reserve: Decimal
    max_unrealized_loss: Decimal | None
    pause_new_buys_after_consecutive_down_levels: int | None
    recenter_up_pct: Decimal | None
    adaptive_sizing_enabled: bool
    adaptive_scale_factor: Decimal
    adaptive_max_order_multiplier: Decimal
    max_single_order_notional: Decimal | None
    allow_fractional_shares: bool = False


@dataclass(frozen=True)
class GridIntent:
    action: str
    level_index: int
    price: Decimal
    qty: Decimal
    notional: Decimal
    sell_target: Decimal | None = None
    lot_id: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "level_index": self.level_index,
            "price": _fmt_decimal(self.price),
            "qty": _fmt_decimal(self.qty),
            "notional": _fmt_decimal(self.notional),
            "sell_target": _fmt_optional_decimal(self.sell_target),
            "lot_id": self.lot_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GridPlan:
    intents: list[GridIntent]
    blocked: list[dict[str, Any]]
    events: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intents": [intent.to_dict() for intent in self.intents],
            "blocked": list(self.blocked),
            "events": list(self.events),
        }


def grid_config_from_app_config(config: AppConfig) -> GridStrategyConfig:
    return GridStrategyConfig(
        name=str(config.get("grid_strategy", "name", default="grid_tqqq")),
        symbol=str(config.get("grid_strategy", "symbol", default="TQQQ")).upper(),
        strategy_capital=Decimal(str(config.get("grid_risk", "strategy_capital", default=10_000))),
        grid_spacing_pct=Decimal(str(config.get("grid_strategy", "grid_spacing_pct", default=3))),
        base_order_notional=Decimal(str(config.get("grid_strategy", "base_order_notional", default=400))),
        max_buy_levels_below_anchor=int(
            config.get("grid_strategy", "max_buy_levels_below_anchor", default=16)
        ),
        max_inventory_value=Decimal(str(config.get("grid_risk", "max_inventory_value", default=8_000))),
        cash_reserve=Decimal(str(config.get("grid_risk", "cash_reserve", default=2_000))),
        max_unrealized_loss=_optional_decimal(
            config.get("grid_risk", "max_unrealized_loss", default=1_200)
        ),
        pause_new_buys_after_consecutive_down_levels=_optional_int(
            config.get("grid_risk", "pause_new_buys_after_consecutive_down_levels", default=5)
        ),
        recenter_up_pct=_optional_decimal(config.get("grid_strategy", "recenter_up_pct", default=5)),
        adaptive_sizing_enabled=bool(config.get("adaptive_sizing", "enabled", default=False)),
        adaptive_scale_factor=Decimal(str(config.get("adaptive_sizing", "scale_factor", default=0))),
        adaptive_max_order_multiplier=Decimal(
            str(config.get("adaptive_sizing", "max_order_multiplier", default=1))
        ),
        max_single_order_notional=_optional_decimal(
            config.get("adaptive_sizing", "max_single_order_notional", default=None)
        ),
        allow_fractional_shares=bool(config.get("grid_strategy", "allow_fractional_shares", default=False)),
    )


def build_grid_plan(state: GridState, config: GridStrategyConfig, bar: PriceBar) -> GridPlan:
    if config.grid_spacing_pct <= 0:
        raise ValueError("grid_spacing_pct must be positive")
    if config.base_order_notional <= 0:
        raise ValueError("base_order_notional must be positive")
    if config.max_buy_levels_below_anchor <= 0:
        raise ValueError("max_buy_levels_below_anchor must be positive")

    events: list[str] = []
    blocked: list[dict[str, Any]] = []
    if state.anchor_price is None:
        state.anchor_price = _round_price(bar.close)
        events.append(f"Initialized anchor at {state.anchor_price}")

    anchor = state.anchor_price
    if not state.active_lots() and _should_recenter_up(anchor, bar.close, config.recenter_up_pct):
        state.anchor_price = _round_price(bar.close)
        state.recenter_count += 1
        anchor = state.anchor_price
        events.append(f"Recentered anchor upward to {state.anchor_price}")

    intents: list[GridIntent] = []
    for lot in state.open_inventory_lots():
        if lot.status != "open":
            continue
        if bar.high < lot.sell_target or lot.qty is None:
            continue
        intents.append(
            GridIntent(
                action="sell",
                level_index=lot.level_index,
                price=lot.sell_target,
                qty=lot.qty,
                notional=lot.sell_target * lot.qty,
                lot_id=lot.lot_id,
                reason=f"bar high {bar.high} reached sell target {lot.sell_target}",
            )
        )

    if intents:
        events.append("Sell intent found; new buys are skipped for this cycle")
        return GridPlan(intents=intents, blocked=blocked, events=events)

    active_levels = state.active_level_indexes()
    active_lots = state.active_lots()
    active_cost = sum((lot.cost_basis() for lot in active_lots), Decimal("0"))
    unrealized = sum((lot.unrealized_pnl(bar.close) for lot in state.open_inventory_lots()), Decimal("0"))
    spacing = config.grid_spacing_pct / Decimal("100")

    for level_index in range(1, config.max_buy_levels_below_anchor + 1):
        if level_index in active_levels:
            continue
        buy_price = buy_price_for_level(anchor, spacing, level_index)
        if bar.low > buy_price:
            continue

        notional = order_notional_for_buy(config=config, anchor=anchor, buy_price=buy_price)
        qty = shares_for_order(notional, buy_price, allow_fractional=config.allow_fractional_shares)
        cost = buy_price * qty
        block_reason = buy_block_reason(
            config=config,
            active_cost=active_cost,
            cost=cost,
            unrealized=unrealized,
            active_down_levels=len(active_levels),
            qty=qty,
        )
        if block_reason:
            blocked.append(
                {
                    "level_index": level_index,
                    "buy_price": _fmt_decimal(buy_price),
                    "reason": block_reason,
                }
            )
            continue

        sell_target = _round_price(buy_price * (Decimal("1") + spacing))
        intents.append(
            GridIntent(
                action="buy",
                level_index=level_index,
                price=buy_price,
                qty=qty,
                notional=cost,
                sell_target=sell_target,
                reason=f"bar low {bar.low} reached buy level {buy_price}",
            )
        )
        active_levels.add(level_index)
        active_cost += cost

    return GridPlan(intents=intents, blocked=blocked, events=events)


def buy_price_for_level(anchor: Decimal, spacing: Decimal, level_index: int) -> Decimal:
    return _round_price(anchor * ((Decimal("1") - spacing) ** level_index))


def order_notional_for_buy(
    *,
    config: GridStrategyConfig,
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


def shares_for_order(notional: Decimal, price: Decimal, *, allow_fractional: bool) -> Decimal:
    if price <= 0:
        return Decimal("0")
    raw_shares = notional / price
    if allow_fractional:
        return raw_shares.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return raw_shares.quantize(Decimal("1"), rounding=ROUND_DOWN)


def buy_block_reason(
    *,
    config: GridStrategyConfig,
    active_cost: Decimal,
    cost: Decimal,
    unrealized: Decimal,
    active_down_levels: int,
    qty: Decimal,
) -> str | None:
    if qty <= 0:
        return "order_too_small"
    if config.strategy_capital - active_cost - cost < config.cash_reserve:
        return "cash_reserve"
    if active_cost + cost > config.max_inventory_value:
        return "max_inventory_value"
    if config.max_unrealized_loss is not None and unrealized <= -abs(config.max_unrealized_loss):
        return "max_unrealized_loss"
    if (
        config.pause_new_buys_after_consecutive_down_levels is not None
        and active_down_levels >= config.pause_new_buys_after_consecutive_down_levels
    ):
        return "consecutive_down_levels"
    return None


def lot_from_buy_intent(
    intent: GridIntent,
    *,
    lot_id: str,
    created_at: str,
    status: str,
    buy_order_id: str | None = None,
    buy_client_order_id: str | None = None,
) -> GridLotState:
    return GridLotState(
        lot_id=lot_id,
        level_index=intent.level_index,
        buy_price=intent.price,
        sell_target=intent.sell_target or intent.price,
        planned_notional=intent.notional,
        status=status,
        qty=None,
        created_at=created_at,
        buy_order_id=buy_order_id,
        buy_client_order_id=buy_client_order_id,
        buy_submitted_at=created_at if buy_order_id else None,
    )


def _should_recenter_up(anchor: Decimal, close: Decimal, recenter_up_pct: Decimal | None) -> bool:
    if recenter_up_pct is None or recenter_up_pct <= 0:
        return False
    return close >= anchor * (Decimal("1") + recenter_up_pct / Decimal("100"))


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip().lower() in {"", "none", "null", "off"}:
        return None
    return Decimal(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip().lower() in {"", "none", "null", "off"}:
        return None
    return int(value)


def _round_price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _fmt_decimal(value)


def _fmt_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")

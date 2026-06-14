from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.brokers.alpaca import AlpacaClient
from trading_bot.config import AppConfig
from trading_bot.execution.orders import build_client_order_id, build_put_credit_spread_close_preview
from trading_bot.utils.mapping import nested_get as _nested_get
from trading_bot.utils.money import decimal_or_none as _decimal_or_none


EASTERN = ZoneInfo("America/New_York")
OCC_SYMBOL_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True)
class ParsedOptionSymbol:
    symbol: str
    underlying_symbol: str
    expiration_date: date
    option_type: str
    strike: Decimal


@dataclass(frozen=True)
class OptionPositionLeg:
    symbol: str
    underlying_symbol: str
    expiration_date: date
    option_type: str
    strike: Decimal
    side: str
    quantity: int
    average_entry_price: Decimal | None
    raw_position: dict[str, Any]


@dataclass(frozen=True)
class PositionMonitorResult:
    generated_at: str
    option_position_count: int
    spread_count: int
    spreads: list[dict[str, Any]]
    unpaired_legs: list[dict[str, Any]]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def monitor_put_credit_spreads(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    positions: list[dict[str, Any]] | None = None,
    option_feed: str | None = None,
) -> PositionMonitorResult:
    now = datetime.now(UTC)
    market_date = now.astimezone(EASTERN).date()
    positions = positions if positions is not None else alpaca.get_positions()
    option_feed = option_feed or str(config.get("alpaca", "option_data_feed", default="indicative"))
    stock_feed = str(config.get("alpaca", "stock_data_feed", default="iex"))

    warnings: list[str] = []
    option_legs = [_position_to_option_leg(position) for position in positions]
    option_legs = [leg for leg in option_legs if leg is not None]
    spread_pairs, unpaired_legs = _pair_put_credit_spreads(option_legs)

    leg_symbols = sorted({leg.symbol for pair in spread_pairs for leg in pair})
    snapshots = alpaca.get_option_snapshots(leg_symbols, feed=option_feed) if leg_symbols else {}

    underlying_symbols = sorted({short.underlying_symbol for short, _long in spread_pairs})
    bars = alpaca.get_latest_stock_bars(underlying_symbols, feed=stock_feed) if underlying_symbols else {}
    underlying_prices = {
        symbol: _decimal_or_none(_nested_get(bar, "c"))
        for symbol, bar in bars.items()
        if isinstance(bar, dict)
    }

    spreads = [
        _build_monitored_spread(
            config=config,
            short_leg=short_leg,
            long_leg=long_leg,
            snapshots=snapshots,
            underlying_price=underlying_prices.get(short_leg.underlying_symbol),
            market_date=market_date,
            sequence=index + 1,
        )
        for index, (short_leg, long_leg) in enumerate(spread_pairs)
    ]

    return PositionMonitorResult(
        generated_at=now.isoformat(),
        option_position_count=len(option_legs),
        spread_count=len(spreads),
        spreads=spreads,
        unpaired_legs=[_leg_to_dict(leg) for leg in unpaired_legs],
        warnings=warnings,
    )


def parse_occ_option_symbol(symbol: str) -> ParsedOptionSymbol | None:
    match = OCC_SYMBOL_RE.match(symbol.strip().upper())
    if not match:
        return None
    underlying, expiration_raw, option_type, strike_raw = match.groups()
    try:
        expiration = date(
            2000 + int(expiration_raw[0:2]),
            int(expiration_raw[2:4]),
            int(expiration_raw[4:6]),
        )
    except ValueError:
        return None
    strike = Decimal(str(int(strike_raw))) / Decimal("1000")
    return ParsedOptionSymbol(
        symbol=symbol.strip().upper(),
        underlying_symbol=underlying,
        expiration_date=expiration,
        option_type="call" if option_type == "C" else "put",
        strike=strike,
    )


def _position_to_option_leg(position: dict[str, Any]) -> OptionPositionLeg | None:
    symbol = str(position.get("symbol") or "")
    parsed = parse_occ_option_symbol(symbol)
    if not parsed:
        return None

    quantity = _decimal_or_none(position.get("qty"))
    if quantity is None or quantity == 0:
        return None
    side = str(position.get("side") or "").lower()
    if side not in {"long", "short"}:
        side = "short" if quantity < 0 else "long"

    quantity_abs = int(abs(quantity))
    if quantity_abs <= 0:
        return None

    return OptionPositionLeg(
        symbol=parsed.symbol,
        underlying_symbol=parsed.underlying_symbol,
        expiration_date=parsed.expiration_date,
        option_type=parsed.option_type,
        strike=parsed.strike,
        side=side,
        quantity=quantity_abs,
        average_entry_price=_average_entry_price(position, quantity_abs),
        raw_position=position,
    )


def _pair_put_credit_spreads(
    legs: list[OptionPositionLeg],
) -> tuple[list[tuple[OptionPositionLeg, OptionPositionLeg]], list[OptionPositionLeg]]:
    short_puts = [leg for leg in legs if leg.option_type == "put" and leg.side == "short"]
    long_puts = [leg for leg in legs if leg.option_type == "put" and leg.side == "long"]
    used_long_symbols: set[str] = set()
    pairs: list[tuple[OptionPositionLeg, OptionPositionLeg]] = []
    unpaired: list[OptionPositionLeg] = []

    for short_leg in sorted(short_puts, key=lambda leg: (leg.underlying_symbol, leg.expiration_date, -leg.strike)):
        candidates = [
            leg
            for leg in long_puts
            if leg.symbol not in used_long_symbols
            and leg.underlying_symbol == short_leg.underlying_symbol
            and leg.expiration_date == short_leg.expiration_date
            and leg.strike < short_leg.strike
            and leg.quantity == short_leg.quantity
        ]
        if not candidates:
            unpaired.append(short_leg)
            continue
        long_leg = max(candidates, key=lambda leg: leg.strike)
        used_long_symbols.add(long_leg.symbol)
        pairs.append((short_leg, long_leg))

    unpaired.extend([leg for leg in long_puts if leg.symbol not in used_long_symbols])
    unpaired.extend([leg for leg in legs if leg.option_type != "put"])
    return pairs, unpaired


def _build_monitored_spread(
    *,
    config: AppConfig,
    short_leg: OptionPositionLeg,
    long_leg: OptionPositionLeg,
    snapshots: dict[str, dict[str, Any]],
    underlying_price: Decimal | None,
    market_date: date,
    sequence: int,
) -> dict[str, Any]:
    short_quote = _quote_from_snapshot(snapshots.get(short_leg.symbol))
    long_quote = _quote_from_snapshot(snapshots.get(long_leg.symbol))
    width = short_leg.strike - long_leg.strike
    dte = short_leg.expiration_date.toordinal() - market_date.toordinal()
    entry_credit = _entry_credit(short_leg, long_leg)
    close_debit = _close_debit(short_quote, long_quote)
    pnl = _pnl(entry_credit=entry_credit, close_debit=close_debit, quantity=short_leg.quantity)
    profit_pct = _profit_pct(entry_credit=entry_credit, close_debit=close_debit)
    exit_flags = _exit_flags(
        config=config,
        dte=dte,
        entry_credit=entry_credit,
        close_debit=close_debit,
        profit_pct=profit_pct,
        underlying_price=underlying_price,
        short_strike=short_leg.strike,
    )
    close_recommended = any(exit_flags.values())
    spread_id = (
        f"{short_leg.underlying_symbol}-{short_leg.expiration_date.isoformat()}-"
        f"{_fmt_decimal(short_leg.strike)}P-{_fmt_decimal(long_leg.strike)}P"
    )
    close_preview = build_put_credit_spread_close_preview(
        config=config,
        spread={
            "spread_id": spread_id,
            "underlying_symbol": short_leg.underlying_symbol,
            "short_put_symbol": short_leg.symbol,
            "long_put_symbol": long_leg.symbol,
            "quantity": short_leg.quantity,
            "close_limit_price": _fmt_optional_decimal(close_debit),
            "estimated_close_debit": _fmt_optional_decimal(_money(close_debit, short_leg.quantity)),
        },
        client_order_id=build_client_order_id("close-preview", short_leg.underlying_symbol, sequence),
    )

    return {
        "spread_id": spread_id,
        "underlying_symbol": short_leg.underlying_symbol,
        "expiration_date": short_leg.expiration_date.isoformat(),
        "dte": dte,
        "quantity": short_leg.quantity,
        "short_put_symbol": short_leg.symbol,
        "long_put_symbol": long_leg.symbol,
        "short_strike": _fmt_decimal(short_leg.strike),
        "long_strike": _fmt_decimal(long_leg.strike),
        "width": _fmt_decimal(width),
        "underlying_price": _fmt_optional_decimal(underlying_price),
        "entry_credit": _fmt_optional_decimal(entry_credit),
        "close_debit": _fmt_optional_decimal(close_debit),
        "estimated_unrealized_pnl": _fmt_optional_decimal(pnl),
        "profit_pct_of_entry_credit": _fmt_optional_decimal(profit_pct),
        "short_quote": short_quote,
        "long_quote": long_quote,
        "exit_flags": exit_flags,
        "close_recommended": close_recommended,
        "close_order_preview": close_preview,
    }


def _quote_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {"bid": None, "ask": None, "quote_time": None}
    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    bid = _decimal_or_none(_nested_get(quote, "bp", "bid_price", "bidPrice"))
    ask = _decimal_or_none(_nested_get(quote, "ap", "ask_price", "askPrice"))
    return {
        "bid": _fmt_optional_decimal(bid),
        "ask": _fmt_optional_decimal(ask),
        "quote_time": _nested_get(quote, "t", "timestamp"),
    }


def _close_debit(short_quote: dict[str, Any], long_quote: dict[str, Any]) -> Decimal | None:
    short_ask = _decimal_or_none(short_quote.get("ask"))
    long_bid = _decimal_or_none(long_quote.get("bid"))
    if short_ask is None or long_bid is None:
        return None
    return max(Decimal("0"), short_ask - long_bid)


def _entry_credit(short_leg: OptionPositionLeg, long_leg: OptionPositionLeg) -> Decimal | None:
    if short_leg.average_entry_price is None or long_leg.average_entry_price is None:
        return None
    return short_leg.average_entry_price - long_leg.average_entry_price


def _pnl(*, entry_credit: Decimal | None, close_debit: Decimal | None, quantity: int) -> Decimal | None:
    if entry_credit is None or close_debit is None:
        return None
    return (entry_credit - close_debit) * Decimal("100") * Decimal(quantity)


def _profit_pct(*, entry_credit: Decimal | None, close_debit: Decimal | None) -> Decimal | None:
    if entry_credit is None or close_debit is None or entry_credit <= 0:
        return None
    return ((entry_credit - close_debit) / entry_credit) * Decimal("100")


def _exit_flags(
    *,
    config: AppConfig,
    dte: int,
    entry_credit: Decimal | None,
    close_debit: Decimal | None,
    profit_pct: Decimal | None,
    underlying_price: Decimal | None,
    short_strike: Decimal,
) -> dict[str, bool]:
    close_profit_pct = _decimal_or_none(config.get("strategy", "close_profit_pct", default=0.50)) or Decimal("0.50")
    profit_target_pct = close_profit_pct * Decimal("100") if close_profit_pct <= 1 else close_profit_pct
    close_before_days = int(config.get("strategy", "close_before_expiry_days", default=3))
    return {
        "profit_target_hit": profit_pct is not None and profit_pct >= profit_target_pct,
        "loss_trigger_hit": (
            entry_credit is not None
            and close_debit is not None
            and entry_credit > 0
            and close_debit >= entry_credit * Decimal("2")
        ),
        "close_before_expiry": dte <= close_before_days,
        "short_strike_threatened": underlying_price is not None and underlying_price <= short_strike,
    }


def _average_entry_price(position: dict[str, Any], quantity_abs: int) -> Decimal | None:
    avg_entry = _decimal_or_none(
        _nested_get(position, "avg_entry_price", "average_entry_price", "avg_entry_price_per_share")
    )
    if avg_entry is not None:
        return abs(avg_entry)

    cost_basis = _decimal_or_none(position.get("cost_basis"))
    if cost_basis is None or quantity_abs <= 0:
        return None
    return abs(cost_basis) / Decimal(quantity_abs) / Decimal("100")


def _leg_to_dict(leg: OptionPositionLeg) -> dict[str, Any]:
    return {
        "symbol": leg.symbol,
        "underlying_symbol": leg.underlying_symbol,
        "expiration_date": leg.expiration_date.isoformat(),
        "option_type": leg.option_type,
        "strike": _fmt_decimal(leg.strike),
        "side": leg.side,
        "quantity": leg.quantity,
        "average_entry_price": _fmt_optional_decimal(leg.average_entry_price),
    }


def _money(value: Decimal | None, quantity: int) -> Decimal | None:
    if value is None:
        return None
    return value * Decimal("100") * Decimal(quantity)


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _fmt_decimal(value)


def _fmt_decimal(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')).normalize():f}"

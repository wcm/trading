from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.brokers.alpaca import AlpacaClient
from trading_bot.config import AppConfig


STRATEGY_NAME = "put_credit_spread"


@dataclass(frozen=True)
class OptionLeg:
    symbol: str
    strike: Decimal
    expiration_date: date
    bid: Decimal
    ask: Decimal
    mid: Decimal
    bid_ask_spread: Decimal
    bid_ask_spread_pct_of_mid: Decimal | None
    open_interest: int | None
    open_interest_date: str | None
    quote_time: datetime | None
    quote_age_seconds: int | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None


@dataclass(frozen=True)
class PutCreditSpreadCandidate:
    candidate_id: str
    underlying_symbol: str
    underlying_price: str | None
    expiration_date: str
    dte: int
    short_put_symbol: str
    long_put_symbol: str
    short_strike: str
    long_strike: str
    short_put_distance_pct: str | None
    width: str
    net_credit: str
    max_profit: str
    max_loss: str
    liquidity_ok: bool
    short_open_interest: int | None
    long_open_interest: int | None
    short_open_interest_date: str | None
    long_open_interest_date: str | None
    short_bid_ask_spread: str
    long_bid_ask_spread: str
    short_spread_pct_of_mid: str | None
    long_spread_pct_of_mid: str | None
    short_delta: str | None
    long_delta: str | None
    short_quote_time: str | None
    long_quote_time: str | None
    max_quote_age_seconds: int | None
    quantity: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PutCreditSpreadScanResult:
    scanned_at: str
    symbols: list[str]
    feed: str
    stock_feed: str
    contracts_seen: int
    snapshots_seen: int
    candidates: list[PutCreditSpreadCandidate]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return data


def scan_put_credit_spreads(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    symbols: list[str],
    max_candidates: int = 20,
    option_feed: str | None = None,
) -> PutCreditSpreadScanResult:
    now = datetime.now(UTC)
    market_date = datetime.now(ZoneInfo("America/New_York")).date()

    min_dte = int(config.get("strategy", "min_dte", default=14))
    max_dte = int(config.get("strategy", "max_dte", default=30))
    min_expiration = market_date.toordinal() + min_dte
    max_expiration = market_date.toordinal() + max_dte

    option_feed = option_feed or str(config.get("alpaca", "option_data_feed", default="indicative"))
    stock_feed = str(config.get("alpaca", "stock_data_feed", default="iex"))

    expiration_date_gte = date.fromordinal(min_expiration).isoformat()
    expiration_date_lte = date.fromordinal(max_expiration).isoformat()

    bars = alpaca.get_latest_stock_bars(symbols, feed=stock_feed)
    underlying_prices = {
        symbol: _decimal_or_none(_nested_get(bar, "c"))
        for symbol, bar in bars.items()
        if isinstance(bar, dict)
    }

    contracts = alpaca.get_option_contracts(
        underlying_symbols=symbols,
        expiration_date_gte=expiration_date_gte,
        expiration_date_lte=expiration_date_lte,
        option_type="put",
    )

    valid_contracts = [contract for contract in contracts if _is_tradable_put(contract)]
    contract_symbols = [str(contract["symbol"]) for contract in valid_contracts if contract.get("symbol")]
    snapshots = alpaca.get_option_snapshots(contract_symbols, feed=option_feed) if contract_symbols else {}

    legs_by_underlying_expiration_strike: dict[tuple[str, date, Decimal], OptionLeg] = {}
    warnings: list[str] = []
    for contract in valid_contracts:
        leg = _build_leg(contract, snapshots.get(str(contract.get("symbol"))), now=now)
        if not leg:
            continue
        key = (str(contract.get("underlying_symbol")), leg.expiration_date, leg.strike)
        legs_by_underlying_expiration_strike[key] = leg

    candidates: list[PutCreditSpreadCandidate] = []
    width_values = [_decimal_or_none(width) for width in config.get("strategy", "spread_widths", default=[5, 10])]
    widths = [width for width in width_values if width is not None and width > 0]
    min_credit_pct = _decimal_or_none(config.get("strategy", "min_credit_as_width_pct", default=0.20)) or Decimal("0.20")
    short_delta_min = _decimal_or_none(config.get("strategy", "short_put_delta_min", default=-0.18)) or Decimal("-0.18")
    short_delta_max = _decimal_or_none(config.get("strategy", "short_put_delta_max", default=-0.10)) or Decimal("-0.10")
    min_short_put_distance_pct = _decimal_or_none(
        config.get("strategy", "min_short_put_distance_pct", default=0)
    ) or Decimal("0")
    min_short_open_interest = _int_or_none(
        config.get("liquidity", "min_short_leg_open_interest", default=0)
    )
    min_long_open_interest = _int_or_none(
        config.get("liquidity", "min_long_leg_open_interest", default=0)
    )
    min_short_bid = _decimal_or_none(config.get("liquidity", "min_short_leg_bid", default=0)) or Decimal("0")
    max_leg_spread_pct_of_mid = _decimal_or_none(
        config.get("liquidity", "max_leg_spread_pct_of_mid", default=1)
    )
    max_leg_spread_absolute = _decimal_or_none(
        config.get("liquidity", "max_leg_spread_absolute", default=999)
    )

    for (underlying, expiration, strike), short_leg in legs_by_underlying_expiration_strike.items():
        if short_leg.delta is None:
            continue
        if not (short_delta_min <= short_leg.delta <= short_delta_max):
            continue
        short_put_distance_pct = _short_put_distance_pct(
            underlying_prices.get(underlying),
            strike,
        )
        if min_short_put_distance_pct > 0:
            if short_put_distance_pct is None or short_put_distance_pct < min_short_put_distance_pct:
                continue

        for width in widths:
            long_strike = strike - width
            long_leg = legs_by_underlying_expiration_strike.get((underlying, expiration, long_strike))
            if not long_leg:
                continue
            if not _liquidity_ok(
                short_leg=short_leg,
                long_leg=long_leg,
                min_short_open_interest=min_short_open_interest,
                min_long_open_interest=min_long_open_interest,
                min_short_bid=min_short_bid,
                max_leg_spread_pct_of_mid=max_leg_spread_pct_of_mid,
                max_leg_spread_absolute=max_leg_spread_absolute,
            ):
                continue

            net_credit = short_leg.bid - long_leg.ask
            if net_credit <= 0:
                continue
            if net_credit < width * min_credit_pct:
                continue

            max_profit = net_credit * Decimal("100")
            max_loss = (width - net_credit) * Decimal("100")
            dte = expiration.toordinal() - market_date.toordinal()
            candidate_id = f"{underlying}-{expiration.isoformat()}-{_fmt_decimal(strike)}P-{_fmt_decimal(long_strike)}P"
            candidates.append(
                PutCreditSpreadCandidate(
                    candidate_id=candidate_id,
                    underlying_symbol=underlying,
                    underlying_price=_fmt_optional_decimal(underlying_prices.get(underlying)),
                    expiration_date=expiration.isoformat(),
                    dte=dte,
                    short_put_symbol=short_leg.symbol,
                    long_put_symbol=long_leg.symbol,
                    short_strike=_fmt_decimal(strike),
                    long_strike=_fmt_decimal(long_strike),
                    short_put_distance_pct=_fmt_optional_decimal(short_put_distance_pct),
                    width=_fmt_decimal(width),
                    net_credit=_fmt_decimal(net_credit),
                    max_profit=_fmt_decimal(max_profit),
                    max_loss=_fmt_decimal(max_loss),
                    liquidity_ok=True,
                    short_open_interest=short_leg.open_interest,
                    long_open_interest=long_leg.open_interest,
                    short_open_interest_date=short_leg.open_interest_date,
                    long_open_interest_date=long_leg.open_interest_date,
                    short_bid_ask_spread=_fmt_decimal(short_leg.bid_ask_spread),
                    long_bid_ask_spread=_fmt_decimal(long_leg.bid_ask_spread),
                    short_spread_pct_of_mid=_fmt_optional_decimal(short_leg.bid_ask_spread_pct_of_mid),
                    long_spread_pct_of_mid=_fmt_optional_decimal(long_leg.bid_ask_spread_pct_of_mid),
                    short_delta=_fmt_optional_decimal(short_leg.delta),
                    long_delta=_fmt_optional_decimal(long_leg.delta),
                    short_quote_time=short_leg.quote_time.isoformat() if short_leg.quote_time else None,
                    long_quote_time=long_leg.quote_time.isoformat() if long_leg.quote_time else None,
                    max_quote_age_seconds=_max_optional_int(
                        short_leg.quote_age_seconds,
                        long_leg.quote_age_seconds,
                    ),
                )
            )

    candidates.sort(key=lambda candidate: (Decimal(candidate.max_loss), -Decimal(candidate.net_credit)))
    if not candidates:
        warnings.append(
            "No put credit spread candidates passed the current delta/distance/liquidity/credit filters."
        )

    return PutCreditSpreadScanResult(
        scanned_at=now.isoformat(),
        symbols=symbols,
        feed=option_feed,
        stock_feed=stock_feed,
        contracts_seen=len(valid_contracts),
        snapshots_seen=len(snapshots),
        candidates=candidates[:max_candidates],
        warnings=warnings,
    )


def _is_tradable_put(contract: dict[str, Any]) -> bool:
    return (
        str(contract.get("type", "")).lower() == "put"
        and str(contract.get("status", "")).lower() == "active"
        and bool(contract.get("tradable", True))
        and bool(contract.get("symbol"))
        and bool(contract.get("expiration_date"))
        and contract.get("strike_price") is not None
    )


def _build_leg(contract: dict[str, Any], snapshot: dict[str, Any] | None, *, now: datetime) -> OptionLeg | None:
    if not snapshot:
        return None

    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    greeks = snapshot.get("greeks") or {}

    bid = _decimal_or_none(_nested_get(quote, "bp", "bid_price", "bidPrice"))
    ask = _decimal_or_none(_nested_get(quote, "ap", "ask_price", "askPrice"))
    strike = _decimal_or_none(contract.get("strike_price"))
    if bid is None or ask is None or strike is None or bid <= 0 or ask <= 0 or ask < bid:
        return None

    expiration_raw = contract.get("expiration_date")
    try:
        expiration = date.fromisoformat(str(expiration_raw))
    except ValueError:
        return None

    quote_time = _parse_time(_nested_get(quote, "t", "timestamp"))
    quote_age_seconds = _age_seconds(now, quote_time)
    bid_ask_spread = ask - bid
    mid = (bid + ask) / Decimal("2")

    return OptionLeg(
        symbol=str(contract["symbol"]),
        strike=strike,
        expiration_date=expiration,
        bid=bid,
        ask=ask,
        mid=mid,
        bid_ask_spread=bid_ask_spread,
        bid_ask_spread_pct_of_mid=(bid_ask_spread / mid) if mid > 0 else None,
        open_interest=_int_or_none(contract.get("open_interest")),
        open_interest_date=str(contract["open_interest_date"]) if contract.get("open_interest_date") else None,
        quote_time=quote_time,
        quote_age_seconds=quote_age_seconds,
        delta=_decimal_or_none(_nested_get(greeks, "delta")),
        gamma=_decimal_or_none(_nested_get(greeks, "gamma")),
        theta=_decimal_or_none(_nested_get(greeks, "theta")),
        vega=_decimal_or_none(_nested_get(greeks, "vega")),
    )


def _liquidity_ok(
    *,
    short_leg: OptionLeg,
    long_leg: OptionLeg,
    min_short_open_interest: int | None,
    min_long_open_interest: int | None,
    min_short_bid: Decimal,
    max_leg_spread_pct_of_mid: Decimal | None,
    max_leg_spread_absolute: Decimal | None,
) -> bool:
    if short_leg.bid < min_short_bid:
        return False
    if min_short_open_interest is not None:
        if short_leg.open_interest is None or short_leg.open_interest < min_short_open_interest:
            return False
    if min_long_open_interest is not None:
        if long_leg.open_interest is None or long_leg.open_interest < min_long_open_interest:
            return False

    for leg in (short_leg, long_leg):
        if max_leg_spread_absolute is not None and leg.bid_ask_spread > max_leg_spread_absolute:
            return False
        if max_leg_spread_pct_of_mid is not None:
            if leg.bid_ask_spread_pct_of_mid is None:
                return False
            if leg.bid_ask_spread_pct_of_mid > max_leg_spread_pct_of_mid:
                return False

    return True


def _short_put_distance_pct(underlying_price: Decimal | None, short_strike: Decimal) -> Decimal | None:
    if underlying_price is None or underlying_price <= 0:
        return None
    return ((underlying_price - short_strike) / underlying_price) * Decimal("100")


def _nested_get(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(now: datetime, timestamp: datetime | None) -> int | None:
    if timestamp is None:
        return None
    age = int((now - timestamp).total_seconds())
    if -60 <= age < 0:
        return 0
    return age


def _max_optional_int(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return max(present)


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _fmt_decimal(value)


def _fmt_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")) if value == value.quantize(Decimal("0.01")) else value, "f")

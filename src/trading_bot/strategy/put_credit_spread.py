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
    width: str
    net_credit: str
    max_profit: str
    max_loss: str
    short_delta: str | None
    long_delta: str | None
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

    min_dte = int(config.get("strategy", "min_dte", default=7))
    max_dte = int(config.get("strategy", "max_dte", default=21))
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
        leg = _build_leg(contract, snapshots.get(str(contract.get("symbol"))))
        if not leg:
            continue
        key = (str(contract.get("underlying_symbol")), leg.expiration_date, leg.strike)
        legs_by_underlying_expiration_strike[key] = leg

    candidates: list[PutCreditSpreadCandidate] = []
    width_values = [_decimal_or_none(width) for width in config.get("strategy", "spread_widths", default=[5, 10])]
    widths = [width for width in width_values if width is not None and width > 0]
    min_credit_pct = _decimal_or_none(config.get("strategy", "min_credit_as_width_pct", default=0.20)) or Decimal("0.20")
    short_delta_min = _decimal_or_none(config.get("strategy", "short_put_delta_min", default=-0.30)) or Decimal("-0.30")
    short_delta_max = _decimal_or_none(config.get("strategy", "short_put_delta_max", default=-0.20)) or Decimal("-0.20")

    for (underlying, expiration, strike), short_leg in legs_by_underlying_expiration_strike.items():
        if short_leg.delta is None:
            continue
        if not (short_delta_min <= short_leg.delta <= short_delta_max):
            continue

        for width in widths:
            long_strike = strike - width
            long_leg = legs_by_underlying_expiration_strike.get((underlying, expiration, long_strike))
            if not long_leg:
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
                    width=_fmt_decimal(width),
                    net_credit=_fmt_decimal(net_credit),
                    max_profit=_fmt_decimal(max_profit),
                    max_loss=_fmt_decimal(max_loss),
                    short_delta=_fmt_optional_decimal(short_leg.delta),
                    long_delta=_fmt_optional_decimal(long_leg.delta),
                )
            )

    candidates.sort(key=lambda candidate: (Decimal(candidate.max_loss), -Decimal(candidate.net_credit)))
    if not candidates:
        warnings.append("No put credit spread candidates passed the current delta/liquidity/credit filters.")

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


def _build_leg(contract: dict[str, Any], snapshot: dict[str, Any] | None) -> OptionLeg | None:
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

    return OptionLeg(
        symbol=str(contract["symbol"]),
        strike=strike,
        expiration_date=expiration,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / Decimal("2"),
        delta=_decimal_or_none(_nested_get(greeks, "delta")),
        gamma=_decimal_or_none(_nested_get(greeks, "gamma")),
        theta=_decimal_or_none(_nested_get(greeks, "theta")),
        vega=_decimal_or_none(_nested_get(greeks, "vega")),
    )


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


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _fmt_decimal(value)


def _fmt_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")) if value == value.quantize(Decimal("0.01")) else value, "f")

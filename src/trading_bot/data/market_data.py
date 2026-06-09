from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.brokers.alpaca import AlpacaClient
from trading_bot.config import AppConfig


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SymbolMarketContext:
    symbol: str
    feed: str
    latest_price: str | None
    latest_bar_time: str | None
    latest_bar_age_seconds: int | None
    latest_bar_fresh: bool
    intraday_open: str | None
    intraday_change_pct: str | None
    block_intraday_down: bool | None
    trend_timeframe: str
    trend_period: int
    trend_latest_close: str | None
    trend_ma: str | None
    above_trend_ma: bool | None
    market_trend_ok: bool
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarketContext:
    generated_at: str
    symbols: dict[str, SymbolMarketContext]
    broad_market_symbol: str | None
    broad_market_filter_ok: bool | None
    broad_market_warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "broad_market_symbol": self.broad_market_symbol,
            "broad_market_filter_ok": self.broad_market_filter_ok,
            "broad_market_warnings": self.broad_market_warnings,
            "symbols": {symbol: context.to_dict() for symbol, context in self.symbols.items()},
        }


def build_market_context(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    symbols: list[str],
) -> MarketContext:
    now = datetime.now(UTC)
    now_eastern = now.astimezone(EASTERN)
    market_day = now_eastern.date()
    market_open = datetime.combine(market_day, time(hour=9, minute=30), tzinfo=EASTERN)

    feed = str(config.get("alpaca", "stock_data_feed", default="iex"))
    intraday_timeframe = str(config.get("market_filters", "intraday_timeframe", default="1Min"))
    trend_timeframe = str(config.get("market_filters", "trend_timeframe", default="30Min"))
    trend_period = int(config.get("market_filters", "require_above_30m_ma_period", default=20))
    trend_lookback_days = int(config.get("market_filters", "trend_lookback_days", default=14))
    underlying_down_threshold = _decimal_or_none(
        config.get("market_filters", "block_if_underlying_down_intraday_pct", default=0.75)
    )
    broad_market_symbol = _symbol_or_none(config.get("market_filters", "broad_market_symbol", default=None))
    broad_down_threshold = _decimal_or_none(
        config.get(
            "market_filters",
            "block_if_broad_market_down_intraday_pct",
            default=underlying_down_threshold,
        )
    )
    require_broad_above_ma = bool(
        config.get("market_filters", "require_broad_market_above_30m_ma", default=True)
    )
    max_bar_age_minutes = int(config.get("market_filters", "max_bar_age_minutes", default=30))
    context_symbols = _dedupe_symbols(
        symbols + ([broad_market_symbol] if broad_market_symbol else [])
    )

    intraday_bars = alpaca.get_stock_bars(
        context_symbols,
        timeframe=intraday_timeframe,
        start=market_open.astimezone(UTC).isoformat(),
        feed=feed,
        limit=10_000,
        sort="asc",
    )
    trend_bars = alpaca.get_stock_bars(
        context_symbols,
        timeframe=trend_timeframe,
        start=(now - timedelta(days=trend_lookback_days)).isoformat(),
        feed=feed,
        limit=10_000,
        sort="asc",
    )

    contexts: dict[str, SymbolMarketContext] = {}
    for symbol in context_symbols:
        down_threshold = (
            broad_down_threshold
            if broad_market_symbol and symbol == broad_market_symbol
            else underlying_down_threshold
        )
        contexts[symbol] = _build_symbol_context(
            symbol=symbol,
            feed=feed,
            now=now,
            intraday_bars=intraday_bars.get(symbol, []),
            trend_bars=trend_bars.get(symbol, []),
            trend_timeframe=trend_timeframe,
            trend_period=trend_period,
            down_threshold=down_threshold,
            max_bar_age_seconds=max_bar_age_minutes * 60,
        )

    broad_market_warnings: list[str] = []
    broad_market_filter_ok = None
    if broad_market_symbol:
        broad_context = contexts.get(broad_market_symbol)
        if broad_context is None:
            broad_market_filter_ok = False
            broad_market_warnings.append(f"Broad market context is unavailable for {broad_market_symbol}.")
        else:
            broad_market_filter_ok = _broad_market_filter_ok(
                broad_context,
                require_above_ma=require_broad_above_ma,
            )
            broad_market_warnings.extend(broad_context.warnings)

    return MarketContext(
        generated_at=now.isoformat(),
        symbols=contexts,
        broad_market_symbol=broad_market_symbol,
        broad_market_filter_ok=broad_market_filter_ok,
        broad_market_warnings=broad_market_warnings,
    )


def _build_symbol_context(
    *,
    symbol: str,
    feed: str,
    now: datetime,
    intraday_bars: list[dict[str, Any]],
    trend_bars: list[dict[str, Any]],
    trend_timeframe: str,
    trend_period: int,
    down_threshold: Decimal | None,
    max_bar_age_seconds: int,
) -> SymbolMarketContext:
    warnings: list[str] = []
    clean_intraday_bars = [_bar for _bar in intraday_bars if _decimal_or_none(_bar.get("c")) is not None]
    clean_trend_bars = [_bar for _bar in trend_bars if _decimal_or_none(_bar.get("c")) is not None]

    latest_intraday = clean_intraday_bars[-1] if clean_intraday_bars else None
    first_intraday = clean_intraday_bars[0] if clean_intraday_bars else None
    latest_price = _decimal_or_none(latest_intraday.get("c")) if latest_intraday else None
    intraday_open = _decimal_or_none(first_intraday.get("o")) if first_intraday else None

    latest_bar_time = _parse_time(latest_intraday.get("t")) if latest_intraday else None
    latest_bar_age_seconds = _age_seconds(now, latest_bar_time)
    if latest_bar_age_seconds is None:
        latest_bar_fresh = False
        warnings.append("No latest intraday bar was available.")
    elif latest_bar_age_seconds < -60:
        latest_bar_fresh = False
        warnings.append(f"Latest intraday bar is from the future: age={latest_bar_age_seconds} seconds.")
    elif latest_bar_age_seconds > max_bar_age_seconds:
        latest_bar_fresh = False
        warnings.append(f"Latest intraday bar is stale: {latest_bar_age_seconds} seconds old.")
    else:
        latest_bar_fresh = True

    intraday_change_pct: Decimal | None = None
    if latest_price is not None and intraday_open is not None and intraday_open != 0:
        intraday_change_pct = ((latest_price - intraday_open) / intraday_open) * Decimal("100")
    else:
        warnings.append("Intraday change could not be calculated.")

    block_intraday_down: bool | None = None
    if intraday_change_pct is not None and down_threshold is not None:
        block_intraday_down = intraday_change_pct <= -abs(down_threshold)

    trend_closes = [_decimal_or_none(bar.get("c")) for bar in clean_trend_bars]
    trend_closes = [close for close in trend_closes if close is not None]
    trend_latest_close = trend_closes[-1] if trend_closes else None
    trend_ma: Decimal | None = None
    above_trend_ma: bool | None = None
    if len(trend_closes) >= trend_period:
        trend_ma = sum(trend_closes[-trend_period:]) / Decimal(str(trend_period))
        if trend_latest_close is not None:
            above_trend_ma = trend_latest_close > trend_ma
    else:
        warnings.append(f"Only {len(trend_closes)} trend bars available; need {trend_period}.")

    market_trend_ok = latest_bar_fresh and block_intraday_down is False and above_trend_ma is True
    return SymbolMarketContext(
        symbol=symbol,
        feed=feed,
        latest_price=_fmt_optional_decimal(latest_price),
        latest_bar_time=latest_bar_time.isoformat() if latest_bar_time else None,
        latest_bar_age_seconds=latest_bar_age_seconds,
        latest_bar_fresh=latest_bar_fresh,
        intraday_open=_fmt_optional_decimal(intraday_open),
        intraday_change_pct=_fmt_optional_decimal(intraday_change_pct),
        block_intraday_down=block_intraday_down,
        trend_timeframe=trend_timeframe,
        trend_period=trend_period,
        trend_latest_close=_fmt_optional_decimal(trend_latest_close),
        trend_ma=_fmt_optional_decimal(trend_ma),
        above_trend_ma=above_trend_ma,
        market_trend_ok=market_trend_ok,
        warnings=warnings,
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
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


def _broad_market_filter_ok(context: SymbolMarketContext, *, require_above_ma: bool) -> bool:
    if context.latest_bar_fresh is not True:
        return False
    if context.block_intraday_down is not False:
        return False
    if require_above_ma and context.above_trend_ma is not True:
        return False
    return True


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw_symbol in symbols:
        symbol = _symbol_or_none(raw_symbol)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result


def _symbol_or_none(value: Any) -> str | None:
    if value is None:
        return None
    symbol = str(value).strip().upper()
    return symbol or None


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
    return format(value.quantize(Decimal("0.0001")), "f")

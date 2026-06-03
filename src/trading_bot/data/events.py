from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.config import AppConfig


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SymbolEventContext:
    symbol: str
    earnings_source: str
    next_earnings_date: str | None
    days_to_earnings: int | None
    block_earnings_within_days: int
    earnings_ok: bool
    confidence: str
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventContext:
    generated_at: str
    symbols: dict[str, SymbolEventContext]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "symbols": {symbol: context.to_dict() for symbol, context in self.symbols.items()},
        }


def build_event_context(*, config: AppConfig, symbols: list[str]) -> EventContext:
    now = datetime.now(UTC)
    market_day = now.astimezone(EASTERN).date()
    source = str(config.get("event_filters", "earnings", "source", default="manual"))
    manual_dates = config.get("event_filters", "earnings", "manual_next_earnings_dates", default={})
    if not isinstance(manual_dates, dict):
        manual_dates = {}
    assume_unknown_ok_in_paper = bool(
        config.get("event_filters", "earnings", "assume_unknown_ok_in_paper", default=False)
    )
    block_days = int(config.get("market_filters", "block_earnings_within_days", default=7))

    contexts = {
        symbol: _build_symbol_event_context(
            symbol=symbol,
            source=source,
            manual_date=manual_dates.get(symbol),
            mode=config.mode,
            assume_unknown_ok_in_paper=assume_unknown_ok_in_paper,
            block_days=block_days,
            market_day=market_day,
        )
        for symbol in symbols
    }
    return EventContext(generated_at=now.isoformat(), symbols=contexts)


def _build_symbol_event_context(
    *,
    symbol: str,
    source: str,
    manual_date: Any,
    mode: str,
    assume_unknown_ok_in_paper: bool,
    block_days: int,
    market_day: date,
) -> SymbolEventContext:
    warnings: list[str] = []
    next_earnings = _date_or_none(manual_date)
    if next_earnings is None:
        if manual_date:
            warnings.append(f"Could not parse configured next earnings date: {manual_date!r}.")
        if mode == "paper" and assume_unknown_ok_in_paper:
            warnings.append("No earnings date configured; paper mode assumes earnings are not blocking.")
            return SymbolEventContext(
                symbol=symbol,
                earnings_source=source,
                next_earnings_date=None,
                days_to_earnings=None,
                block_earnings_within_days=block_days,
                earnings_ok=True,
                confidence="paper_assumption",
                warnings=warnings,
            )
        warnings.append("No earnings date configured.")
        return SymbolEventContext(
            symbol=symbol,
            earnings_source=source,
            next_earnings_date=None,
            days_to_earnings=None,
            block_earnings_within_days=block_days,
            earnings_ok=False,
            confidence="unknown",
            warnings=warnings,
        )

    days_to_earnings = (next_earnings - market_day).days
    earnings_ok = days_to_earnings < 0 or days_to_earnings > block_days
    return SymbolEventContext(
        symbol=symbol,
        earnings_source=source,
        next_earnings_date=next_earnings.isoformat(),
        days_to_earnings=days_to_earnings,
        block_earnings_within_days=block_days,
        earnings_ok=earnings_ok,
        confidence="manual",
        warnings=warnings,
    )


def _date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None

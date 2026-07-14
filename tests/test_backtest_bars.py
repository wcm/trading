from __future__ import annotations

from decimal import Decimal

from trading_bot.backtesting.bars import (
    PriceBar,
    _is_regular_session_bar,
    _regular_session_bars_for_alpaca,
)


def test_regular_session_filter_uses_new_york_market_hours() -> None:
    assert not _is_regular_session_bar(_bar("2026-07-13T12:00:00Z"))
    assert _is_regular_session_bar(_bar("2026-07-13T13:30:00Z"))
    assert _is_regular_session_bar(_bar("2026-07-13T19:55:00Z"))
    assert not _is_regular_session_bar(_bar("2026-07-13T20:00:00Z"))


def test_alpaca_intraday_filter_also_applies_to_cached_bars() -> None:
    bars = [_bar("2026-07-13T12:00:00Z"), _bar("2026-07-13T13:30:00Z")]

    filtered = _regular_session_bars_for_alpaca(
        bars,
        source="alpaca",
        timeframe="5Min",
    )

    assert filtered == [bars[1]]
    assert _regular_session_bars_for_alpaca(bars, source="yahoo", timeframe="5Min") == bars


def _bar(timestamp: str) -> PriceBar:
    return PriceBar(
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
    )

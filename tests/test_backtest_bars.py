from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from trading_bot.backtesting.bars import (
    PriceBar,
    _cache_path,
    _is_regular_session_bar,
    _regular_session_bars_for_alpaca,
    _validate_requested_coverage,
)
from trading_bot.brokers.alpaca import AlpacaClient


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


def test_alpaca_cache_separates_raw_and_split_adjusted_bars(tmp_path) -> None:
    common = {
        "cache_dir": tmp_path,
        "source": "alpaca",
        "symbol": "TQQQ",
        "timeframe": "5Min",
        "start": date(2025, 7, 17),
        "end": date(2026, 7, 17),
        "feed": "iex",
    }

    raw_path = _cache_path(**common, adjustment="raw")
    split_path = _cache_path(**common, adjustment="split")

    assert raw_path != split_path
    assert raw_path.name.endswith("_iex_raw.json")
    assert split_path.name.endswith("_iex_split.json")


def test_alpaca_stock_bars_forwards_adjustment_parameter() -> None:
    client = AlpacaClient(
        base_url="https://paper-api.alpaca.markets",
        data_base_url="https://data.alpaca.markets",
        key_id="key",
        secret_key="secret",
    )

    with patch.object(
        AlpacaClient,
        "_get_data",
        return_value={"bars": {}, "next_page_token": None},
    ) as get_data:
        client.get_stock_bars(
            ["TQQQ"],
            timeframe="5Min",
            start="2025-07-17T04:00:00+00:00",
            end="2026-07-18T04:00:00+00:00",
            feed="iex",
            adjustment="split",
        )

    assert get_data.call_args.kwargs["params"]["adjustment"] == "split"


def test_historical_coverage_allows_normal_weekend_boundary_gap() -> None:
    bars = [
        _bar("2026-07-20T13:30:00Z"),
        _bar("2026-07-24T19:55:00Z"),
    ]

    _validate_requested_coverage(
        bars,
        start=date(2026, 7, 17),
        end=date(2026, 7, 26),
    )


def test_historical_coverage_rejects_missing_start_of_period() -> None:
    bars = [
        _bar("2020-07-27T13:30:00Z"),
        _bar("2020-12-31T20:55:00Z"),
    ]

    with pytest.raises(ValueError, match="refusing to run a partial-period backtest"):
        _validate_requested_coverage(
            bars,
            start=date(2020, 1, 1),
            end=date(2020, 12, 31),
        )


def _bar(timestamp: str) -> PriceBar:
    return PriceBar(
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
    )

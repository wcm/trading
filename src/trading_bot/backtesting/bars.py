from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from trading_bot.brokers.alpaca import AlpacaClient
from trading_bot.config import AppConfig, resolve_path
from trading_bot.utils.money import decimal_or_none


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PriceBar:
    timestamp: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None = None

    @property
    def day(self) -> date:
        parsed = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        return parsed.astimezone(EASTERN).date()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("open", "high", "low", "close"):
            data[key] = str(data[key])
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PriceBar":
        return cls(
            timestamp=str(data["timestamp"]),
            open=Decimal(str(data["open"])),
            high=Decimal(str(data["high"])),
            low=Decimal(str(data["low"])),
            close=Decimal(str(data["close"])),
            volume=int(data["volume"]) if data.get("volume") is not None else None,
        )


def load_historical_bars(
    *,
    config: AppConfig,
    alpaca: AlpacaClient | None,
    symbol: str,
    timeframe: str,
    start: date,
    end: date,
    source: str,
    cache_dir: str | Path,
    feed: str | None = None,
    use_cache: bool = True,
) -> list[PriceBar]:
    cache_path = _cache_path(
        cache_dir=cache_dir,
        source=source,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        feed=feed,
    )
    if use_cache and cache_path.exists():
        bars = _read_cached_bars(cache_path)
        return _regular_session_bars_for_alpaca(bars, source=source, timeframe=timeframe)

    if source == "alpaca":
        if alpaca is None:
            raise ValueError("Alpaca data source requires an Alpaca client")
        bars = _load_alpaca_bars(
            config=config,
            alpaca=alpaca,
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            feed=feed,
        )
    elif source == "yahoo":
        bars = _load_yahoo_bars(symbol=symbol, timeframe=timeframe, start=start, end=end)
    else:
        raise ValueError(f"Unsupported historical data source: {source}")

    if use_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps([bar.to_dict() for bar in bars], indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return bars


def _load_alpaca_bars(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    symbol: str,
    timeframe: str,
    start: date,
    end: date,
    feed: str | None,
) -> list[PriceBar]:
    market_start = datetime.combine(start, time.min, tzinfo=EASTERN).astimezone(UTC)
    market_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=EASTERN).astimezone(UTC)
    data = alpaca.get_stock_bars(
        [symbol],
        timeframe=timeframe,
        start=market_start.isoformat(),
        end=market_end.isoformat(),
        feed=feed or str(config.get("alpaca", "stock_data_feed", default="iex")),
        limit=10_000,
        sort="asc",
    )
    bars = _bars_from_alpaca_rows(data.get(symbol.upper(), []))
    return _regular_session_bars_for_alpaca(bars, source="alpaca", timeframe=timeframe)


def _bars_from_alpaca_rows(rows: list[dict[str, Any]]) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for row in rows:
        open_price = decimal_or_none(row.get("o"))
        high = decimal_or_none(row.get("h"))
        low = decimal_or_none(row.get("l"))
        close = decimal_or_none(row.get("c"))
        timestamp = row.get("t")
        if None in (open_price, high, low, close) or not timestamp:
            continue
        bars.append(
            PriceBar(
                timestamp=str(timestamp),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=int(row["v"]) if row.get("v") is not None else None,
            )
        )
    return bars


def _is_intraday_timeframe(timeframe: str) -> bool:
    normalized = timeframe.strip().lower()
    return "min" in normalized or "hour" in normalized


def _regular_session_bars_for_alpaca(
    bars: list[PriceBar],
    *,
    source: str,
    timeframe: str,
) -> list[PriceBar]:
    if source != "alpaca" or not _is_intraday_timeframe(timeframe):
        return bars
    return [bar for bar in bars if _is_regular_session_bar(bar)]


def _is_regular_session_bar(bar: PriceBar) -> bool:
    timestamp = datetime.fromisoformat(bar.timestamp.replace("Z", "+00:00"))
    eastern_time = timestamp.astimezone(EASTERN)
    return (
        eastern_time.weekday() < 5
        and time(9, 30) <= eastern_time.time().replace(tzinfo=None) < time(16, 0)
    )


def _load_yahoo_bars(*, symbol: str, timeframe: str, start: date, end: date) -> list[PriceBar]:
    if timeframe != "1Day":
        raise ValueError("Yahoo data source currently supports only timeframe=1Day")

    period1 = int(datetime.combine(start, time.min, tzinfo=UTC).timestamp())
    period2 = int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
    params = {
        "period1": str(period1),
        "period2": str(period2),
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    response = httpx.get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("chart", {}).get("result", [None])[0]
    if not isinstance(result, dict):
        raise ValueError(f"Yahoo response did not contain chart data for {symbol}")

    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    bars: list[PriceBar] = []
    for index, raw_timestamp in enumerate(timestamps):
        values = (
            _list_get(opens, index),
            _list_get(highs, index),
            _list_get(lows, index),
            _list_get(closes, index),
        )
        if any(value is None for value in values):
            continue
        timestamp = datetime.fromtimestamp(int(raw_timestamp), tz=UTC).isoformat()
        bars.append(
            PriceBar(
                timestamp=timestamp,
                open=Decimal(str(values[0])),
                high=Decimal(str(values[1])),
                low=Decimal(str(values[2])),
                close=Decimal(str(values[3])),
                volume=int(volumes[index]) if index < len(volumes) and volumes[index] is not None else None,
            )
        )
    return bars


def _list_get(values: list[Any], index: int) -> Any:
    return values[index] if index < len(values) else None


def _cache_path(
    *,
    cache_dir: str | Path,
    source: str,
    symbol: str,
    timeframe: str,
    start: date,
    end: date,
    feed: str | None,
) -> Path:
    safe_parts = [
        source,
        symbol.upper(),
        timeframe,
        start.isoformat(),
        end.isoformat(),
    ]
    if feed:
        safe_parts.append(feed)
    filename = _safe_filename("_".join(safe_parts)) + ".json"
    return resolve_path(cache_dir) / filename


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def _read_cached_bars(path: Path) -> list[PriceBar]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"Cached bars file must contain a list: {path}")
    return [PriceBar.from_dict(row) for row in rows if isinstance(row, dict)]

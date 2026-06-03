from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_bot.brokers.alpaca import AlpacaClient
from trading_bot.config import AppConfig


@dataclass(frozen=True)
class NewsItem:
    id: int | str | None
    headline: str | None
    summary: str | None
    author: str | None
    created_at: str | None
    updated_at: str | None
    symbols: list[str]
    url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NewsContext:
    provider: str
    generated_at: str
    lookback_hours: int
    symbols: list[str]
    item_count: int
    items: list[NewsItem]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "generated_at": self.generated_at,
            "lookback_hours": self.lookback_hours,
            "symbols": self.symbols,
            "item_count": self.item_count,
            "items": [item.to_dict() for item in self.items],
            "warnings": self.warnings,
        }


def build_news_context(
    *,
    config: AppConfig,
    alpaca: AlpacaClient,
    symbols: list[str],
) -> NewsContext:
    now = datetime.now(UTC)
    lookback_hours = int(config.get("news", "lookback_hours", default=24))
    limit = int(config.get("news", "limit", default=10))
    include_content = bool(config.get("news", "include_content", default=False))
    provider = str(config.get("news", "provider", default="alpaca"))

    warnings: list[str] = []
    try:
        raw_items = alpaca.get_news(
            symbols,
            start=(now - timedelta(hours=lookback_hours)).isoformat(),
            end=now.isoformat(),
            limit=limit,
            include_content=include_content,
            sort="desc",
        )
    except Exception as exc:  # noqa: BLE001 - news should not crash read-only decisioning
        raw_items = []
        warnings.append(f"News retrieval failed: {exc}")

    items = [_build_news_item(raw) for raw in raw_items[:limit]]
    if not items:
        warnings.append("No recent news items were returned.")

    return NewsContext(
        provider=provider,
        generated_at=now.isoformat(),
        lookback_hours=lookback_hours,
        symbols=symbols,
        item_count=len(items),
        items=items,
        warnings=warnings,
    )


def _build_news_item(raw: dict[str, Any]) -> NewsItem:
    symbols = raw.get("symbols") or []
    if not isinstance(symbols, list):
        symbols = []
    return NewsItem(
        id=raw.get("id"),
        headline=raw.get("headline"),
        summary=raw.get("summary"),
        author=raw.get("author"),
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
        symbols=[str(symbol) for symbol in symbols],
        url=raw.get("url"),
    )

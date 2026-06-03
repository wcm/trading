from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from trading_bot.config import AppConfig
from trading_bot.data.events import EventContext
from trading_bot.data.market_data import MarketContext
from trading_bot.data.news import NewsContext
from trading_bot.strategy.put_credit_spread import PutCreditSpreadScanResult


@dataclass(frozen=True)
class DecisionPacket:
    packet_version: str
    generated_at: str
    mode: str
    strategy: str
    account: dict[str, Any]
    clock: dict[str, Any]
    positions: list[dict[str, Any]]
    open_orders: list[dict[str, Any]]
    risk_limits: dict[str, Any]
    market_filters: dict[str, Any]
    market_context: dict[str, Any]
    event_context: dict[str, Any]
    option_scan: dict[str, Any]
    news_context: dict[str, Any]
    instructions: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_decision_packet(
    *,
    config: AppConfig,
    account: dict[str, Any],
    clock: dict[str, Any],
    positions: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    scan_result: PutCreditSpreadScanResult,
    market_context: MarketContext,
    event_context: EventContext,
    news_context: NewsContext,
) -> DecisionPacket:
    return DecisionPacket(
        packet_version="put_credit_spread_decision_v1",
        generated_at=datetime.now(UTC).isoformat(),
        mode=config.mode,
        strategy=str(config.get("strategy", "name", default="put_credit_spread")),
        account=_compact_account(account),
        clock={
            "is_open": clock.get("is_open"),
            "timestamp": clock.get("timestamp"),
            "next_open": clock.get("next_open"),
            "next_close": clock.get("next_close"),
        },
        positions=[_compact_position(position) for position in positions],
        open_orders=[_compact_order(order) for order in open_orders],
        risk_limits=config.get("risk", default={}),
        market_filters=config.get("market_filters", default={}),
        market_context=market_context.to_dict(),
        event_context=event_context.to_dict(),
        option_scan=scan_result.to_dict(),
        news_context=news_context.to_dict(),
        instructions={
            "allowed_actions": config.get("decision_engine", "allowed_actions", default=[]),
            "strategy_scope": "put_credit_spread_only",
            "read_only": True,
            "must_choose_from_candidate_ids": [
                candidate.candidate_id for candidate in scan_result.candidates
            ],
            "no_order_placement_in_this_phase": True,
            "no_trade_is_valid": True,
            "limit_price_for_credit_spread_must_be_negative": True,
            "max_quantity": 1,
        },
    )


def _compact_account(account: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "status",
        "equity",
        "buying_power",
        "cash",
        "portfolio_value",
        "pattern_day_trader",
        "trading_blocked",
        "account_blocked",
    ]
    return {key: account.get(key) for key in keys if key in account}


def _compact_position(position: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "symbol",
        "asset_class",
        "qty",
        "side",
        "market_value",
        "cost_basis",
        "unrealized_pl",
        "unrealized_plpc",
        "current_price",
    ]
    return {key: position.get(key) for key in keys if key in position}


def _compact_order(order: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "client_order_id",
        "created_at",
        "symbol",
        "asset_class",
        "qty",
        "type",
        "side",
        "order_class",
        "limit_price",
        "status",
        "legs",
    ]
    return {key: order.get(key) for key in keys if key in order}


def candidate_dicts_by_id(scan_result: PutCreditSpreadScanResult) -> dict[str, dict[str, Any]]:
    return {candidate.candidate_id: candidate.to_dict() for candidate in scan_result.candidates}


def packet_candidate_ids(scan_result: PutCreditSpreadScanResult) -> set[str]:
    return set(candidate_dicts_by_id(scan_result))

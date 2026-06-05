from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time as dt_time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_bot.config import AppConfig
from trading_bot.storage.db import (
    count_submitted_open_attempts,
    first_account_snapshot,
    record_account_snapshot,
)
from trading_bot.utils.market_time import EASTERN, eastern_date_window_utc
from trading_bot.utils.money import decimal_or_none, format_optional_decimal


@dataclass(frozen=True)
class AccountRiskState:
    generated_at: str
    blocks_new_opens: bool
    block_reasons: list[str]
    equity: str | None
    last_equity: str | None
    daily_pnl: str | None
    weekly_pnl: str | None
    emergency_equity_floor: str | None
    max_daily_loss: str | None
    max_weekly_loss: str | None
    max_new_trades_per_day: int
    new_trades_today: int
    daily_window: dict[str, str]
    weekly_window: dict[str, str]
    weekly_pnl_source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_account_risk_state(
    *,
    config: AppConfig,
    db_path: Path,
    account: dict[str, Any],
    now: datetime | None = None,
) -> AccountRiskState:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    now_iso = now.isoformat()
    mode = config.mode

    day_start, day_end = eastern_date_window_utc(now.astimezone(EASTERN).date())
    week_start, week_end = _eastern_week_window_utc(now)
    new_trades_today = count_submitted_open_attempts(
        db_path,
        mode=mode,
        start_at=day_start,
        end_at=day_end,
    )

    equity = decimal_or_none(account.get("equity"))
    last_equity = decimal_or_none(account.get("last_equity"))
    daily_pnl = equity - last_equity if equity is not None and last_equity is not None else None

    first_week_snapshot = first_account_snapshot(
        db_path,
        mode=mode,
        start_at=week_start,
        end_at=week_end,
    )
    weekly_start_equity = None
    if first_week_snapshot and isinstance(first_week_snapshot.get("payload"), dict):
        weekly_start_equity = decimal_or_none(first_week_snapshot["payload"].get("equity"))
    weekly_pnl = equity - weekly_start_equity if equity is not None and weekly_start_equity is not None else None

    record_account_snapshot(
        db_path,
        captured_at=now_iso,
        broker=config.broker,
        mode=mode,
        payload=account,
    )

    block_reasons = _account_risk_block_reasons(
        config=config,
        equity=equity,
        daily_pnl=daily_pnl,
        weekly_pnl=weekly_pnl,
        new_trades_today=new_trades_today,
    )

    return AccountRiskState(
        generated_at=now_iso,
        blocks_new_opens=bool(block_reasons),
        block_reasons=block_reasons,
        equity=format_optional_decimal(equity),
        last_equity=format_optional_decimal(last_equity),
        daily_pnl=format_optional_decimal(daily_pnl),
        weekly_pnl=format_optional_decimal(weekly_pnl),
        emergency_equity_floor=format_optional_decimal(
            decimal_or_none(config.get("account", "emergency_equity_floor"))
        ),
        max_daily_loss=format_optional_decimal(decimal_or_none(config.get("risk", "max_daily_loss"))),
        max_weekly_loss=format_optional_decimal(decimal_or_none(config.get("risk", "max_weekly_loss"))),
        max_new_trades_per_day=int(config.get("risk", "max_new_trades_per_day", default=0)),
        new_trades_today=new_trades_today,
        daily_window={"start_at": day_start, "end_at": day_end},
        weekly_window={"start_at": week_start, "end_at": week_end},
        weekly_pnl_source="account_snapshots" if weekly_pnl is not None else "unavailable",
    )


def _account_risk_block_reasons(
    *,
    config: AppConfig,
    equity: Decimal | None,
    daily_pnl: Decimal | None,
    weekly_pnl: Decimal | None,
    new_trades_today: int,
) -> list[str]:
    reasons: list[str] = []
    emergency_floor = decimal_or_none(config.get("account", "emergency_equity_floor"))
    if emergency_floor is not None and equity is not None and equity <= emergency_floor:
        reasons.append(f"Equity {equity} is at or below emergency floor {emergency_floor}")

    max_daily_loss = decimal_or_none(config.get("risk", "max_daily_loss"))
    if max_daily_loss is not None and max_daily_loss > 0 and daily_pnl is not None:
        if daily_pnl <= -max_daily_loss:
            reasons.append(f"Daily P&L {daily_pnl} breaches max_daily_loss {max_daily_loss}")

    max_weekly_loss = decimal_or_none(config.get("risk", "max_weekly_loss"))
    if max_weekly_loss is not None and max_weekly_loss > 0 and weekly_pnl is not None:
        if weekly_pnl <= -max_weekly_loss:
            reasons.append(f"Weekly P&L {weekly_pnl} breaches max_weekly_loss {max_weekly_loss}")

    max_new_trades = int(config.get("risk", "max_new_trades_per_day", default=0))
    if max_new_trades > 0 and new_trades_today >= max_new_trades:
        reasons.append(f"New trade limit reached: {new_trades_today}/{max_new_trades} today")

    return reasons


def _eastern_week_window_utc(now: datetime) -> tuple[str, str]:
    now_et = now.astimezone(EASTERN)
    week_start_date = now_et.date() - timedelta(days=now_et.weekday())
    week_end_date = date.fromordinal(week_start_date.toordinal() + 7)
    start = datetime.combine(week_start_date, dt_time.min, tzinfo=EASTERN).astimezone(UTC)
    end = datetime.combine(week_end_date, dt_time.min, tzinfo=EASTERN).astimezone(UTC)
    return start.isoformat(), end.isoformat()

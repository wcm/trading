from __future__ import annotations

from datetime import UTC, date, datetime, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


def daily_summary_date_from_arg(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(UTC).astimezone(EASTERN).date()


def eastern_date_window_utc(summary_date: date) -> tuple[str, str]:
    start = datetime.combine(summary_date, dt_time.min, tzinfo=EASTERN).astimezone(UTC)
    end = datetime.combine(date.fromordinal(summary_date.toordinal() + 1), dt_time.min, tzinfo=EASTERN)
    end = end.astimezone(UTC)
    return start.isoformat(), end.isoformat()


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def age_seconds(now: datetime, timestamp: datetime | None) -> int | None:
    if timestamp is None:
        return None
    age = int((now - timestamp).total_seconds())
    if -60 <= age < 0:
        return 0
    return age

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_agent.domain.errors import DomainError

INTERVAL_SECONDS = {"1m": 60, "1h": 3600, "4h": 14400, "6h": 21600, "12h": 43200}
TIMEFRAMES = ("1h", "4h", "6h", "12h")
TRENDS = ("4h", "6h", "12h")
OKX_BARS = {"1m": "1m", "1h": "1H", "4h": "4H", "6h": "6Hutc", "12h": "12Hutc"}


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainError("INVALID_TIME", "时间必须包含时区")
    return value.astimezone(UTC)


def boundary(value: datetime, timeframe: str = "1h") -> datetime:
    seconds = INTERVAL_SECONDS[timeframe]
    return datetime.fromtimestamp(int(utc(value).timestamp()) // seconds * seconds, UTC)


def require_anchor(value: datetime) -> datetime:
    value = utc(value)
    if boundary(value) != value:
        raise DomainError("INVALID_ANCHOR", "预测时段必须为 UTC 整点")
    return value


def window(anchor: datetime) -> tuple[datetime, datetime]:
    start = require_anchor(anchor) + timedelta(minutes=5)
    return start, start + timedelta(hours=1)


def within_deadline(now: datetime, deadline: datetime) -> bool:
    return utc(now) <= utc(deadline)

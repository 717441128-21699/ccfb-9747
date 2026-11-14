"""UTC 时间工具：所有时间戳统一为带 Z 的 ISO-8601 字符串。"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def now_iso() -> str:
    """当前 UTC 时间，毫秒精度，Z 后缀。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_http_date(value: str) -> datetime | None:
    """解析 Last-Modified 这类 RFC 7231 日期；失败返回 None。"""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def http_date(dt: datetime) -> str:
    from email.utils import format_datetime
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)

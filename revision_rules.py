"""返修规则：窗口期、可返修决定类型与逐条回复校验，独立维护便于调整。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 作者在决定发出后 7 天内可提交修订稿。
REVISION_WINDOW_DAYS = 7
# 仅修订类决定（大修/小修）开放返修交付。
REVISABLE_DECISIONS = frozenset({"major_revision", "minor_revision"})
# 逐条回复的最短长度。
MIN_RESPONSE_LENGTH = 10


def parse_timestamp(value: str) -> datetime:
    """解析系统使用的 ISO 时间戳。"""
    return datetime.fromisoformat(value)


def deadline_for(decided_at: str) -> datetime:
    """返修截止时刻 = 决定时刻 + 窗口期。"""
    return parse_timestamp(decided_at) + timedelta(days=REVISION_WINDOW_DAYS)


def is_overdue(decided_at: str, now: datetime | None = None) -> bool:
    """判断当前时刻是否已超过返修截止时刻。"""
    return (now or datetime.now(timezone.utc)) > deadline_for(decided_at)

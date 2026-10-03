"""Teacher adoption from PR117's retained usage ledger, without content or LLMs.

These are observed-action proxies, not teacher-confirmed value metrics. The
existing event writer is best-effort and has no deployment coverage marker or
QA cohort, so this service never claims complete historical coverage.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import math
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from backend.analytics.admin_usage import EVENTS_FOR_ACTIVATION
from backend.db.models import AdminUsageEventRecord as Event, UserRecord
from backend.db.session import session_scope

VERSION = "observed-teacher-actions-v1"
BUSINESS_EVENTS = EVENTS_FOR_ACTIVATION - {"submission_created"}
MAX_ROWS = 100_000
DAY = 86400


class AnalyticsRangeTooLarge(ValueError):
    pass


def query_adoption(*, start: float, end: float, timezone: str = "Asia/Singapore", now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not all(math.isfinite(value) for value in (start, end)) or not 0 < start < end <= now:
        raise ValueError("invalid_time_range")
    if end - start > 93 * DAY:
        raise ValueError("range_exceeds_93_days")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("invalid_timezone") from None

    eligible = [Event.role == "teacher", Event.user_id.is_not(None), Event.success.is_(True), Event.occurred_at < end]
    names = sorted(BUSINESS_EVENTS | {"account_created", "login_success"})
    # Fetch only the three columns needed for aggregates. Never load dimensions,
    # provider settings, names, emails, passwords, prompts or student content.
    with session_scope() as session:
        rows = session.execute(select(Event.event_name, Event.user_id, Event.occurred_at)
            .where(*eligible, Event.event_name.in_(names), Event.occurred_at >= start)
            .order_by(Event.occurred_at).limit(MAX_ROWS + 1)).all()
        first_actions = session.execute(select(Event.user_id, func.min(Event.occurred_at))
            .where(*eligible, Event.event_name.in_(sorted(BUSINESS_EVENTS)))
            .group_by(Event.user_id).limit(MAX_ROWS + 1)).all()
        coverage_start = session.scalar(select(func.min(Event.occurred_at)).where(*eligible, Event.event_name.in_(names)))
        last_event = session.scalar(select(func.max(Event.occurred_at)).where(*eligible, Event.event_name.in_(names)))
        accounts = int(session.scalar(select(func.count()).select_from(UserRecord).where(UserRecord.role == "teacher")) or 0)
    if len(rows) > MAX_ROWS or len(first_actions) > MAX_ROWS:
        raise AnalyticsRangeTooLarge("analytics_range_too_large")

    buckets: dict[str, dict] = {}
    cursor = datetime.fromtimestamp(start, zone).replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor.timestamp() < end:
        next_day = cursor + timedelta(days=1)
        key = cursor.date().isoformat()
        observed = coverage_start is not None and next_day.timestamp() > coverage_start
        buckets[key] = {"date": key, "start": max(start, cursor.timestamp()), "end": min(end, next_day.timestamp()),
                        "status": "partial" if observed else "unavailable", "new": set(), "login": set(), "business": set(), "actions": 0}
        cursor = next_day

    active: set[str] = set()
    logins: set[str] = set()
    new_users: set[str] = set()
    action_days: dict[str, set[str]] = defaultdict(set)
    actions_by_user: dict[str, list[float]] = defaultdict(list)
    for name, user_id, occurred_at in rows:
        key = datetime.fromtimestamp(occurred_at, zone).date().isoformat()
        bucket = buckets[key]
        if name == "account_created":
            bucket["new"].add(user_id)
            new_users.add(user_id)
        elif name == "login_success":
            bucket["login"].add(user_id)
            logins.add(user_id)
        else:
            bucket["business"].add(user_id)
            bucket["actions"] += 1
            active.add(user_id)
            action_days[user_id].add(key)
            actions_by_user[user_id].append(occurred_at)

    series = [{"date": b["date"], "start": b["start"], "end": b["end"], "status": b["status"],
               "new_users": len(b["new"]) if b["status"] != "unavailable" else None,
               "login_users": len(b["login"]) if b["status"] != "unavailable" else None,
               "business_users": len(b["business"]) if b["status"] != "unavailable" else None,
               "business_actions": b["actions"] if b["status"] != "unavailable" else None} for b in buckets.values()]

    # Relative elapsed-time windows, not calendar weeks: only fully matured
    # users enter each denominator. Disabled/deleted users remain in events.
    cohorts: dict[str, dict] = {}
    for user_id, first in first_actions:
        if not start <= first < end:
            continue
        date = datetime.fromtimestamp(first, zone).date().isoformat()
        cohort = cohorts.setdefault(date, {"date": date, "users": 0, "w1": _cell(), "w4": _cell()})
        cohort["users"] += 1
        for key, days in (("w1", 7), ("w4", 28)):
            lower, upper = first + days * DAY, first + (days + 7) * DAY
            cell = cohort[key]
            if upper > end:
                cell["pending"] += 1
                continue
            cell["denominator"] += 1
            cell["numerator"] += int(any(lower <= timestamp < upper for timestamp in actions_by_user[user_id]))
    for cohort in cohorts.values():
        for key in ("w1", "w4"):
            cell = cohort[key]
            cell["rate"] = cell["numerator"] / cell["denominator"] if cell["denominator"] else None
    total_actions = sum(b["actions"] for b in buckets.values())
    frequency = [{"label": label, "users": sum(lower <= len(days) <= upper for days in action_days.values())}
                 for label, lower, upper in (("1 天", 1, 1), ("2–3 天", 2, 3), ("4–7 天", 4, 7), ("8 天及以上", 8, 94))]
    observed = coverage_start is not None and coverage_start < end
    return {
        "metric_version": VERSION, "as_of": now, "start": start, "end": end, "timezone": timezone,
        "coverage": {"status": "partial" if observed else "unavailable", "first_retained_event_at": coverage_start,
                     "last_retained_event_at": last_event, "event_count": len(rows),
                     "notes": ["起点是最早保留事件，不是部署或完整采集起点；此前数据未知。", "业务事件尽力写入，可能遗漏；历史未回填。", "仅按事件发生时的教师角色统计，无法区分内部测试与真实试用账号。", "停用或删除账号的历史事件仍计入；没有用户身份明细。"]},
        "filters": {"event_role": "teacher", "success": True, "account_cohort": "all_unclassified", "environment": "current_database"},
        "summary": {"current_teacher_accounts": accounts, "new_users": len(new_users) if observed else None,
                    "login_users": len(logins) if observed else None, "business_users": len(active) if observed else None,
                    "business_actions": total_actions if observed else None,
                    "actions_per_business_user": total_actions / len(active) if active else None},
        "series": series, "frequency": frequency if observed else [], "retention": sorted(cohorts.values(), key=lambda c: c["date"]),
        "definitions": {"growth": "新增 = 已记录 account_created 的去重教师；当前账号存量单独展示，删除后存量会变化。",
                        "activity": "业务活跃 = 至少一次成功建任务、建课程、建作业或启动批改；登录单列，不代表已完成批改。",
                        "frequency": "使用频率 = 区间内每位业务活跃教师有业务动作的日数；人均动作 = 已记录动作条数 / 业务活跃人数。",
                        "retention": "以最早保留业务动作为代理起点；W1=[7,14天)，W4=[28,35天)，回访人数 / 已完整经历窗口的人数。尚未成熟者不进分母。",
                        "business_events": sorted(BUSINESS_EVENTS)},
    }


def _cell() -> dict:
    return {"numerator": 0, "denominator": 0, "pending": 0, "rate": None}

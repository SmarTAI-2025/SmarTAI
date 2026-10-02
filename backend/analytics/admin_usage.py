"""Durable product usage events and deterministic admin metrics.

Only bounded operational labels are accepted here.  The private admin API can
query this ledger, while product requests record events through the small
``track_usage_event`` helper so analytics failures never break user traffic.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import time
import uuid
from typing import Any, Iterable, Mapping

from sqlalchemy import select

from backend.db.models import AdminUsageEventRecord
from backend.db.session import session_scope

logger = logging.getLogger(__name__)

EVENTS_FOR_ACTIVE_USERS = frozenset({
    "login_success", "course_created", "assignment_created", "grading_run_started",
    "task_created", "submission_created",
})
EVENTS_FOR_ACTIVATION = EVENTS_FOR_ACTIVE_USERS - {"login_success"}

METRIC_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "active_users", "label": "活跃用户", "description": "在时间范围内登录或完成业务动作的去重用户数", "event_names": sorted(EVENTS_FOR_ACTIVE_USERS)},
    {"key": "activated_users", "label": "已激活用户", "description": "在时间范围内完成首个业务动作的去重用户数", "event_names": sorted(EVENTS_FOR_ACTIVATION)},
    {"key": "new_users", "label": "新增账号", "description": "完成账号创建的数量", "event_names": ["account_created"]},
    {"key": "login_successes", "label": "登录次数", "description": "成功登录次数", "event_names": ["login_success"]},
    {"key": "tasks_created", "label": "任务创建", "description": "创建批改任务的次数", "event_names": ["task_created"]},
    {"key": "courses_created", "label": "课程创建", "description": "创建课程的次数", "event_names": ["course_created"]},
    {"key": "assignments_created", "label": "作业创建", "description": "创建作业的次数", "event_names": ["assignment_created"]},
    {"key": "grading_runs_started", "label": "批改启动", "description": "启动批改运行的次数", "event_names": ["grading_run_started"]},
    {"key": "submissions_created", "label": "提交次数", "description": "学生提交作业的次数", "event_names": ["submission_created"]},
    {"key": "ai_calls", "label": "AI 调用", "description": "已接入流水的 AI 调用次数", "event_names": ["ai_call"]},
    {"key": "ai_failures", "label": "AI 失败", "description": "已接入流水的 AI 调用失败次数", "event_names": ["ai_call"]},
    {"key": "input_tokens", "label": "输入 token", "description": "已接入流水的输入 token 总数", "event_names": ["ai_call"]},
    {"key": "output_tokens", "label": "输出 token", "description": "已接入流水的输出 token 总数", "event_names": ["ai_call"]},
    {"key": "estimated_cost_usd", "label": "估算费用（美元）", "description": "已接入流水且带费用字段的调用成本", "event_names": ["ai_call"]},
)
_CATALOG_BY_KEY = {item["key"]: item for item in METRIC_CATALOG}


def record_usage_event_in_session(
    session,
    *,
    event_name: str,
    user_id: str | None = None,
    role: str | None = None,
    occurred_at: float | None = None,
    success: bool = True,
    duration_ms: float | None = None,
    provider: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    dimensions: Mapping[str, str | int | float | bool | None] | None = None,
) -> None:
    """Add one bounded event to an existing transaction."""
    if not event_name or len(event_name) > 64:
        raise ValueError("event_name must be 1-64 characters")
    safe_dimensions = {
        str(key)[:64]: value
        for key, value in (dimensions or {}).items()
        if value is not None
    }
    session.add(AdminUsageEventRecord(
        id=uuid.uuid4().hex,
        event_name=event_name,
        user_id=user_id,
        role=role,
        occurred_at=occurred_at or time.time(),
        success=bool(success),
        duration_ms=duration_ms,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        dimensions=safe_dimensions or None,
    ))


def track_usage_event(**kwargs: Any) -> None:
    """Best-effort event write for requests that already committed their work."""
    try:
        with session_scope() as session:
            record_usage_event_in_session(session, **kwargs)
    except Exception:
        logger.exception("usage_event_write_failed", extra={"event_name": kwargs.get("event_name")})


def metrics_catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in METRIC_CATALOG]


def _bucket_start(timestamp: float, granularity: str) -> datetime:
    current = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if granularity == "week":
        current = current - timedelta(days=current.weekday())
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


def _bucket_label(bucket: datetime, granularity: str) -> str:
    return bucket.date().isoformat() if granularity == "day" else bucket.date().isoformat()


def _metric_value(metric: str, rows: Iterable[AdminUsageEventRecord]) -> int | float:
    rows = list(rows)
    if metric in {"active_users", "activated_users"}:
        names = EVENTS_FOR_ACTIVE_USERS if metric == "active_users" else EVENTS_FOR_ACTIVATION
        return len({row.user_id for row in rows if row.user_id and row.event_name in names})
    if metric == "new_users":
        return sum(row.event_name == "account_created" for row in rows)
    if metric == "login_successes":
        return sum(row.event_name == "login_success" and row.success for row in rows)
    if metric == "tasks_created":
        return sum(row.event_name == "task_created" and row.success for row in rows)
    if metric == "courses_created":
        return sum(row.event_name == "course_created" and row.success for row in rows)
    if metric == "assignments_created":
        return sum(row.event_name == "assignment_created" and row.success for row in rows)
    if metric == "grading_runs_started":
        return sum(row.event_name == "grading_run_started" and row.success for row in rows)
    if metric == "submissions_created":
        return sum(row.event_name == "submission_created" and row.success for row in rows)
    if metric == "ai_calls":
        return sum(row.event_name == "ai_call")
    if metric == "ai_failures":
        return sum(row.event_name == "ai_call" and not row.success for row in rows)
    if metric == "input_tokens":
        return sum((row.input_tokens or 0) for row in rows if row.event_name == "ai_call")
    if metric == "output_tokens":
        return sum((row.output_tokens or 0) for row in rows if row.event_name == "ai_call")
    if metric == "estimated_cost_usd":
        return round(sum((row.cost_usd or 0.0) for row in rows if row.event_name == "ai_call"), 6)
    raise ValueError(f"Unsupported metric: {metric}")


def query_usage_metrics(
    *,
    start: float,
    end: float,
    granularity: str,
    metrics: list[str],
) -> dict[str, Any]:
    if start >= end:
        raise ValueError("start must be before end")
    if end - start > 366 * 86400:
        raise ValueError("The maximum metrics range is 366 days")
    unknown = sorted(set(metrics) - set(_CATALOG_BY_KEY))
    if unknown:
        raise ValueError(f"Unsupported metrics: {', '.join(unknown)}")
    with session_scope() as session:
        rows = session.scalars(
            select(AdminUsageEventRecord)
            .where(AdminUsageEventRecord.occurred_at >= start, AdminUsageEventRecord.occurred_at < end)
            .order_by(AdminUsageEventRecord.occurred_at)
        ).all()
    # Administrator navigation and maintenance must not make product
    # adoption look active.  Product metrics count teacher/student activity;
    # operator actions remain available in the audit feed.
    rows = [row for row in rows if row.role != "admin"]

    grouped: dict[str, list[AdminUsageEventRecord]] = defaultdict(list)
    for row in rows:
        grouped[_bucket_label(_bucket_start(row.occurred_at, granularity), granularity)].append(row)
    availability: dict[str, str] = {}
    event_names = {row.event_name for row in rows}
    for metric in metrics:
        supported = set(_CATALOG_BY_KEY[metric]["event_names"])
        available = bool(event_names & supported)
        if metric == "input_tokens":
            available = any(row.event_name == "ai_call" and row.input_tokens is not None for row in rows)
        elif metric == "output_tokens":
            available = any(row.event_name == "ai_call" and row.output_tokens is not None for row in rows)
        elif metric == "estimated_cost_usd":
            available = any(row.event_name == "ai_call" and row.cost_usd is not None for row in rows)
        availability[metric] = "available" if available else "not_collected"
    series = [
        {
            "bucket": bucket,
            **{
                metric: _metric_value(metric, bucket_rows) if availability[metric] == "available" else None
                for metric in metrics
            },
        }
        for bucket, bucket_rows in sorted(grouped.items())
    ]
    totals = {
        metric: _metric_value(metric, rows) if availability[metric] == "available" else None
        for metric in metrics
    }
    return {
        "status": "available" if rows else "not_collected",
        "start": start,
        "end": end,
        "granularity": granularity,
        "metrics": metrics,
        "metric_status": availability,
        "series": series,
        "totals": totals,
        "event_count": len(rows),
    }

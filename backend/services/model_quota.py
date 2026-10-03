"""Commit admission BEFORE dispatch. Failed/cancelled calls remain charged.

SDK-internal retries are one logical invocation; a new logical retry is another.
The input estimate preserves the existing length/4 heuristic. No actual/output
token or cost claims. UTC days; no in-process allowance or cache. SQLite writes
serialize with BEGIN IMMEDIATE, PostgreSQL admissions lock the owner row.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select, text
from backend.config import settings
from backend.db.model_quota_models import ModelDailyUsageRecord
from backend.db.models import UserRecord
from backend.db.session import get_session
from backend.services.business_config import read_business_config

class ModelQuotaError(RuntimeError):
    retryable = False

def utc_now():
    return datetime.now(timezone.utc)

def _limits(config, scope):
    if scope == "shared":
        return int(config.values["shared_pool_daily_request_limit"]), int(config.values["shared_pool_daily_estimated_token_limit"])
    if scope == "history":
        return int(config.values["history_query_llm_daily_limit"]), -1
    raise ValueError("Unknown model quota scope")

def admit_model_call(owner_id: str, scope: str, *, estimated_input_tokens: int = 0, now=None):
    if type(estimated_input_tokens) is not int or estimated_input_tokens < 0:
        raise ValueError("Invalid estimate")
    moment = (now or utc_now()).astimezone(timezone.utc)
    day = moment.date().isoformat()
    with get_session() as session:
        try:
            if session.bind.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            owner = session.scalar(select(UserRecord).where(UserRecord.id == owner_id).with_for_update())
            if owner is None:
                raise ModelQuotaError("model_quota_owner_unavailable")
            config = read_business_config(session, owner_id)
            request_limit, token_limit = _limits(config, scope)
            row = session.get(ModelDailyUsageRecord, (owner_id, scope, day))
            requests, tokens = (row.requests, row.estimated_input_tokens) if row else (0, 0)
            if (request_limit >= 0 and requests + 1 > request_limit) or (token_limit >= 0 and tokens + estimated_input_tokens > token_limit):
                raise ModelQuotaError("shared_pool_daily_limit_reached" if scope == "shared" else "history_query_daily_limit_reached")
            if scope == "history":
                last = session.scalar(select(func.max(ModelDailyUsageRecord.last_admitted_at)).where(ModelDailyUsageRecord.owner_id == owner_id, ModelDailyUsageRecord.scope == scope))
                if last is not None and moment.timestamp() - last < max(0, settings.history_query_llm_cooldown_seconds):
                    raise ModelQuotaError("history_query_cooldown")
            if row is None:
                row = ModelDailyUsageRecord(owner_id=owner_id, scope=scope, day=day, requests=0, estimated_input_tokens=0)
                session.add(row)
            row.requests += 1
            row.estimated_input_tokens += estimated_input_tokens
            row.last_admitted_at = moment.timestamp()
            session.commit()
        except BaseException:
            session.rollback()
            raise

def model_usage(session, owner_id: str, *, config=None, now=None):
    moment = (now or utc_now()).astimezone(timezone.utc)
    config = config or read_business_config(session, owner_id)
    day = moment.date().isoformat()
    rows = {row.scope: row for row in session.scalars(select(ModelDailyUsageRecord).where(ModelDailyUsageRecord.owner_id == owner_id, ModelDailyUsageRecord.day == day))}
    result = {"day": day, "timezone": "UTC", "resets_at": datetime.combine(moment.date()+timedelta(days=1), datetime.min.time(), timezone.utc).isoformat(),
              "shared_pool_enabled": bool(settings.shared_pool_enabled), "history_enabled": bool(settings.history_query_llm_enabled)}
    for scope in ("shared", "history"):
        row = rows.get(scope)
        requests, tokens = (row.requests, row.estimated_input_tokens) if row else (0, 0)
        req_limit, tok_limit = _limits(config, scope)
        result[scope] = {"requests": requests, "estimated_input_tokens": tokens, "request_limit": req_limit,
                         "estimated_token_limit": tok_limit, "remaining_requests": None if req_limit < 0 else max(0, req_limit-requests)}
    return result
